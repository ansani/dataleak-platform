"""Template di risposta per le classificazioni intent del classificatore NVIDIA.

Ogni template utilizza placeholder nel formato {{variable_name}}.
I placeholder non disponibili vengono sostituiti con una stringa esplicita
di indisponibilità per evitare di inviare informazioni ipotetiche.

I template NON includono frasi di ringraziamento o chiusura "S":
vengono aggiunte separatamente dopo il passaggio attraverso il modello NVIDIA.
"""

from __future__ import annotations

import re
from typing import Any

# Placeholder da usare quando un valore non è disponibile
PLACEHOLDER_MISSING = "[non disponibile]"


# ---------------------------------------------------------------------------
# Template di risposta (uno per categoria intent)
# ---------------------------------------------------------------------------

TEMPLATES: dict[str, str] = {
    "insulti": """Hi {{recipient_name}},

I received your message. My notification was sent in good faith to inform you that your email appeared in a stolen-credentials dataset.

If you prefer not to receive further notifications, please let me know.

S""",

    "generica": """Hi {{recipient_name}},

An infostealer is malware that collects sensitive information from infected devices, such as passwords, cookies, and authentication tokens.

Your email appeared in a related dataset, but this does not mean your device is still infected.

Scan your devices with updated security software, change important passwords from a trusted device, enable multi-factor authentication, and revoke active sessions where possible.

S""",

    "lista_URI": """Hi {{recipient_name}},

URLs and services associated with your email:

{{uri_list}}

Change passwords for these services, prioritizing email and financial accounts. Revoke active sessions and enable multi-factor authentication wherever possible.

For security, I do not share passwords, authentication tokens, or session cookies.

S""",

    "device": """Hi {{recipient_name}},

Device details from the infostealer dataset:

- Device: {{device_name}}
- OS: {{device_os}}
- Log timestamp: {{log_timestamp}}

These details help identify the device but do not confirm whether it is currently infected.

Scan the device with updated security software and review suspicious applications or downloads.

S""",

    "group_id": """Hi {{recipient_name}},

Your email was identified in material published through a Telegram group or channel:

{{telegram_group_id}}

This identifies where the material was observed, not necessarily where the original compromise occurred.

Avoid downloading or executing files distributed through such groups, as they may contain malicious software.

S""",

    "email_found": """Hi {{recipient_name}},

I identified {{affected_email}} while analyzing datasets associated with stolen credentials and infostealer activity.

Source: {{source_name}}

These datasets may contain information collected from infected devices and subsequently distributed by third parties.

My research involves identifying potentially affected users and notifying them so they can take appropriate security measures.

I did not obtain your information by accessing your personal accounts or devices.

S""",

    "verifica_autenticita": """Hi {{recipient_name}},

Your caution is understandable. Unexpected security notifications should always be treated carefully.

My name is {{researcher_name}}, and I conduct independent security research through KiruruSec:

{{website_url}}

I do not ask for payment, passwords, verification codes, or remote access to your devices.

Verify my sender address and research my public professional profile before deciding whether to trust this notification.

My purpose is to alert potentially affected individuals so they can protect their accounts.

S""",

    "evidenze": """Hi {{recipient_name}},

Available details:

{{evidence_summary}}

Email: {{affected_email}}
Log timestamp: {{log_timestamp}}

These findings originate from data associated with infostealer activity. However, the available evidence does not necessarily establish when the original infection occurred or whether the affected device remains compromised.

For security reasons, I do not share plaintext passwords, session cookies, authentication tokens, or other sensitive data that could facilitate unauthorized access.

S""",

    "data_infezione": """Hi {{recipient_name}},

The timestamp available in the dataset associated with your email address is:

{{log_timestamp}}

This timestamp may indicate when the information was collected, recorded, or processed, depending on the source.

It does not necessarily represent the exact time when the malware was installed on your device.

If possible, review downloads, recently installed applications, and security alerts around the relevant period.

S""",

    "rimozione_dati": """Hi {{recipient_name}},

Once information has been stolen and distributed, it may be impossible to guarantee its complete removal from the internet.

Even if a particular publication or Telegram channel is removed, copies of the data may continue to exist elsewhere.

You can report the publication to the hosting platform and request its removal. However, this does not guarantee that all copies will disappear.

The most important steps are to change potentially exposed passwords, revoke active sessions, enable multi-factor authentication, and ensure that your devices are no longer compromised.

I cannot guarantee the deletion of data from systems or platforms outside my control.

S""",

    "bonifica_device": """Hi {{recipient_name}},

If your device may have been infected by an infostealer:

1. Disconnect the suspected device from the internet if you believe the infection is still active.
2. Use a trusted, updated antivirus or endpoint security solution to perform a complete scan.
3. Review recently installed applications, especially cracked software or downloads from untrusted sources.
4. Consider reinstalling the operating system from trusted installation media if the device's integrity cannot be established.
5. From a separate, trusted device, change important passwords and revoke active sessions.
6. Enable multi-factor authentication wherever possible.

The malware family reported in the available data is:

{{malware_name}}

Antivirus scanning alone cannot always guarantee complete removal of a compromise.

S""",

    "messa_in_sicurezza": """Hi {{recipient_name}},

If your credentials were exposed through an infostealer, changing passwords is an important first step, but it may not be sufficient.

Some infostealers also collect browser cookies and authentication tokens, which may allow attackers to access existing sessions.

From a trusted device:

1. Change your primary email account password.
2. Change the passwords of potentially affected services, using a unique password for each account.
3. Revoke active sessions and sign out of all devices where supported.
4. Enable multi-factor authentication, preferably using passkeys or phishing-resistant methods where available.
5. Review account recovery options and remove any unfamiliar devices or authentication methods.
6. Verify that the suspected infected device has been secured.

These actions can substantially reduce the risk of continued unauthorized access.

S""",

    "recupero_account": """Hi {{recipient_name}},

If an attacker has taken control of your account, use the official account recovery process provided by the service.

Contact the provider through its official website and explain that your account may have been compromised following an infostealer infection.

Secure your email account and other linked services, as attackers sometimes use compromised recovery methods to regain access.

I cannot directly restore accounts or override a service provider's security procedures.

S""",

    "frode_finanziaria": """Hi {{recipient_name}},

If you have noticed unauthorized transactions or suspect that your banking or payment information has been compromised, contact your bank or payment provider immediately using its official telephone number or application.

Explain that your credentials may have been exposed and request appropriate protective measures, which may include freezing affected cards or accounts and disputing unauthorized transactions.

Secure your primary email account, change compromised passwords from a trusted device, and revoke suspicious sessions.

Do not share banking credentials, card details, verification codes, or recovery information with anyone contacting you unexpectedly.

I can provide information about the exposure I identified, but I cannot investigate financial transactions or reverse unauthorized payments.

S""",

    "richiesta_assistenza": """Hi {{recipient_name}},

What would you like to understand or what kind of assistance do you need?

I may be able to help you understand the available information about the exposure, identify potentially affected services, or suggest general steps to secure your accounts and devices.

Please do not send passwords, verification codes, or other sensitive authentication information.

S""",

    "ringraziamento": """Hi {{recipient_name}},

Stay safe.

S""",

    "risposta_automatica": """Hi {{recipient_name}},

This mailbox may not be monitored.

The original message was sent as a security notification concerning potentially exposed credentials associated with {{affected_email}}.

No immediate response is required.

S""",

    "rifiuto_contatto": """Hi {{recipient_name}},

I acknowledge your request not to receive further security notifications from KiruruSec.

Your email address, {{affected_email}}, will be excluded from future notifications managed by my system.

I apologize for any inconvenience caused by the previous communication.

S""",

    "altro": """Hi {{recipient_name}},

I have received your inquiry regarding {{affected_email}}.

Could you please provide more information about your request so I can better understand what you need?

Please avoid sharing passwords, authentication tokens, or other sensitive information.

S""",

    "non_classificabile": """Hi {{recipient_name}},

I was unable to understand the content of your reply.

Could you please resend your question or briefly explain what information you need regarding the security notification?

I will do my best to provide clarification.

S""",

    "richiede_operatore": """Hi {{recipient_name}},

I have received your message regarding the potential exposure of your information.

Your request requires further review, as I want to avoid providing incomplete or inaccurate information.

Reference: {{support_reference}}

In the meantime, I recommend taking appropriate precautions to secure your accounts and devices.

S""",
}

# ---------------------------------------------------------------------------
# Mappatura categoria -> template_id e handling
# ---------------------------------------------------------------------------

TEMPLATE_CONFIG: dict[str, dict[str, str]] = {
    "insulti": {"template_id": "68101", "handling": "review"},
    "generica": {"template_id": "68102", "handling": "auto"},
    "lista_URI": {"template_id": "68103", "handling": "lookup"},
    "device": {"template_id": "68104", "handling": "lookup"},
    "group_id": {"template_id": "68105", "handling": "lookup"},
    "email_found": {"template_id": "68106", "handling": "lookup"},
    "verifica_autenticita": {"template_id": "68107", "handling": "auto"},
    "evidenze": {"template_id": "68108", "handling": "lookup"},
    "data_infezione": {"template_id": "68109", "handling": "lookup"},
    "rimozione_dati": {"template_id": "68110", "handling": "review"},
    "bonifica_device": {"template_id": "68111", "handling": "auto"},
    "messa_in_sicurezza": {"template_id": "68112", "handling": "auto"},
    "recupero_account": {"template_id": "68113", "handling": "review"},
    "frode_finanziaria": {"template_id": "68114", "handling": "review"},
    "richiesta_assistenza": {"template_id": "68115", "handling": "auto"},
    "ringraziamento": {"template_id": "68116", "handling": "optional"},
    "risposta_automatica": {"template_id": "68117", "handling": "none"},
    "rifiuto_contatto": {"template_id": "68118", "handling": "suppress"},
    "altro": {"template_id": "68119", "handling": "review"},
    "non_classificabile": {"template_id": "68120", "handling": "review"},
    "richiede_operatore": {"template_id": "68121", "handling": "review"},
}

# Mappatura intent -> template_id per comodità
INTENT_TO_TEMPLATE_ID: dict[str, str] = {
    intent: cfg["template_id"] for intent, cfg in TEMPLATE_CONFIG.items()
}


def render_template(template_text: str, values: dict[str, str]) -> str:
    """Sostituisce i placeholder {{nome}} con i valori forniti.

    I placeholder non presenti in `values` vengono sostituiti con
    PLACEHOLDER_MISSING per evitare di inviare informazioni ipotetiche.
    """

    def replacer(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values and values[key] is not None and values[key] != "":
            return str(values[key])
        return PLACEHOLDER_MISSING

    return re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}").sub(replacer, template_text)


def get_template_for_intent(intent: str) -> str | None:
    """Restituisce il testo del template associato a un intent, se esiste."""
    return TEMPLATES.get(intent)


def get_template_config(intent: str) -> dict[str, str]:
    """Restituisce la configurazione (template_id, handling) per un intent."""
    return TEMPLATE_CONFIG.get(intent, {"template_id": "", "handling": "review"})