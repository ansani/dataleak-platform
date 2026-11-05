"""Authenticazione M365 tramite MSAL (client credentials flow - app-only).

Nessun login interattivo richiesto: il token viene ottenuto automaticamente
tramite client secret e riutilizzato dalla cache.
"""

from __future__ import annotations

import logging
from typing import Any

import msal

logger = logging.getLogger(__name__)

TOKEN_CACHE_FILE = "./db/token_cache.json"


class M365Auth:
    """Gestisce l'autenticazione OAuth2 verso Microsoft Graph tramite client credentials."""

    def __init__(
        self,
        client_id: str,
        tenant_id: str,
        client_secret: str,
        authority: str | None = None,
        scopes: list[str] | None = None,
    ) -> None:
        self.client_id = client_id
        self.tenant_id = tenant_id
        self.client_secret = client_secret
        self.authority = authority or f"https://login.microsoftonline.com/{tenant_id}"
        self.scopes = scopes or ["https://graph.microsoft.com/.default"]

        self.app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=self.authority,
        )

        self._load_cache()

    def _load_cache(self) -> None:
        try:
            import os

            if os.path.exists(TOKEN_CACHE_FILE):
                self.app.token_cache.deserialize(
                    open(TOKEN_CACHE_FILE, "r", encoding="utf-8").read()
                )
                logger.info("Token cache caricata da %s", TOKEN_CACHE_FILE)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Impossibile caricare token cache: %s", exc)

    def _save_cache(self) -> None:
        try:
            import os

            os.makedirs(os.path.dirname(TOKEN_CACHE_FILE), exist_ok=True)
            cache = self.app.token_cache
            if hasattr(cache, "has_state_changed") and cache.has_state_changed:
                with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
                    f.write(cache.serialize())
                logger.debug("Token cache salvata in %s", TOKEN_CACHE_FILE)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Impossibile salvare token cache: %s", exc)

    def acquire_token_silent(self) -> dict[str, Any] | None:
        """Tenta di ottenere un token silenziosamente dalla cache."""
        result = self.app.acquire_token_silent(self.scopes, account=None)
        if result and "access_token" in result:
            token_preview = result["access_token"][:20] + "..."
            logger.info("Token recuperato dalla cache (anteprima: %s)", token_preview)
            self._save_cache()
            return result
        logger.debug("Nessun token valido in cache.")
        return None

    def acquire_token_for_client(self) -> dict[str, Any] | None:
        """Ottiene un token tramite client credentials flow (app-only, nessun utente)."""
        logger.info("Richiesta token client credentials a %s", self.authority)
        result = self.app.acquire_token_for_client(scopes=self.scopes)
        if "access_token" in result:
            token_preview = result["access_token"][:20] + "..."
            logger.info("Token client credentials ottenuto (anteprima: %s)", token_preview)
            self._save_cache()
            return result

        logger.error(
            "Autenticazione client credentials fallita: %s",
            result.get("error_description", result.get("error", "sconosciuto")),
        )
        return None

    def get_access_token(self) -> str | None:
        """Restituisce un access token valido."""
        token = self.acquire_token_silent()
        if token:
            return token["access_token"]

        token = self.acquire_token_for_client()
        if token:
            return token["access_token"]
        return None


def build_auth_from_env() -> M365Auth:
    """Costruisce l'oggetto auth leggendo le variabili d'ambiente."""
    import os

    from dotenv import load_dotenv

    load_dotenv()

    return M365Auth(
        client_id=os.getenv("M365_CLIENT_ID", ""),
        tenant_id=os.getenv("M365_TENANT_ID", ""),
        client_secret=os.getenv("M365_CLIENT_SECRET", ""),
        scopes=[
            "https://graph.microsoft.com/.default",
        ],
    )