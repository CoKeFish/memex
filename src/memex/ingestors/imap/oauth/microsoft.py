"""Microsoft OAuth2 provider for Outlook 365 IMAP — STUB.

Not implemented yet. The registry includes this provider so `resolve("microsoft")`
returns an instance, but any method call raises NotImplementedError with a clear
message explaining what's missing.

To complete this provider:
1. Add `msal` (Microsoft Authentication Library) to pyproject.toml.
2. Register an app in Azure Portal → grab tenant_id + client_id (+ client_secret
   if confidential, or use device-flow if public client).
3. Replace the NotImplementedError calls below with the actual MSAL flow.
4. Refresh logic similar to Google's `_load_and_refresh`.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar


class MicrosoftOAuthProvider:
    """Stub provider. Exists in the registry, raises NotImplementedError on use."""

    name: ClassVar[str] = "microsoft"

    def authorize_interactive(
        self,
        *,
        client_secret_path: str | Path,
        token_path: str | Path,
    ) -> None:
        raise NotImplementedError(
            "MicrosoftOAuthProvider no implementado. Para soportar Outlook OAuth2: "
            "agregar `msal` a pyproject.toml, registrar app en Azure Portal, "
            "implementar este provider."
        )

    def get_access_token(
        self,
        *,
        token_path: str | Path,
    ) -> str:
        raise NotImplementedError("MicrosoftOAuthProvider.get_access_token no implementado.")
