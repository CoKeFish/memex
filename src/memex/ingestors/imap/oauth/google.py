# mypy: disable-error-code="no-any-return, no-untyped-call"
"""Google OAuth2 provider for Gmail IMAP (XOAUTH2 SASL).

Initial authorization uses google-auth-oauthlib's Desktop App flow (opens a
browser, captures the redirect, exchanges the code for tokens). After that,
runs use the persisted refresh_token to mint fresh access tokens
automatically.

Token file is JSON (the format google-auth's `Credentials.to_json()`
produces). It contains the refresh_token, so its filesystem permissions
matter: the writer attempts to chmod 0600 on POSIX systems (Windows ignores).
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import ClassVar

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

from memex.ingestors.imap.oauth import OAuthError

# Gmail IMAP requires the full mail scope ("https://mail.google.com/").
# The narrower "gmail.readonly" scope only works for the REST Gmail API, not
# for IMAP XOAUTH2 authentication. memex's IMAP ingestor only issues read
# commands; the broader scope is a Google constraint, not a memex choice.
GMAIL_IMAP_SCOPES = ["https://mail.google.com/"]


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

        Opens the user's default browser to Google's consent page. Captures the
        redirect locally on a randomly-chosen port. Stores the resulting tokens
        to `token_path` in google-auth's JSON format.
        """
        cs_path = Path(client_secret_path)
        if not cs_path.exists():
            raise OAuthError(f"client_secret file not found: {cs_path}")

        flow = InstalledAppFlow.from_client_secrets_file(str(cs_path), GMAIL_IMAP_SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
        _save_credentials(creds, token_path)

    def get_access_token(
        self,
        *,
        token_path: str | Path,
    ) -> str:
        """Load credentials, refresh if needed, return the access_token string."""
        creds = _load_and_refresh(token_path)
        if not creds.token:
            raise OAuthError("no access_token after refresh")
        return creds.token


def _load_and_refresh(token_path: str | Path) -> Credentials:
    """Load credentials from disk and refresh them if needed.

    Persists the refreshed token back to disk so subsequent runs reuse the
    same access_token until it expires again.
    """
    path = Path(token_path)
    if not path.exists():
        raise OAuthError(
            f"OAuth token file not found at {path}. Run "
            f"`python -m memex.ingestors.imap.cli authorize --source-id N` first."
        )

    try:
        creds = Credentials.from_authorized_user_file(str(path), GMAIL_IMAP_SCOPES)
    except Exception as e:
        raise OAuthError(f"failed to load token file {path}: {e}") from e

    if creds.valid:
        return creds

    if not (creds.expired and creds.refresh_token):
        raise OAuthError(
            f"OAuth credentials at {path} are invalid and cannot be refreshed "
            "(missing refresh_token or unexpired but invalid). Re-run authorize."
        )

    try:
        creds.refresh(Request())
    except Exception as e:
        raise OAuthError(f"token refresh failed for {path}: {e}") from e

    _save_credentials(creds, path)
    return creds


def _save_credentials(creds: Credentials, token_path: str | Path) -> None:
    path = Path(token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    with contextlib.suppress(OSError):
        # Best-effort tighten on POSIX; Windows raises and we ignore.
        path.chmod(0o600)
