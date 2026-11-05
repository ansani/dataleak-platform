"""Orchestratore principale: poll M365 -> classifica NVIDIA -> esegue azioni -> risponde S/MIME."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

import config
from utils import setup_logging

from m365.auth import build_auth_from_env
from m365.client import M365Client
from classifier.nvidia_client import NVIDIAClassifier
from actions.runners import run_kleak, run_infostealer
from smime.signer import load_signature_html, build_mime_message, sign_smime
from db.store import ProcessedDB
from templates.responses import (
    get_template_for_intent,
    get_template_config,
    render_template,
)

logger = logging.getLogger("datal leak reply")

TEST_RECIPIENT = config.TEST_RECIPIENT


def extract_sender(msg: dict[str, Any]) -> str:
    from_field = msg.get("from", {})
    email_address = from_field.get("emailAddress", {}).get("address", "")
    return email_address


def extract_body(msg: dict[str, Any]) -> str:
    body = msg.get("body", {})
    return body.get("content", "") or msg.get("bodyPreview", "")


def build_response_from_template(
    intent: str,
    script_output: str | None,
    lang: str,
    sender: str,
    classification: dict[str, Any],
) -> str | None:
    """Costruisce il corpo della risposta usando i template di kirurusec.

    Restituisce None se non deve essere inviata alcuna risposta (handling=none).
    Restituisce il testo del template con i placeholder sostituiti (senza firma),
    pronto per essere passato al modello NVIDIA per la raffinazione.
    """
    template_cfg = get_template_config(intent)
    handling = template_cfg.get("handling", "review")

    if handling == "none":
        return None

    template_text = get_template_for_intent(intent)
    if template_text is None:
        logger.warning("Nessun template trovato per intent: %s", intent)
        return None

    # Costruisce i valori per i placeholder
    local_name = sender.split("@")[0] if "@" in sender else "there"
    values: dict[str, str] = {
        "recipient_name": local_name,
        "affected_email": sender,
        "uri_list": script_output or "[non disponibile]",
        "device_name": script_output or "[non disponibile]",
        "device_os": "[non disponibile]",
        "telegram_group_id": "[non disponibile]",
        "source_name": "dataset infostealer",
        "log_timestamp": "[non disponibile]",
        "evidence_summary": classification.get("evidence", "[non disponibile]"),
        "malware_name": "[non disponibile]",
        "researcher_name": "KiruruSec",
        "website_url": "https://kirurusec.com",
        "support_reference": classification.get("evidence", "")[:50] or "[non disponibile]",
    }

    body = render_template(template_text, values)

    return body


def process_email(
    msg: dict[str, Any],
    client: M365Client,
    classifier: NVIDIAClassifier,
    db: ProcessedDB,
    signature_html: str,
    smime_cert_path: str,
    smime_cert_password: str,
    from_addr: str,
    original_leak_subject: str | None = None,
    original_leak_body: str | None = None,
    override_to: str | None = None,
    skip_already_processed: bool = True,
) -> str:
    """Processa una singola email e restituisce lo stato."""
    message_id = msg.get("id", "")
    internet_message_id = msg.get("internetMessageId", "")
    conversation_id = msg.get("conversationId", "")
    subject = msg.get("subject", "(nessun oggetto)")
    sender = extract_sender(msg)
    body = extract_body(msg)
    received = msg.get("receivedDateTime", "")

    # Destinatario della risposta: override se fornito (modalità test), altrimenti il mittente originale
    to_recipient = override_to if override_to else sender

    if not internet_message_id:
        logger.warning("Email senza internetMessageId, salto: %s", message_id)
        return "skipped_no_id"

    if skip_already_processed and db.is_processed(internet_message_id):
        logger.info("Già processata: %s", internet_message_id)
        return "already_processed"

    # Classifica via NVIDIA
    logger.debug(
        "Invio a NVIDIA per classificazione: oggetto='%s', corpo_lunghezza=%d",
        subject, len(body),
    )
    classification = classifier.classify(
        email_subject=subject,
        email_body=body,
        original_leak_subject=original_leak_subject,
        original_leak_body=original_leak_body,
    )

    is_dataleak_reply = classification.get("is_dataleak_reply", False)
    primary_intent = classification.get("primary_intent", "non_classificabile")
    secondary_intents = classification.get("secondary_intents", [])
    confidence = classification.get("confidence", 0.0)
    recommended_action = classification.get("recommended_action", "human_review")
    urgency = classification.get("urgency", "low")
    requires_lookup = classification.get("requires_lookup", False)
    requires_human_review = classification.get("requires_human_review", False)
    reply_lang = classification.get("reply_language", classification.get("language", "en"))
    summary = classification.get("summary", "")
    evidence = classification.get("evidence", "")

    logger.info(
        "Classificazione NVIDIA: is_dataleak_reply=%s, primary_intent=%s, "
        "action=%s, urgency=%s, lang=%s, confidence=%.4f",
        is_dataleak_reply, primary_intent, recommended_action, urgency,
        reply_lang, confidence,
    )
    logger.debug("Classificazione: summary=%s, evidence=%s", summary, evidence)

    # Decidiamo se rispondere
    if not is_dataleak_reply or recommended_action == "no_reply":
        db.mark_processed(internet_message_id, conversation_id, subject, primary_intent, "ignored")
        return "ignored"

    if requires_human_review:
        db.mark_processed(internet_message_id, conversation_id, subject, primary_intent, "human_review_required")
        logger.info("Richiede revisione umana, nessun invio automatico.")
        return "human_review_required"

    lang = reply_lang if reply_lang else ("it" if is_dataleak_reply else "en")

    # Esegui script se richiesto (usa sempre il mittente originale come input per gli script)
    script_output: str | None = None
    script_intent = primary_intent
    if primary_intent in ("kleak", "lista_URI", "data_infezione", "messa_in_sicurezza"):
        logger.info(
            "Intent %s rilevato, esecuzione script kleak.py con email=%s",
            primary_intent, sender,
        )
        script_intent = "kleak"
    elif primary_intent in ("device", "bonifica_device"):
        logger.info(
            "Intent %s rilevato, esecuzione script infostealer.py con email=%s",
            primary_intent, sender,
        )
        script_intent = "infostealer"

    try:
        if script_intent == "kleak":
            script_output = run_kleak(sender)
            logger.debug("Output script kleak.py: %s", script_output[:500] if script_output else None)
        elif script_intent == "infostealer":
            script_output = run_infostealer(sender)
            logger.debug("Output script infostealer.py: %s", script_output[:500] if script_output else None)
    except Exception as exc:  # noqa: BLE001
        logger.error("Errore esecuzione script per %s: %s", sender, exc)
        script_output = None

    # Componi risposta usando i template
    response_body = build_response_from_template(
        intent=primary_intent,
        script_output=script_output,
        lang=lang,
        sender=sender,
        classification=classification,
    )
    if response_body is None:
        db.mark_processed(internet_message_id, conversation_id, subject, primary_intent, "no_response")
        return "no_response"

    # Passa il testo del template e l'email ricevuta al modello NVIDIA per ottenere il testo definitivo
    logger.debug("Invio testo template e email ricevuta a NVIDIA per raffinazione")
    final_body = classifier.refine_text(response_body, classification, email_received=body)
    logger.debug("Testo raffinato da NVIDIA: %d caratteri", len(final_body))

    # Costruisci citazione dell'email originale (quoted original)
    # Il corpo originale è già HTML (viene da M365), lo includiamo così com'è
    # senza escapare i tag HTML, per permettere la corretta visualizzazione
    # del contenuto originale nel blocco citato.
    safe_sender = html.escape(sender)
    safe_from = html.escape(from_addr)
    safe_subject = html.escape(subject)
    safe_received = html.escape(received)

    # Se il corpo originale contiene tag HTML, lo includiamo direttamente.
    # Altrimenti lo avvolgiamo in un <pre> con escape dei caratteri speciali.
    import re as _re
    body_stripped = body.strip()
    if _re.search(r"<[a-zA-Z][^>]*>", body_stripped):
        # Corpo già HTML, lo includiamo direttamente (i tag verranno renderiti)
        original_body_html = body
    else:
        # Corpo testo semplice, lo avvolgiamo in pre con escape dei caratteri speciali
        original_body_html = f"<pre style=\"white-space: pre-wrap; font-family: monospace; font-size: 12px;\">{html.escape(body)}</pre>"

    quoted_original = f"""
<br/><br/>
<hr/>
<blockquote style="border-left: 2px solid #ccc; padding-left: 10px; margin-left: 10px; color: #555; font-size: 13px;">
  <p style="margin: 4px 0;"><strong>Da:</strong> {safe_sender}</p>
  <p style="margin: 4px 0;"><strong>A:</strong> {safe_from}</p>
  <p style="margin: 4px 0;"><strong>Oggetto:</strong> {safe_subject}</p>
  <p style="margin: 4px 0;"><strong>Data:</strong> {safe_received}</p>
  <hr style="margin: 8px 0; border: 0; border-top: 1px solid #ccc;"/>
  {original_body_html}
</blockquote>
"""

    # Aggiunge firma HTML prima della citazione dell'email originale
    if signature_html:
        final_body = f"{final_body}<br/><br/><div style=\"border-top:1px solid #ccc; margin-top:20px; padding-top:10px;\">{signature_html}</div>"

    final_body = f"{final_body}{quoted_original}"

    reply_subject = f"Re: {subject}" if not subject.startswith("Re:") else subject

    mime_msg = build_mime_message(
        subject=reply_subject,
        body_html=final_body,
        to=to_recipient,
        from_addr=from_addr,
        in_reply_to=internet_message_id,
        references=internet_message_id,
    )

    # Firma S/MIME e invio tramite Graph API (MIME raw, Content-Type: text/plain)
    logger.debug(
        "Invio a S/MIME signer: destinatario=%s, oggetto='%s', corpo_lunghezza=%d",
        to_recipient, reply_subject, len(final_body),
    )
    signed_bytes = sign_smime(mime_msg, smime_cert_path, smime_cert_password)
    logger.debug("Messaggio firmato S/MIME: %d byte", len(signed_bytes))

    logger.info(
        "Invio risposta via Graph API: destinatario=%s, oggetto='%s', mime_message=%s",
        to_recipient, reply_subject, "sì" if signed_bytes else "no",
    )
    client.send_mail(
        subject=reply_subject,
        body_html=final_body,
        to_recipients=[to_recipient],
        in_reply_to=internet_message_id,
        references=internet_message_id,
        mime_message=signed_bytes,
    )

    db.mark_processed(internet_message_id, conversation_id, subject, primary_intent, "sent")
    client.mark_read(message_id)

    logger.info("Risposta inviata a %s per %s", to_recipient, subject)
    return "sent"


def run_once(
    auth,
    classifier: NVIDIAClassifier,
    db: ProcessedDB,
    signature_html: str,
    original_leak_subject: str | None = None,
    original_leak_body: str | None = None,
) -> None:
    """Esegue un singolo ciclo di polling."""
    import os

    token = auth.get_access_token()
    if not token:
        logger.error("Impossibile ottenere access token M365.")
        return

    client = M365Client(access_token=token, username=config.M365_USERNAME)

    from_addr = config.M365_USERNAME

    try:
        messages = client.list_unread(
            lookback_hours=config.M365_LOOKBACK_HOURS,
            top=50,
            folder=config.M365_INBOX_FOLDER,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Errore recupero email: %s", exc)
        return

    logger.info(
        "Trovate %d email non lette nella casella %s",
        len(messages),
        config.M365_USERNAME,
    )
    for i, msg in enumerate(messages):
        logger.debug(
            "Email #%d: id=%s, oggetto='%s', mittente=%s, ricevuta=%s",
            i + 1, msg.get("id"), msg.get("subject"), extract_sender(msg), msg.get("receivedDateTime"),
        )

    for msg in messages:
        try:
            process_email(
                msg=msg,
                client=client,
                classifier=classifier,
                db=db,
                signature_html=signature_html,
                smime_cert_path=config.SMIME_CERT_PATH,
                smime_cert_password=config.SMIME_CERT_PASSWORD,
                from_addr=from_addr,
                original_leak_subject=original_leak_subject,
                original_leak_body=original_leak_body,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Errore processando email %s: %s", msg.get("id"), exc)


def run_test_mode(
    auth,
    classifier: NVIDIAClassifier,
    db: ProcessedDB,
    signature_html: str,
    numero: int,
    original_leak_subject: str | None = None,
    original_leak_body: str | None = None,
) -> None:
    """Modalità test: legge l'email NUMERO (1 = più recente) e processa tutto,
    inviando la risposta a salvatoreansani@gmail.com."""
    import os

    token = auth.get_access_token()
    if not token:
        logger.error("Impossibile ottenere access token M365.")
        return

    client = M365Client(access_token=token, username=config.M365_USERNAME)
    from_addr = config.M365_USERNAME

    try:
        messages = client.list_messages(
            top=max(numero, 10),
            folder=config.M365_INBOX_FOLDER,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Errore recupero email: %s", exc)
        return

    if not messages:
        logger.warning("Nessuna email trovata nella casella.")
        return

    if numero < 1 or numero > len(messages):
        logger.error(
            "Email #%d non disponibile. Ne sono presenti %d (dalla più recente).",
            numero, len(messages),
        )
        return

    msg = messages[numero - 1]
    subject = msg.get("subject", "(nessun oggetto)")
    sender = extract_sender(msg)
    received = msg.get("receivedDateTime", "sconosciuta")

    logger.info(
        "MODALITÀ TEST: elaborando email #%d (più recente=1): id=%s, mittente=%s, oggetto='%s', ricevuta=%s",
        numero, msg.get("id"), sender, subject, received,
    )
    logger.debug("Contenuto email #%d: %s", numero, extract_body(msg)[:500])

    try:
        status = process_email(
            msg=msg,
            client=client,
            classifier=classifier,
            db=db,
            signature_html=signature_html,
            smime_cert_path=config.SMIME_CERT_PATH,
            smime_cert_password=config.SMIME_CERT_PASSWORD,
            from_addr=from_addr,
            override_to=TEST_RECIPIENT,
            skip_already_processed=False,
            original_leak_subject=original_leak_subject,
            original_leak_body=original_leak_body,
        )
        logger.info("MODALITÀ TEST completata. Stato: %s", status)
    except Exception as exc:  # noqa: BLE001
        logger.error("Errore processando email #%d in modalità test: %s", numero, exc)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Data Leak Reply Manager - Gestione risposte automatiche a notifiche di data leak"
    )
    parser.add_argument(
        "--test",
        type=int,
        metavar="NUMERO",
        help="Modalità test: elabora l'email NUMERO (1 = più recente) e invia la risposta a salvatoreansani@gmail.com",
    )
    args = parser.parse_args()

    setup_logging(level=config.LOG_LEVEL, log_file=config.LOG_FILE)

    auth = build_auth_from_env()

    classifier = NVIDIAClassifier(
        api_key=config.NVIDIA_API_KEY,
        endpoint_url=config.NVIDIA_ENDPOINT_URL,
        model=config.NVIDIA_MODEL,
        timeout=config.NVIDIA_TIMEOUT_SECONDS,
    )

    db = ProcessedDB(config.DB_PATH)
    signature_html = load_signature_html(config.SIGNATURE_HTML_PATH)

    if args.test is not None:
        run_test_mode(
            auth=auth,
            classifier=classifier,
            db=db,
            signature_html=signature_html,
            numero=args.test,
        )
        return

    poll_interval = config.M365_POLL_INTERVAL_SECONDS

    import time

    logger.info("Avvio orchestrato data-leak-reply. Poll ogni %ds", poll_interval)
    while True:
        try:
            run_once(auth, classifier, db, signature_html)
        except KeyboardInterrupt:
            logger.info("Interruzione da tastiera, uscita.")
            break
        except Exception as exc:  # noqa: BLE001
            logger.error("Errore nel ciclo principale: %s", exc)
        time.sleep(poll_interval)
if __name__ == "__main__":
    main()