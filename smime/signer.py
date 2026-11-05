"""Firma S/MIME dei messaggi di risposta."""

from __future__ import annotations

import logging
import subprocess
import tempfile
from copy import deepcopy
from email import policy
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path

from utils import html_to_text

logger = logging.getLogger(__name__)


def load_signature_html(path: str) -> str:
    """Legge il file HTML della firma da disco."""
    p = Path(path)
    if not p.exists():
        logger.warning("File firma HTML non trovato: %s", path)
        return ""
    return p.read_text(encoding="utf-8")


def build_mime_message(
    subject: str,
    body_html: str,
    to: str,
    from_addr: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> MIMEMultipart:
    """Costruisce un messaggio MIME multipart/alternative.

    Gli header From/To/Subject vengono impostati qui e saranno preservati
    come header esterni del multipart/signed prodotto da openssl.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    # Corpo testo semplice (riusa html_to_text da utils per coerenza DRY)
    text_plain = html_to_text(body_html)
    text_part = MIMEText(text_plain, "plain", "utf-8")
    msg.attach(text_part)

    # Corpo HTML
    html_part = MIMEText(body_html, "html", "utf-8")
    msg.attach(html_part)

    return msg


def sign_smime(
    mime_message: MIMEMultipart,
    p12_path: str,
    p12_password: str,
) -> bytes:
    """Firma l'entità MIME completa e aggiunge gli header di trasporto esterni.

    Il contenuto firmato conserva Content-Type e boundary; OpenSSL produce
    il multipart/signed in formato SMIME. I byte firmati non vengono
    riserializzati dopo la firma.
    """
    p12 = Path(p12_path)
    if not p12.exists():
        raise FileNotFoundError(f"Certificato S/MIME non trovato: {p12_path}")

    logger.info("Inizio firma S/MIME con certificato: %s", p12_path)

    outer = EmailMessage(policy=policy.SMTP)
    entity = deepcopy(mime_message)
    for key in list(entity.keys()):
        if key.lower() != "mime-version" and not key.lower().startswith("content-"):
            for value in entity.get_all(key, []):
                outer[key] = value
            del entity[key]

    # Firma anche gli header MIME: senza Content-Type il multipart/alternative
    # e i suoi boundary non possono essere interpretati dal destinatario.
    body_part = entity.as_bytes(policy=policy.SMTP)

    with tempfile.NamedTemporaryFile(suffix=".body", delete=False, mode="wb") as f:
        f.write(body_part)
        body_path = f.name

    logger.debug("Corpo del messaggio scritto in %s (%d byte)", body_path, len(body_part))

    # Esporta chiave privata e certificato dal p12
    with tempfile.NamedTemporaryFile(suffix=".key", delete=False) as key_file:
        key_path = key_file.name
    with tempfile.NamedTemporaryFile(suffix=".cert", delete=False) as cert_file:
        cert_path = cert_file.name

    try:
        # Estrai chiave privata
        result_key = subprocess.run(
            [
                "openssl", "pkcs12", "-in", str(p12),
                "-nocerts", "-nodes",
                "-passin", f"pass:{p12_password}",
                "-out", key_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result_key.returncode != 0:
            logger.error("Estrazione chiave privata fallita: %s", result_key.stderr)
            raise RuntimeError(f"Estrazione chiave privata fallita: {result_key.stderr}")
        logger.debug("Chiave privata estratta")

        # Estrai certificato (+ eventuali CA)
        result_cert = subprocess.run(
            [
                "openssl", "pkcs12", "-in", str(p12),
                "-nokeys",
                "-passin", f"pass:{p12_password}",
                "-out", cert_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result_cert.returncode != 0:
            logger.error("Estrazione certificato fallita: %s", result_cert.stderr)
            raise RuntimeError(f"Estrazione certificato fallita: {result_cert.stderr}")
        logger.debug("Certificato estratto")

        # Firma in formato SMIME: output completo multipart/signed
        # con il corpo firmato come prima parte e la firma come seconda
        signed_path = body_path + ".signed"
        result_sign = subprocess.run(
            [
                "openssl", "smime", "-sign",
                "-in", body_path,
                "-signer", cert_path,
                "-inkey", key_path,
                "-outform", "SMIME",
                "-binary", "-crlfeol",
                "-md", "sha256",
                "-out", signed_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result_sign.returncode != 0:
            logger.error("Firma S/MIME fallita: %s", result_sign.stderr)
            raise RuntimeError(f"Firma S/MIME fallita: {result_sign.stderr}")
        logger.debug("Firma S/MIME completata")

        signed_bytes = Path(signed_path).read_bytes()
        # Alcune versioni OpenSSL usano LF nel base64 della firma anche con
        # -crlfeol. L'entità firmata è già CRLF: questa normalizzazione
        # modifica solo l'involucro, senza cambiare i byte coperti dalla firma.
        signed_bytes = signed_bytes.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        # Rimuove solo la riga vuota finale degli header di trasporto.
        # Non decodificare o riserializzare il contenuto firmato.
        header_block = outer.as_bytes(policy=policy.SMTP).split(b"\r\n\r\n", 1)[0]
        final_bytes = header_block + b"\r\n" + signed_bytes

        logger.info(
            "Messaggio S/MIME multipart/signed pronto (%d byte)",
            len(final_bytes),
        )
        return final_bytes

    finally:
        for p in [body_path, key_path, cert_path]:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        signed_path = body_path + ".signed"
        try:
            Path(signed_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass