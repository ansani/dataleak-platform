"""Client Microsoft Graph per operazioni su posta."""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class M365Client:
    """Wrapper attorno a Microsoft Graph API per lettura e invio email."""

    def __init__(self, access_token: str, username: str) -> None:
        self.access_token = access_token
        self.username = username
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
            }
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def list_unread(
        self,
        lookback_hours: int = 24,
        top: int = 50,
        folder: str = "Inbox",
    ) -> list[dict[str, Any]]:
        """Recupera email non lette ricevute nelle ultime N ore."""
        url = f"{GRAPH_BASE}/users/{self.username}/mailFolders/{folder}/messages"

        params: dict[str, Any] = {
            "$top": top,
            "$orderby": "receivedDateTime desc",
            "$select": "id,internetMessageId,subject,receivedDateTime,isRead,from,toRecipients,body,bodyPreview,conversationId",
            "$filter": f"isRead eq false and receivedDateTime ge {self._iso(lookback_hours)}",
        }

        self._log_request("GET", url, params=params)
        resp = self.session.get(url, params=params, headers=self._headers(), timeout=30)
        logger.debug("Graph API risposta %d per GET %s", resp.status_code, url)
        resp.raise_for_status()
        return resp.json().get("value", [])

    @staticmethod
    def _iso(hours: int) -> str:
        from datetime import datetime, timedelta, timezone

        dt = datetime.now(timezone.utc) - timedelta(hours=hours)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def get_message(self, message_id: str) -> dict[str, Any]:
        """Recupera un singolo messaggio completo."""
        url = f"{GRAPH_BASE}/users/{self.username}/messages/{message_id}"
        self._log_request("GET", url)
        resp = self.session.get(url, headers=self._headers(), timeout=30)
        logger.debug("Graph API risposta %d per GET %s", resp.status_code, url)
        resp.raise_for_status()
        return resp.json()

    def list_messages(
        self,
        top: int = 50,
        folder: str = "Inbox",
    ) -> list[dict[str, Any]]:
        """Recupera gli ultimi N messaggi dalla cartella (indipendentemente dallo stato di lettura)."""
        url = f"{GRAPH_BASE}/users/{self.username}/mailFolders/{folder}/messages"

        params: dict[str, Any] = {
            "$top": top,
            "$orderby": "receivedDateTime desc",
            "$select": "id,internetMessageId,subject,receivedDateTime,isRead,from,toRecipients,body,bodyPreview,conversationId",
        }

        self._log_request("GET", url, params=params)
        resp = self.session.get(url, params=params, headers=self._headers(), timeout=30)
        logger.debug("Graph API risposta %d per GET %s", resp.status_code, url)
        resp.raise_for_status()
        return resp.json().get("value", [])

    def mark_read(self, message_id: str) -> None:
        """Segna un messaggio come letto."""
        url = f"{GRAPH_BASE}/users/{self.username}/messages/{message_id}"
        self._log_request("PATCH", url, body={"isRead": True})
        resp = self.session.patch(
            url, headers=self._headers(), json={"isRead": True}, timeout=30
        )
        logger.debug("Graph API risposta %d per PATCH %s", resp.status_code, url)
        resp.raise_for_status()

    def _log_request(self, method: str, url: str, params: dict | None = None, body: dict | None = None) -> None:
        """Log dettagliato della richiesta in uscita."""
        logger.debug("Graph API %s %s", method.upper(), url)
        if params:
            logger.debug("  Parametri: %s", params)
        if body:
            safe_body = {k: v for k, v in body.items() if k not in ("message",)}
            if "message" in body:
                msg = body["message"]
                if "mimeContent" in msg:
                    mime_len = len(str(msg["mimeContent"]))
                    safe_body["message"] = {"mimeContent": f"<{mime_len} caratteri base64>"}
            logger.debug("  Corpo: %s", safe_body)

    def send_mail(
        self,
        subject: str,
        body_html: str,
        to_recipients: list[str],
        cc_recipients: list[str] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        mime_message: bytes | None = None,
    ) -> None:
        """Invia un'email tramite Microsoft Graph API.

        Se ``mime_message`` è fornito (es. messaggio già firmato S/MIME),
        il contenuto MIME completo viene inviato direttamente nel corpo della
        richiesta HTTP con Content-Type: text/plain, come documentato da
        Microsoft:
        https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0

        Altrimenti viene inviato un messaggio strutturato classico (JSON).
        """
        url = f"{GRAPH_BASE}/users/{self.username}/sendMail"

        if mime_message:
            import base64

            # Invio MIME firmato S/MIME: Graph API si aspetta il MIME completo
            # codificato in Base64 nel corpo della richiesta con
            # Content-Type: text/plain.
            # I destinatari (To) devono essere presenti come header esterni
            # nel MIME stesso (già inseriti da build_mime_message e preservati
            # da sign_smime).
            raw = base64.b64encode(mime_message)
            headers = dict(self._headers())
            headers["Content-Type"] = "text/plain"

            self._log_request("POST", url, body={"mimeContent": f"<{len(raw)} caratteri base64>"})
            resp = self.session.post(
                url, headers=headers, data=raw, timeout=30
            )
        else:
            payload: dict[str, Any] = {
                "Message": {
                    "Subject": subject,
                    "Body": {"ContentType": "HTML", "Content": body_html},
                    "ToRecipients": [
                        {"EmailAddress": {"Address": a}} for a in to_recipients
                    ],
                }
            }
            if cc_recipients:
                payload["Message"]["CcRecipients"] = [
                    {"EmailAddress": {"Address": a}} for a in cc_recipients
                ]
            if in_reply_to:
                payload["Message"]["InternetMessageHeaders"] = [
                    {"Name": "In-Reply-To", "Value": in_reply_to}
                ]
            if references:
                payload["Message"]["InternetMessageHeaders"].append(
                    {"Name": "References", "Value": references}
                )

            self._log_request("POST", url, body=payload)
            resp = self.session.post(
                url, headers=self._headers(), json=payload, timeout=30
            )

        if resp.status_code >= 400:
            logger.error(
                "Graph API errore %d per POST %s: %s",
                resp.status_code, url, resp.text[:2000],
            )
        else:
            logger.debug("Graph API risposta %d per POST %s", resp.status_code, url)
        resp.raise_for_status()
        logger.info("Email inviata a %s", ", ".join(to_recipients))