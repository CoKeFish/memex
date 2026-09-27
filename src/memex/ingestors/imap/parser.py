from __future__ import annotations

import email.utils
import re
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from typing import Literal

from memex.core.html_text import html_to_text as _html_to_text
from memex.core.html_text import strip_invisible_padding as _strip_invisible_padding
from memex.core.html_text import with_image_text as _with_image_text
from memex.core.media_types import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    DEFAULT_MIN_MEDIA_BYTES,
    MEDIA_CONTENT_TYPES,
    make_media_blob,
)
from memex.core.payloads import Address, Attachment, EmailPayload, msgid_dedupe_key
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
    msgid_key = msgid_dedupe_key(msg_id)
    if msgid_key:
        dedupe_keys.append(msgid_key)
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


def _header_text(raw: object) -> str:
    """Valor de una cabecera como `str`, venga como venga.

    Con la policy por defecto (`compat32`), `msg.get()` devuelve un `email.header.Header` —no un
    `str`— cuando la cabecera cruda trae bytes no-ASCII sin codificar (p. ej.
    `Content-Disposition: attachment; filename="informe año.pdf"` mandado en latin-1). Llamar
    `.lower()` sobre eso tumbaba TODA la corrida de fetch (`'Header' object has no attribute
    'lower'`, visto en Gmail 2026-09-01). `str()` lo aplana; None → "".
    """
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else str(raw)


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

    Prefer the first text/plain part WITH CONTENT. Fallback to text/html stripped to text.
    Attachments are excluded based on Content-Disposition.

    Gane la parte que gane, al cuerpo se le SUMA el texto de las imágenes (`alt`) que no esté ya
    dicho: un boletín puede llevar ahí el nombre del evento y la fecha, y la parte de texto plano
    nunca puede contenerlo.

    Una parte vacía no es «el texto»: hay remitentes (Kronnect, almanax.ai, Tullave…) que mandan
    `multipart/alternative` con un text/plain de 0 bytes DELANTE del texto real y del HTML.
    Quedarse con la primera parte a secas guardaba el cuerpo vacío (16 correos así en dev,
    2026-09-04); por eso se sigue buscando hasta una parte con contenido.
    """
    text_part: Message | None = None
    html_part: Message | None = None

    for part in msg.walk():
        if part.is_multipart():
            continue
        cdisp = _header_text(part.get("Content-Disposition")).lower()
        if "attachment" in cdisp:
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and text_part is None and _decode_part(part).strip():
            text_part = part
        elif ctype == "text/html" and html_part is None and _decode_part(part).strip():
            html_part = part

    html_text: str | None = None
    alts: list[str] = []
    if html_part is not None:
        html_text, alts = _html_to_text(_decode_part(html_part))

    if text_part is not None:
        text = _strip_invisible_padding(_decode_part(text_part))
        if html_text is not None and _is_stub(text, html_text):
            # Texto plano «resumen» («Ver en el navegador» + link) con el contenido real en el
            # HTML: medido 2026-09-04 en 300 correos, 1 caso (Google One) — raro pero ese correo
            # quedaba sin su contenido. Se prefiere el HTML convertido.
            _log.info(
                "imap.body.html_preferred",
                text_len=len(text.strip()),
                html_text_len=len(html_text.strip()),
            )
            return _maybe_truncate(html_text, max_bytes, source="html_stripped")
        # El texto de las imágenes se suma: la parte de texto plano no puede traerlo.
        return _maybe_truncate(_with_image_text(text, alts), max_bytes, source="text")

    if html_text is not None:
        return _maybe_truncate(html_text, max_bytes, source="html_stripped")

    # Single non-text part — return empty body
    return "", "text", False


#: El texto plano es un «stub» si, quitadas las URLs, tiene menos de la MITAD del contenido del
#: HTML convertido y la diferencia es material. Se miden sin URLs porque el stub típico es una frase
#: más un enlace de 2 KB («To view the details, please visit https://…»): el recibo de Steam del
#: 2026-09-03 medía 2915 chars de texto (510 sin URLs) contra 1655 del HTML, y el umbral anterior
#: (20 % del largo bruto) lo dejaba pasar: se perdían los artículos, montos y la fecha.
_STUB_RATIO = 0.5
_STUB_MIN_GAP = 200
_URL_RE = re.compile(r"https?://\S+")


def _is_stub(text: str, html_text: str) -> bool:
    t = len(_URL_RE.sub("", text).strip())
    h = len(_URL_RE.sub("", html_text).strip())
    return h > 0 and t < _STUB_RATIO * h and (h - t) >= _STUB_MIN_GAP


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


#: `alt` que no dice nada: el rótulo genérico que ponen las plantillas de boletín. Se comparan en
#: minúscula y sin espacios de sobra. Todo lo demás con 3+ caracteres se toma como contenido.
_ALT_NOISE = frozenset(
    {
        "image",
        "img",
        "imagen",
        "logo",
        "icon",
        "icono",
        "banner",
        "photo",
        "foto",
        "picture",
        "avatar",
        "header",
        "footer",
        "spacer",
        "divider",
        "separator",
        "no title",
        "sin titulo",
        "sin título",
        "untitled",
    }
)


def _extract_attachment_meta(msg: Message) -> list[Attachment]:
    result: list[Attachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        cdisp = _header_text(part.get("Content-Disposition")).lower()
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
