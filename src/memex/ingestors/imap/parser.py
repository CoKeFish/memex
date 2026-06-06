from __future__ import annotations

import email.utils
import re
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from html.parser import HTMLParser
from io import StringIO
from typing import Literal

from memex.core.media_types import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    DEFAULT_MIN_MEDIA_BYTES,
    MEDIA_CONTENT_TYPES,
    make_media_blob,
)
from memex.core.payloads import Address, Attachment, EmailPayload
from memex.core.source import MediaBlob, SourceRecord
from memex.logging import get_logger

RAW_HEADERS_WHITELIST = (
    "X-Mailer",
    "Received-SPF",
    "Authentication-Results",
)

BodySource = Literal["text", "html_stripped"]

#: Content-types de adjuntos que extraemos para OCR (imágenes + PDF + ZIP) y el tope por adjunto
#: viven en `core.media_types` (fuente única, compartida con las sources sociales). Re-exportados
#: acá por compatibilidad con quien los importe desde el parser.
__all__ = ["DEFAULT_MAX_ATTACHMENT_BYTES", "DEFAULT_MIN_MEDIA_BYTES", "MEDIA_CONTENT_TYPES"]

_log = get_logger("memex.ingestors.imap.parser")


def parse_email_message(
    msg: Message,
    *,
    server: str,
    folder: str,
    uidvalidity: int,
    uid: int,
    internaldate: datetime,
    flags: list[str],
    size_bytes: int,
    max_body_bytes: int = 524288,
    fetch_body: bool = True,
    extract_media: bool = False,
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    min_attachment_bytes: int = DEFAULT_MIN_MEDIA_BYTES,
) -> SourceRecord:
    """Convert a stdlib email.message.Message + IMAP metadata into a SourceRecord.

    Pure function — no I/O. Testable with raw .eml fixtures.

    `internaldate` is the IMAP server's INTERNALDATE for the message. It serves
    as fallback when the Date header is missing, malformed, or absurd (clock skew,
    date in the future, pre-2000).

    The payload is built via the typed `EmailPayload` Pydantic model — typos in
    field names become static type errors here, not silent JSONB misses at
    classification time. The model is serialized to dict at the SourceRecord
    boundary; the storage layer stays schema-agnostic.

    When `extract_media` is on, image/PDF attachments are decoded to bytes and carried
    on `SourceRecord.media` (base64) for the ingest boundary to upload to MinIO. Default
    off → unchanged behavior. Attachment METADATA always stays in `payload.attachments`.
    """
    msg_id = _strip_brackets(msg.get("Message-ID"))
    in_reply_to = _strip_brackets(msg.get("In-Reply-To"))
    references = _parse_references(msg.get("References"))

    from_addr = _parse_address(msg.get("From"))
    to_addrs = _parse_address_list(msg.get_all("To"))
    cc_addrs = _parse_address_list(msg.get_all("Cc"))
    reply_to_addrs = _parse_address_list(msg.get_all("Reply-To"))

    parsed_date = _parse_date(msg.get("Date"))
    occurred_at = (
        parsed_date
        if parsed_date is not None and _is_reasonable_date(parsed_date)
        else internaldate
    )

    body_text = ""
    body_source: BodySource = "text"
    body_truncated = False
    if fetch_body:
        body_text, body_source, body_truncated = _extract_body(msg, max_body_bytes)

    attachments = _extract_attachment_meta(msg)
    media = (
        _extract_media(msg, max_bytes=max_attachment_bytes, min_bytes=min_attachment_bytes)
        if extract_media
        else []
    )

    raw_headers: dict[str, str] = {}
    for h in RAW_HEADERS_WHITELIST:
        v = msg.get(h)
        if v is not None:
            raw_headers[h] = str(v)

    external_id = f"imap:{server}:{uidvalidity}:{uid}"
    dedupe_keys: list[str] = []
    if msg_id:
        dedupe_keys.append(f"msgid:{msg_id}")
    dedupe_keys.append(external_id)

    payload_model = EmailPayload(
        from_=from_addr,
        to=to_addrs,
        cc=cc_addrs,
        reply_to=reply_to_addrs,
        subject=_decode_header(msg.get("Subject")),
        date=occurred_at,
        message_id=msg_id,
        in_reply_to=in_reply_to,
        references=references,
        list_id=_nonempty(msg.get("List-ID") or msg.get("List-Id")),
        list_unsubscribe=_nonempty(msg.get("List-Unsubscribe")),
        list_unsubscribe_post=_nonempty(msg.get("List-Unsubscribe-Post")),
        precedence=_nonempty(msg.get("Precedence")),
        auto_submitted=_nonempty(msg.get("Auto-Submitted")),
        body_text=body_text,
        body_source=body_source,
        body_truncated=body_truncated,
        folder=folder,
        flags=list(flags),
        size_bytes=size_bytes,
        attachments=attachments,
        raw_headers=raw_headers,
    )

    return SourceRecord(
        external_id=external_id,
        occurred_at=occurred_at,
        payload=payload_model.model_dump(mode="json", by_alias=True),
        dedupe_keys=dedupe_keys,
        media=media,
    )


# ----- Helpers (pure functions) ----------------------------------------------


def _nonempty(raw: str | None) -> str | None:
    """Return None for missing or whitespace-only header values."""
    if not raw:
        return None
    stripped = raw.strip()
    return stripped or None


def _decode_header(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _strip_brackets(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if v.startswith("<") and v.endswith(">"):
        return v[1:-1]
    return v


_REF_RE = re.compile(r"<([^>]+)>")


def _parse_references(value: str | None) -> list[str]:
    if not value:
        return []
    return _REF_RE.findall(value)


def _parse_address(raw: str | None) -> Address | None:
    if not raw:
        return None
    name, email_addr = email.utils.parseaddr(raw)
    decoded_name = _decode_header(name) if name else None
    if not email_addr:
        return None
    return Address(email=email_addr, name=decoded_name or None)


def _parse_address_list(raw_headers: list[str] | None) -> list[Address]:
    if not raw_headers:
        return []
    result: list[Address] = []
    for header_value in raw_headers:
        for name, email_addr in email.utils.getaddresses([header_value]):
            if not email_addr:
                continue
            decoded_name = _decode_header(name) if name else None
            result.append(Address(email=email_addr, name=decoded_name or None))
    return result


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _is_reasonable_date(dt: datetime) -> bool:
    """Reject dates outside [2000-01-01, now + 1 year]."""
    lower = datetime(2000, 1, 1, tzinfo=UTC)
    upper = datetime.now(UTC) + timedelta(days=365)
    return lower <= dt <= upper


def _extract_body(msg: Message, max_bytes: int) -> tuple[str, BodySource, bool]:
    """Returns (body_text, body_source, body_truncated).

    Prefer text/plain. Fallback to text/html stripped to text. Attachments are
    excluded based on Content-Disposition.
    """
    text_part: Message | None = None
    html_part: Message | None = None

    for part in msg.walk():
        if part.is_multipart():
            continue
        cdisp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in cdisp:
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and text_part is None:
            text_part = part
        elif ctype == "text/html" and html_part is None:
            html_part = part

    if text_part is not None:
        text = _decode_part(text_part)
        return _maybe_truncate(text, max_bytes, source="text")

    if html_part is not None:
        html = _decode_part(html_part)
        text = _html_to_text(html)
        return _maybe_truncate(text, max_bytes, source="html_stripped")

    # Single non-text part — return empty body
    return "", "text", False


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    if isinstance(payload, bytes):
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            return payload.decode("utf-8", errors="replace")
    return str(payload)


def _maybe_truncate(
    text: str, max_bytes: int, *, source: BodySource
) -> tuple[str, BodySource, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, source, False
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated, source, True


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._buf = StringIO()
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head"):
            self._skip_depth += 1
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self._buf.write("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in ("p", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self._buf.write("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._buf.write(data)

    def text(self) -> str:
        return self._buf.getvalue()


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    raw = parser.text()
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw.split("\n")]
    return "\n".join(line for line in lines if line)


def _extract_attachment_meta(msg: Message) -> list[Attachment]:
    result: list[Attachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        cdisp = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        is_attachment = "attachment" in cdisp or "inline" in cdisp or bool(filename)
        if not is_attachment:
            continue
        if not filename and part.get_content_type() in ("text/plain", "text/html"):
            continue
        decoded_filename = _decode_header(filename) if filename else None
        payload = part.get_payload(decode=True)
        size = len(payload) if isinstance(payload, bytes) else 0
        result.append(
            Attachment(
                filename=decoded_filename,
                content_type=part.get_content_type(),
                size=size,
                content_id=_strip_brackets(part.get("Content-ID")),
            )
        )
    return result


def _extract_media(
    msg: Message, *, max_bytes: int, min_bytes: int = DEFAULT_MIN_MEDIA_BYTES
) -> list[MediaBlob]:
    """Extrae los BYTES de adjuntos imagen/PDF (para subir a MinIO + OCR).

    Re-camina `msg.walk()` (independiente de `_extract_attachment_meta`, que solo saca metadata).
    Solo content-types en `MEDIA_CONTENT_TYPES`. Saltea (y loguea) los que pasan `max_bytes` o
    no llegan a `min_bytes` (tracking pixels / logos de firma). sha256 + base64 con stdlib →
    función pura, testeable con `.eml`.
    """
    result: list[MediaBlob] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if ctype not in MEDIA_CONTENT_TYPES:
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or not payload:
            continue
        if len(payload) > max_bytes:
            _log.warning(
                "imap.media.too_large",
                content_type=ctype,
                size=len(payload),
                max_bytes=max_bytes,
            )
            continue
        if len(payload) < min_bytes:
            # Casi seguro un tracking pixel o logo de firma; no vale subirlo+OCR-earlo. A nivel
            # debug (no warning) porque son altísima frecuencia y a warning inundarían el log.
            _log.debug(
                "imap.media.too_small",
                content_type=ctype,
                size=len(payload),
                min_bytes=min_bytes,
            )
            continue
        filename = part.get_filename()
        result.append(
            make_media_blob(
                payload,
                content_type=ctype,
                filename=_decode_header(filename) if filename else None,
            )
        )
    return result
