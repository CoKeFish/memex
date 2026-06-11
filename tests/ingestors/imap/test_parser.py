from __future__ import annotations

import email
from datetime import UTC, datetime
from email.message import EmailMessage
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from memex.ingestors.imap.parser import parse_email_message

SERVER = "imap.example.com"
FOLDER = "INBOX"
UIDVALIDITY = 17
UID = 42
INTERNALDATE = datetime(2026, 5, 23, 10, 0, 0, tzinfo=UTC)


def _parse(raw: bytes, **overrides: Any) -> Any:
    msg = email.message_from_bytes(raw)
    kwargs: dict[str, Any] = {
        "server": SERVER,
        "folder": FOLDER,
        "uidvalidity": UIDVALIDITY,
        "uid": UID,
        "internaldate": INTERNALDATE,
        "flags": ["\\Seen"],
        "size_bytes": len(raw),
        "max_body_bytes": 524288,
        "fetch_body": True,
    }
    kwargs.update(overrides)
    return parse_email_message(msg, **kwargs)


# ---------- 1. Simple text -------------------------------------------------- #


def test_simple_text_email() -> None:
    msg = MIMEText("Hello, world!\nThis is a simple test.")
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "me@gmail.com"
    msg["Subject"] = "Simple test"
    msg["Date"] = "Mon, 23 May 2026 10:00:00 +0000"
    msg["Message-ID"] = "<simple1@example.com>"

    record = _parse(msg.as_bytes())

    assert record.external_id == "imap:imap.example.com:17:42"
    assert record.payload["from"] == {"email": "alice@example.com", "name": "Alice"}
    assert record.payload["to"] == [{"email": "me@gmail.com", "name": None}]
    assert record.payload["subject"] == "Simple test"
    assert record.payload["body_text"].startswith("Hello, world!")
    assert record.payload["body_source"] == "text"
    assert record.payload["body_truncated"] is False
    assert record.payload["message_id"] == "simple1@example.com"
    assert record.payload["folder"] == "INBOX"
    assert record.payload["flags"] == ["\\Seen"]
    assert record.dedupe_keys == [
        "msgid:simple1@example.com",
        "imap:imap.example.com:17:42",
    ]


# ---------- 2. HTML only (must strip) -------------------------------------- #


def test_html_parse_failure_logs_partial_extraction(sink_capture: Any, monkeypatch: Any) -> None:
    """Si el HTMLParser revienta a mitad, el cuerpo queda PARCIAL (lo acumulado hasta el fallo):
    el recorte debe dejar `imap.html_to_text.partial` en log_events, no pasar inadvertido."""
    import json

    from memex.ingestors.imap import parser as parser_mod

    def boom(self: Any, data: str) -> None:
        raise RuntimeError("html venenoso")

    monkeypatch.setattr(parser_mod._TextExtractor, "feed", boom)
    out = parser_mod._html_to_text("<p>hola</p>")

    assert out == ""  # nada alcanzó a acumularse; el mensaje sigue parseando sin reventar
    records = []
    while not sink_capture.empty():
        records.append(sink_capture.get_nowait())
    partial = [r for r in records if r["event"] == "imap.html_to_text.partial"]
    assert len(partial) == 1
    assert partial[0]["level"] == "warning"
    fields = json.loads(partial[0]["fields"])
    assert fields["exc_type"] == "RuntimeError"
    assert fields["html_len"] == len("<p>hola</p>")
    assert fields["extracted_len"] == 0


def test_html_only_strips_to_text() -> None:
    html = (
        "<html><head><style>p { color: red; }</style></head>"
        "<body><p>Hola <b>mundo</b></p>"
        "<script>evil()</script>"
        "<p>otra línea</p></body></html>"
    )
    msg = MIMEText(html, "html")
    msg["From"] = "Bob <bob@example.com>"
    msg["To"] = "me@gmail.com"
    msg["Subject"] = "HTML"
    msg["Date"] = "Mon, 23 May 2026 10:00:00 +0000"
    msg["Message-ID"] = "<html1@example.com>"

    record = _parse(msg.as_bytes())

    body = record.payload["body_text"]
    assert "Hola" in body
    assert "mundo" in body
    assert "otra línea" in body
    # Script/style content stripped
    assert "evil" not in body
    assert "color: red" not in body
    # No HTML tags left
    assert "<" not in body and ">" not in body
    assert record.payload["body_source"] == "html_stripped"


# ---------- 3. Multipart with attachments ---------------------------------- #


def test_multipart_with_attachments() -> None:
    root = MIMEMultipart("mixed")
    root["From"] = "Alice <alice@example.com>"
    root["To"] = "me@example.com"
    root["Subject"] = "with attachments"
    root["Date"] = "Mon, 23 May 2026 10:00:00 +0000"
    root["Message-ID"] = "<attach1@example.com>"

    body = MIMEText("Texto del cuerpo")
    root.attach(body)

    pdf = MIMEBase("application", "pdf")
    pdf.set_payload(b"%PDF-fake-bytes-here")
    pdf.add_header("Content-Disposition", "attachment", filename="invoice.pdf")
    root.attach(pdf)

    record = _parse(root.as_bytes())

    assert record.payload["body_text"].startswith("Texto del cuerpo")
    atts = record.payload["attachments"]
    assert len(atts) == 1
    assert atts[0]["filename"] == "invoice.pdf"
    assert atts[0]["content_type"] == "application/pdf"
    assert atts[0]["size"] > 0


# ---------- 4. Newsletter with List-Id ------------------------------------- #


def test_newsletter_with_list_id() -> None:
    msg = MIMEText("Aquí está tu newsletter semanal")
    msg["From"] = "Newsletter <news@bigcompany.com>"
    msg["To"] = "me@gmail.com"
    msg["Subject"] = "Weekly digest"
    msg["Date"] = "Mon, 23 May 2026 10:00:00 +0000"
    msg["Message-ID"] = "<news1@bigcompany.com>"
    msg["List-Id"] = "<weekly.bigcompany.com>"
    msg["List-Unsubscribe"] = "<mailto:unsub@bigcompany.com>, <https://unsub.example>"
    msg["Precedence"] = "bulk"

    record = _parse(msg.as_bytes())

    assert record.payload["list_id"] == "<weekly.bigcompany.com>"
    assert record.payload["list_unsubscribe"] is not None
    assert record.payload["precedence"] == "bulk"


# ---------- 5. Thread reply with In-Reply-To + References ------------------ #


def test_thread_reply_carries_threading_headers() -> None:
    msg = MIMEText("Sí, perfecto")
    msg["From"] = "Bob <bob@example.com>"
    msg["To"] = "Alice <alice@example.com>"
    msg["Subject"] = "Re: dinner plans"
    msg["Date"] = "Mon, 23 May 2026 10:00:00 +0000"
    msg["Message-ID"] = "<reply3@example.com>"
    msg["In-Reply-To"] = "<orig2@example.com>"
    msg["References"] = "<orig1@example.com> <orig2@example.com>"

    record = _parse(msg.as_bytes())

    assert record.payload["in_reply_to"] == "orig2@example.com"
    assert record.payload["references"] == ["orig1@example.com", "orig2@example.com"]


# ---------- 6. No Message-ID — only imap: dedupe key ----------------------- #


def test_no_message_id_uses_imap_dedupe_key_only() -> None:
    msg = MIMEText("No header Message-ID")
    msg["From"] = "Auto <noreply@autosystem.example>"
    msg["To"] = "me@gmail.com"
    msg["Subject"] = "Auto alert"
    msg["Date"] = "Mon, 23 May 2026 10:00:00 +0000"
    # Note: NO Message-ID set

    record = _parse(msg.as_bytes())

    assert record.payload["message_id"] is None
    assert record.dedupe_keys == ["imap:imap.example.com:17:42"]


# ---------- 7. Non-ASCII subject (MIME encoded) ---------------------------- #


def test_non_ascii_subject_decoded() -> None:
    msg = EmailMessage()
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "me@gmail.com"
    # email.policy default supports unicode in headers natively
    msg["Subject"] = "Reunión con José — ñoño café"
    msg["Date"] = "Mon, 23 May 2026 10:00:00 +0000"
    msg["Message-ID"] = "<utf1@example.com>"
    msg.set_content("Hola.")

    record = _parse(msg.as_bytes())

    assert record.payload["subject"] == "Reunión con José — ñoño café"


# ---------- 8. Huge body — truncated --------------------------------------- #


def test_huge_body_is_truncated() -> None:
    huge = "A" * 1_000_000  # 1 MB
    msg = MIMEText(huge)
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "me@gmail.com"
    msg["Subject"] = "Big"
    msg["Date"] = "Mon, 23 May 2026 10:00:00 +0000"
    msg["Message-ID"] = "<big1@example.com>"

    record = _parse(msg.as_bytes(), max_body_bytes=10_000)

    assert record.payload["body_truncated"] is True
    body = record.payload["body_text"]
    # Truncated body fits within max_body_bytes when encoded.
    assert len(body.encode("utf-8")) <= 10_000


# ---------- 9. Malformed date — falls back to internaldate ----------------- #


def test_malformed_date_falls_back_to_internaldate() -> None:
    msg = MIMEText("hi")
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "me@gmail.com"
    msg["Subject"] = "Bad date"
    msg["Date"] = "this is not a date"
    msg["Message-ID"] = "<baddate@example.com>"

    record = _parse(msg.as_bytes())

    assert record.occurred_at == INTERNALDATE


def test_pre_2000_date_falls_back_to_internaldate() -> None:
    msg = MIMEText("hi")
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "me@gmail.com"
    msg["Subject"] = "Old"
    msg["Date"] = "Tue, 03 Jan 1995 12:00:00 +0000"
    msg["Message-ID"] = "<old@example.com>"

    record = _parse(msg.as_bytes())

    assert record.occurred_at == INTERNALDATE


# ---------- Additional invariants ------------------------------------------ #


def test_external_id_is_first_in_dedupe_when_no_msg_id() -> None:
    msg = MIMEText("hi")
    msg["From"] = "x@example.com"
    msg["Subject"] = "no msg id"
    record = _parse(msg.as_bytes())
    assert record.dedupe_keys[0].startswith("imap:")


def test_msg_id_is_first_in_dedupe_when_present() -> None:
    msg = MIMEText("hi")
    msg["From"] = "x@example.com"
    msg["Subject"] = "with msg id"
    msg["Message-ID"] = "<m1@x>"
    record = _parse(msg.as_bytes())
    assert record.dedupe_keys[0] == "msgid:m1@x"


def test_address_list_parsing_multiple_recipients() -> None:
    msg = MIMEText("hi")
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "Bob <bob@example.com>, Carol <carol@example.com>"
    msg["Cc"] = "Dave <dave@example.com>"
    msg["Subject"] = "multi"
    msg["Message-ID"] = "<multi@example.com>"

    record = _parse(msg.as_bytes())

    to_list = record.payload["to"]
    assert len(to_list) == 2
    assert to_list[0]["email"] == "bob@example.com"
    assert to_list[1]["email"] == "carol@example.com"
    assert record.payload["cc"] == [{"email": "dave@example.com", "name": "Dave"}]


def test_raw_headers_whitelist() -> None:
    msg = MIMEText("hi")
    msg["From"] = "x@example.com"
    msg["Subject"] = "headers test"
    msg["X-Mailer"] = "OutlookFakeClient 1.0"
    msg["Received-SPF"] = "pass"
    msg["X-Some-Random-Header"] = "shouldnotappear"

    record = _parse(msg.as_bytes())

    raw = record.payload["raw_headers"]
    assert raw.get("X-Mailer") == "OutlookFakeClient 1.0"
    assert raw.get("Received-SPF") == "pass"
    assert "X-Some-Random-Header" not in raw


def test_fetch_body_false_skips_body_extraction() -> None:
    msg = MIMEText("texto del body")
    msg["From"] = "x@example.com"
    msg["Subject"] = "no body fetch"
    record = _parse(msg.as_bytes(), fetch_body=False)
    assert record.payload["body_text"] == ""
    assert record.payload["body_truncated"] is False
