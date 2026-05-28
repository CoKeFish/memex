"""Plugin de cliente local: lee Inbox desde Outlook desktop vía COM/MAPI.

Pattern: misma idea que `rodion-corp-bridge.sources.outlook` — se apoya en la
sesión de Outlook ya autenticada en este Windows. No pide credenciales
propias, no expone secretos, no necesita IMAP (que de todos modos suele estar
deshabilitado en tenants corporativos de Microsoft 365).

Produce `SourceRecord` con `EmailPayload` (mismo shape que el IMAP source),
para que el procesador downstream trate emails Outlook+IMAP uniformemente.

Requiere:
- Windows con Outlook desktop instalado y configurado con la cuenta deseada.
- `pywin32` instalado en el venv del cliente local.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from memex.core.payloads import Address, EmailPayload
from memex.core.source import Source, SourceRecord
from memex.logging import get_logger

# ---------------- LocalPlugin contract -----------------------------------------

name = "outlook-desktop"
version = "0.1.0"
source_type = "outlook"
default_schedule = "PT5M"


def build_source(local_config: Mapping[str, Any]) -> Source:
    return _OutlookSource(
        account=local_config.get("account"),
        max_items_per_run=int(local_config.get("max_items_per_run", 100)),
        since_days=int(local_config.get("since_days", 7)),
        max_body_bytes=int(local_config.get("max_body_bytes", 524288)),
    )


def validate_requirements(local_config: Mapping[str, Any]) -> list:
    from memex_local_client.protocol import Problem

    problems: list[Problem] = []
    accs: list[str] = []
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError as e:
        problems.append(
            Problem(
                "error",
                "pywin32-missing",
                f"pywin32 no instalado en el venv del cliente local: {e}",
            )
        )
        return problems

    try:
        pythoncom.CoInitialize()
        app = win32com.client.Dispatch("Outlook.Application")
        ns = app.GetNamespace("MAPI")
        accs = [acc.SmtpAddress for acc in ns.Accounts]
        if not accs:
            problems.append(
                Problem("error", "no-outlook-accounts", "Outlook no tiene cuentas configuradas")
            )
    except Exception as e:
        problems.append(
            Problem(
                "error",
                "outlook-unavailable",
                f"no se pudo abrir Outlook COM: {type(e).__name__}: {e}",
            )
        )
        return problems
    finally:
        with suppress(Exception):
            pythoncom.CoUninitialize()

    account = local_config.get("account")
    if account and isinstance(account, str) and account not in accs:
        problems.append(
            Problem(
                "error",
                "account-not-found",
                f"cuenta {account!r} no está en Outlook. Disponibles: {accs}",
            )
        )
    return problems


# ---------------- internals ----------------------------------------------------

OL_FOLDER_INBOX = 6
OL_MAIL_ITEM_CLASS = 43

PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
PR_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
PR_TRANSPORT_MESSAGE_HEADERS = "http://schemas.microsoft.com/mapi/proptag/0x007D001E"


def _lazy_com() -> tuple[Any, Any]:
    import pythoncom  # type: ignore
    import win32com.client  # type: ignore

    return pythoncom, win32com.client


def _safe_get(obj: Any, attr: str, default: Any = None) -> Any:
    try:
        return getattr(obj, attr)
    except Exception:
        return default


def _safe_prop(item: Any, tag: str) -> str:
    try:
        v = item.PropertyAccessor.GetProperty(tag)
        return str(v) if v is not None else ""
    except Exception:
        return ""


def _resolve_smtp(item: Any) -> str:
    sender_type = _safe_get(item, "SenderEmailType", "")
    addr = _safe_get(item, "SenderEmailAddress", "") or ""
    if sender_type == "EX":
        try:
            ae = item.Sender
            if ae is not None:
                ex = ae.GetExchangeUser()
                if ex and ex.PrimarySmtpAddress:
                    return str(ex.PrimarySmtpAddress)
        except Exception:
            pass
        try:
            pa = item.Sender.PropertyAccessor
            smtp = pa.GetProperty(PR_SMTP_ADDRESS)
            if smtp:
                return str(smtp)
        except Exception:
            pass
    return addr or ""


def _parse_headers(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    out: dict[str, str] = {}
    current: str | None = None
    for line in raw.splitlines():
        if not line:
            continue
        if line[0] in (" ", "\t") and current:
            out[current] = out[current] + " " + line.strip()
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower()
            out[key] = val.strip()
            current = key
    return out


def _received_dt(item: Any) -> datetime:
    rt = _safe_get(item, "ReceivedTime")
    if rt is None:
        return datetime.now(UTC)
    try:
        dt = rt if isinstance(rt, datetime) else datetime.fromisoformat(str(rt))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return datetime.now(UTC)


def _body_text(item: Any, max_bytes: int) -> tuple[str, bool]:
    body = _safe_get(item, "Body", "") or ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    enc = body.encode("utf-8", errors="replace")
    if len(enc) > max_bytes:
        return enc[:max_bytes].decode("utf-8", errors="replace") + "\n\n[...truncated]", True
    return body, False


def _split_addresses(s: str) -> list[Address]:
    """Parser muy permisivo del campo To/CC de Outlook ('Name <email>; Name2 <email2>')."""
    if not s:
        return []
    out: list[Address] = []
    for part in s.split(";"):
        p = part.strip()
        if not p:
            continue
        if "<" in p and ">" in p:
            name = p.split("<", 1)[0].strip().strip('"').strip()
            email = p.split("<", 1)[1].split(">", 1)[0].strip()
            if email:
                out.append(Address(email=email, name=name or None))
        else:
            if "@" in p:
                out.append(Address(email=p))
    return out


@dataclass
class _OutlookSource:
    """Implementa `memex.core.source.Source` leyendo de Outlook desktop."""

    account: str | None
    max_items_per_run: int
    since_days: int
    max_body_bytes: int
    type: ClassVar[str] = "outlook"

    def fetch(self, checkpoint: dict[str, Any] | None) -> Iterable[SourceRecord]:
        log = get_logger("memex_local.outlook").bind(account=self.account or "default")
        pythoncom, win32com = _lazy_com()
        pythoncom.CoInitialize()
        try:
            app = win32com.Dispatch("Outlook.Application")
            ns = app.GetNamespace("MAPI")
            inbox = self._resolve_inbox(ns)

            since = self._compute_since(checkpoint)
            log.info(
                "outlook.fetch_start", since=since.isoformat(), max_items=self.max_items_per_run
            )

            items = inbox.Items
            try:
                items.Sort("[ReceivedTime]", False)  # ascending
            except Exception as e:
                log.warning("outlook.sort_failed", error=str(e))

            count = 0
            skipped_old = 0
            for item in items:
                if count >= self.max_items_per_run:
                    break
                try:
                    rec = self._to_record(item, inbox.Name)
                except Exception as e:
                    log.warning("outlook.item_failed", error=str(e))
                    continue
                if rec is None:
                    continue
                if rec.occurred_at <= since:
                    skipped_old += 1
                    continue
                yield rec
                count += 1
            log.info("outlook.fetch_done", yielded=count, skipped_old=skipped_old)
        finally:
            with suppress(Exception):
                pythoncom.CoUninitialize()

    def _resolve_inbox(self, ns: Any) -> Any:
        if not self.account:
            return ns.GetDefaultFolder(OL_FOLDER_INBOX)
        for acc in ns.Accounts:
            if acc.SmtpAddress == self.account:
                store = acc.DeliveryStore
                if store is not None:
                    return store.GetDefaultFolder(OL_FOLDER_INBOX)
        for store in ns.Stores:
            if store.DisplayName == self.account:
                return store.GetDefaultFolder(OL_FOLDER_INBOX)
        raise RuntimeError(f"outlook account not found: {self.account!r}")

    def _compute_since(self, checkpoint: dict[str, Any] | None) -> datetime:
        if checkpoint and isinstance(checkpoint.get("last_received_at"), str):
            try:
                dt = datetime.fromisoformat(checkpoint["last_received_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt.astimezone(UTC)
            except ValueError:
                pass
        return datetime.now(UTC) - timedelta(days=self.since_days)

    def _to_record(self, item: Any, folder_name: str) -> SourceRecord | None:
        if _safe_get(item, "Class") != OL_MAIL_ITEM_CLASS:
            return None
        msg_id = _safe_prop(item, PR_INTERNET_MESSAGE_ID)
        entry_id = _safe_get(item, "EntryID")
        if not msg_id and not entry_id:
            return None
        external_id = f"outlook:{entry_id}" if entry_id else f"outlook-mid:{msg_id}"

        received = _received_dt(item)
        raw_headers = _safe_prop(item, PR_TRANSPORT_MESSAGE_HEADERS)
        headers = _parse_headers(raw_headers)

        from_addr = _resolve_smtp(item)
        from_name = _safe_get(item, "SenderName", "") or ""
        from_obj: Address | None = None
        if from_addr:
            from_obj = Address(email=from_addr, name=from_name or None)

        subject = _safe_get(item, "Subject", "") or ""
        to_list = _split_addresses(_safe_get(item, "To", "") or "")
        cc_list = _split_addresses(_safe_get(item, "CC", "") or "")

        body, truncated = _body_text(item, self.max_body_bytes)
        size_bytes = int(_safe_get(item, "Size", 0) or 0)

        refs = [r.strip() for r in (headers.get("references") or "").split() if r.strip()]

        payload = EmailPayload(
            from_=from_obj,
            to=to_list,
            cc=cc_list,
            subject=subject,
            date=received,
            message_id=msg_id or None,
            in_reply_to=headers.get("in-reply-to"),
            references=refs,
            list_id=headers.get("list-id"),
            list_unsubscribe=headers.get("list-unsubscribe"),
            precedence=headers.get("precedence"),
            auto_submitted=headers.get("auto-submitted"),
            body_text=body,
            body_truncated=truncated,
            folder=folder_name,
            flags=[],
            size_bytes=size_bytes,
            raw_headers=headers,
        )

        return SourceRecord(
            external_id=external_id,
            occurred_at=received,
            payload=payload.model_dump(mode="json", by_alias=True),
            dedupe_keys=[f"msgid:<{msg_id}>"] if msg_id else [],
        )

    def advance_checkpoint(
        self, checkpoint: dict[str, Any] | None, last: SourceRecord
    ) -> dict[str, Any]:
        return {"last_received_at": last.occurred_at.isoformat()}
