"""Google OAuth2 provider for Gmail IMAP (XOAUTH2 SASL).

Thin adapter sobre el helper NEUTRAL `memex.google_oauth` (compartido con el módulo de
calendario): el glue genérico de google-auth (flow Desktop App, refresh, persistencia 0600) vive
allá; este archivo solo lo liga al Protocol `OAuthProvider` del ingestor y al scope de Gmail, y
traduce `GoogleOAuthError` → `OAuthError` (la base que atrapan los callers de IMAP).

Decisión 6 (2026-05-30): la autorización pide el conjunto COMPLETO de scopes de memex en un solo
consentimiento (Gmail full + Calendar), así que el token consolidado sirve también acá; Gmail es
un subset del token, por eso el refresh con `GMAIL_IMAP_SCOPES` funciona.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from memex import google_oauth
from memex.ingestors.imap.oauth import OAuthError

# Gmail IMAP requiere el scope full mail (subset del token consolidado de la decisión 6).
GMAIL_IMAP_SCOPES = [google_oauth.GMAIL_SCOPE]


class GoogleOAuthProvider:
    """OAuth provider for Gmail. Implements memex.ingestors.imap.oauth.OAuthProvider."""

    name: ClassVar[str] = "google"

    def authorize_interactive(
        self,
        *,
        client_secret_path: str | Path,
        token_path: str | Path,
    ) -> None:
        """Run the interactive OAuth flow and persist tokens to `token_path`.

        Pide el set COMPLETO de scopes de memex (decisión 6) en una sola pantalla de
        consentimiento, así un único token sirve para IMAP y para el módulo de calendario.
        """
        try:
            google_oauth.authorize_interactive(
                client_secret_path=client_secret_path, token_path=token_path
            )
        except google_oauth.GoogleOAuthError as e:
            raise OAuthError(str(e)) from e

    def get_access_token(
        self,
        *,
        token_path: str | Path,
    ) -> str:
        """Load credentials, refresh if needed, return the access_token string."""
        try:
            return google_oauth.get_access_token(token_path=token_path, scopes=GMAIL_IMAP_SCOPES)
        except google_oauth.GoogleOAuthError as e:
            raise OAuthError(str(e)) from e
