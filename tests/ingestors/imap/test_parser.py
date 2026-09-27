from __future__ import annotations

import email
from datetime import UTC, datetime
from email.message import EmailMessage, Message
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from memex.core.html_text import strip_invisible_padding as _strip_invisible_padding
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
    el recorte debe dejar `html_to_text.partial` en log_events, no pasar inadvertido."""
    import json

    from memex.core import html_text as html_mod

    def boom(self: Any, data: str) -> None:
        raise RuntimeError("html venenoso")

    monkeypatch.setattr(html_mod.HtmlTextExtractor, "feed", boom)
    out, alts = html_mod.html_to_text("<p>hola</p>")

    assert out == "" and alts == []  # nada alcanzó a acumularse; el mensaje sigue parseando
    records = []
    while not sink_capture.empty():
        records.append(sink_capture.get_nowait())
    partial = [r for r in records if r["event"] == "html_to_text.partial"]
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


def test_content_disposition_with_raw_non_ascii_header_does_not_crash() -> None:
    """Regresión (Gmail 2026-09-01): un `Content-Disposition` con bytes no-ASCII SIN codificar hace
    que `msg.get()` devuelva un `email.header.Header` (no `str`); el parser llamaba `.lower()` sobre
    él y tumbaba toda la corrida de fetch. Debe parsear igual: cuerpo + adjunto detectado."""
    raw = (
        b"From: a@x.io\r\nTo: b@x.io\r\nSubject: informe\r\nMessage-ID: <h@x.io>\r\n"
        b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nhola\r\n"
        b"--B\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="informe a\xf1o.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0=\r\n--B--\r\n"
    )
    msg = email.message_from_bytes(raw)
    parts = msg.get_payload()
    assert isinstance(parts, list)
    attachment = parts[1]
    assert isinstance(attachment, Message)
    assert not isinstance(attachment.get("Content-Disposition"), str)  # el caso real: Header

    record = _parse(raw)

    assert record.payload["body_text"].strip() == "hola"
    assert len(record.payload["attachments"]) == 1


# ---------- Parte de texto vacía delante del texto real (Kronnect, 2026-09-04) --------------- #


def _alternative(*parts: Any) -> bytes:
    msg = MIMEMultipart("alternative")
    msg["From"] = "Kronnect <contact@kronnect.com>"
    msg["To"] = "me@gmail.com"
    msg["Subject"] = "Player32 is open"
    msg["Date"] = "Thu, 04 Sep 2026 17:00:00 +0000"
    for p in parts:
        msg.attach(p)
    return msg.as_bytes()


def test_empty_text_part_before_real_text_is_skipped() -> None:
    """`multipart/alternative` con un text/plain VACÍO, luego el texto real, luego el HTML: el
    cuerpo es el texto real (antes se tomaba la primera parte y quedaba vacío)."""
    raw = _alternative(
        MIMEText("", "plain"),
        MIMEText("Player32 is open: submit your game.", "plain", "utf-8"),
        MIMEText("<html><body><p>Player32 is open</p></body></html>", "html", "utf-8"),
    )
    record = _parse(raw)
    assert record.payload["body_text"].strip() == "Player32 is open: submit your game."
    assert record.payload["body_source"] == "text"


def test_empty_text_part_falls_back_to_html() -> None:
    """Sin ninguna parte de texto con contenido, cae al HTML (no a un cuerpo vacío)."""
    raw = _alternative(
        MIMEText("   \n", "plain"),
        MIMEText("<html><body><p>Player32 is open</p></body></html>", "html", "utf-8"),
    )
    record = _parse(raw)
    assert "Player32 is open" in record.payload["body_text"]
    assert record.payload["body_source"] == "html_stripped"


def test_text_stub_prefers_html_content() -> None:
    """Texto plano «Ver en el navegador» + link (stub) mientras el HTML trae el contenido real
    (Google One, 2026-09-04): gana el HTML convertido."""
    real = "<p>No podemos procesar el pago de tu plan Google One de 100 GB.</p>" * 25
    raw = _alternative(
        MIMEText("Ver en el navegador <https://notifications.google.com/x>", "plain", "utf-8"),
        MIMEText(f"<html><body>{real}</body></html>", "html", "utf-8"),
    )
    record = _parse(raw)
    assert record.payload["body_source"] == "html_stripped"
    assert "No podemos procesar el pago" in record.payload["body_text"]


def test_comparable_text_and_html_keeps_text() -> None:
    """Con texto plano de largo comparable al HTML (lo normal), sigue ganando el texto plano."""
    body = "Tu recibo de Uber: viaje de 17:07 a 17:24, total COP 24.139. Gracias por viajar.\n" * 5
    raw = _alternative(
        MIMEText(body, "plain", "utf-8"),
        MIMEText("<html><body><p>" + body.replace("\n", "</p><p>") + "</p></body></html>", "html"),
    )
    record = _parse(raw)
    assert record.payload["body_source"] == "text"
    assert record.payload["body_text"].startswith("Tu recibo de Uber")


# ---------- Relleno invisible del «preheader» (2026-09-04) ---------------------------------- #


def test_strip_padding_runs_but_keeps_legit_zero_width() -> None:
    pad = "\u034f \u034f \u034f \u034f \u034f \u200c\xa0\u200c\xa0\u200c"
    assert _strip_invisible_padding(f"Hola{pad}mundo") == "Hola mundo"
    # ZWJ de emoji compuesto: sueltos entre emojis → intactos
    familia = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert _strip_invisible_padding(f"{familia} familia") == f"{familia} familia"
    # ZWNJ dentro de palabra (persa) → intacto
    assert _strip_invisible_padding("\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645") == (
        "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"
    )
    # guion suave y BOM: siempre fuera, la palabra queda entera
    assert _strip_invisible_padding("\ufeffin\u00adfor\u00admation") == "information"
    # dos invisibles sueltos NO son tira
    assert _strip_invisible_padding("a\u200b\u200bb") == "a\u200b\u200bb"
    # separadores que no son el espacio ASCII: figure space (Google) y saltos de l\u00ednea del HTML
    assert _strip_invisible_padding("A\u2007\u034f\u2007\u034f\u2007\u034f\u2007B") == "A B"
    assert _strip_invisible_padding("A\u034f\r\n\u034f\r\n\u034f\r\nB") == "A B"


def test_html_preheader_padding_is_removed_from_body() -> None:
    """Boletín con preheader relleno de U+034F (LinkedIn/Google): el cuerpo no lo arrastra y el
    largo comparado para la regla de «stub» ya es el limpio."""
    pad = "".join("\u034f " for _ in range(300))
    html = (
        f"<html><body><div>Resumen semanal{pad}</div>"
        "<p>Te has puesto en contacto con Ana. Reunión el martes 10:00.</p></body></html>"
    )
    text_plain = "Resumen semanal\nTe has puesto en contacto con Ana. Reunión el martes 10:00.\n"
    raw = _alternative(MIMEText(text_plain, "plain", "utf-8"), MIMEText(html, "html", "utf-8"))
    record = _parse(raw)
    assert record.payload["body_source"] == "text"  # sin el relleno, el texto plano es comparable
    raw_html_only = _alternative(MIMEText(html, "html", "utf-8"))
    body = _parse(raw_html_only).payload["body_text"]
    assert "\u034f" not in body
    assert body.startswith("Resumen semanal")
    assert "Reunión el martes 10:00" in body


def test_text_stub_with_long_url_prefers_html_receipt() -> None:
    """Recibo de Steam (2026-09-03): el texto plano es «visita este enlace» + una URL de 2 KB y
    boilerplate; el HTML trae artículos, montos y fecha. Sin URLs el texto es < 50 % del HTML."""
    url = "https://store.steampowered.com/email/VATPurchaseReceipt?sparams=" + "eJyFVduO2yAQ" * 200
    boiler = (
        "This notification has been sent to the email address associated with your Steam "
        "account. For information about Valve's privacy policy, visit the link above. "
    )
    text_plain = (
        "Hello ifishcokei\n\nThank you for your recent transaction on Steam. To view the "
        f"details, please visit {url}\n\nCheers,\nThe Steam Team\n\n" + boiler
    )
    items = (("Celeste", "9.979", "1.896", "11.875"), ("Outer Wilds", "28.185", "5.355", "33.540"))
    rows = "".join(
        f"<tr><td>{g}</td><td>Subtotal (excl. VAT): COL$ {a}</td>"
        f"<td>VAT at 19%: COL$ {v}</td><td>Total: COL$ {tt}</td></tr>"
        for g, a, v, tt in items
    )
    html = (
        "<html><body><p>Hello ifishcokei</p>"
        "<p>Thank you for your recent transaction on Steam.</p>"
        "<p>The items below have been added to your Steam Library.</p>"
        "<p>If you are new to Steam, you can get the free Steam application here.</p>"
        "<p>Your Steam Points</p><p>You've earned Steam Points</p><p>Visit the Points Shop</p>"
        "<p>1,446</p>"
        f"<table>{rows}</table><p>Account name: ifishcokei</p><p>Invoice: 196623530707115053</p>"
        "<p>Date issued: 3 Sep, 2026 @ 7:18pm -05</p>"
        "<p>Subtotal (excl. VAT): COL$ 38.164</p><p>VAT at 19%: COL$ 7.251</p>"
        "<p>Total: COL$ 45.415</p><p>Payment method: Visa</p>"
        "<p>Billing address: Danilo Salgado, cl 1 # 2-3, Bogota, 110111, Colombia</p>"
        "<p>Valve Corporation, PO Box 1688, Bellevue, WA 98009, USA. VAT ID: 901245475</p>"
        "<p>Please note that this is not a returns address.</p>"
        "<p>Account name: ifishcokei</p><p>Payment method: Visa</p>"
        "<p>Your total for this transaction: COL$ 45.415</p></body></html>"
    )
    raw = _alternative(MIMEText(text_plain, "plain", "utf-8"), MIMEText(html, "html", "utf-8"))
    record = _parse(raw)
    assert record.payload["body_source"] == "html_stripped"
    assert "Outer Wilds" in record.payload["body_text"]
    assert "COL$ 45.415" in record.payload["body_text"]
    assert "7:18pm -05" in record.payload["body_text"]


def test_alt_de_las_imagenes_entra_al_cuerpo() -> None:
    """El boletín que lleva todo en imágenes: el `alt` es lo único que dice qué es y cuándo.

    Caso real (FIB, 2026-09-14): el texto plano decía «Te invitamos a nuestro bootcamp virtual» y
    nada más; el nombre y las fechas estaban en tres PNG remotos.
    """
    html = (
        "<html><body>"
        '<img src="h.png" alt="Logo">'
        '<img src="a.png" alt="Bootcamp Web3 Esenciales | 22 y 23 de septiembre">'
        '<img src="a.png" alt="Bootcamp Web3 Esenciales | 22 y 23 de septiembre">'
        "<p>Te invitamos a nuestro bootcamp virtual</p>"
        '<img src="b.png" alt="Postulate aqui: cierre 20 de septiembre">'
        '<img src="px.gif" alt="">'
        "</body></html>"
    )
    raw = _alternative(
        MIMEText("Te invitamos a nuestro bootcamp virtual", "plain", "utf-8"),
        MIMEText(html, "html", "utf-8"),
    )
    cuerpo = _parse(raw).payload["body_text"]
    assert "Bootcamp Web3 Esenciales | 22 y 23 de septiembre" in cuerpo
    assert "Postulate aqui: cierre 20 de septiembre" in cuerpo
    assert cuerpo.count("Bootcamp Web3 Esenciales") == 1, "el alt repetido seguido no se duplica"
    assert "Logo" not in cuerpo, "el rotulo generico de la plantilla no es contenido"


def test_el_alt_hace_que_gane_el_html_cuando_el_texto_plano_es_pobre() -> None:
    """Con el `alt` adentro, el HTML pasa a tener contenido material y el criterio de stub que ya
    existía lo prefiere sobre un texto plano que no dice nada."""
    alts = "".join(
        f'<img src="{i}.png" alt="Sesion {i}: charla del 2{i} de octubre, cupo limitado">'
        for i in range(1, 6)
    )
    raw = _alternative(
        MIMEText("Te invitamos a nuestro bootcamp virtual", "plain", "utf-8"),
        MIMEText(f"<html><body>{alts}</body></html>", "html", "utf-8"),
    )
    record = _parse(raw)
    assert record.payload["body_source"] == "html_stripped"
    assert "Sesion 3" in record.payload["body_text"]


def test_el_alt_dentro_de_script_o_head_no_entra() -> None:
    html = (
        '<html><head><img src="x.png" alt="cabecera que no es contenido"></head>'
        "<body><p>cuerpo real</p></body></html>"
    )
    raw = _alternative(MIMEText("   ", "plain"), MIMEText(html, "html", "utf-8"))
    cuerpo = _parse(raw).payload["body_text"]
    assert "cuerpo real" in cuerpo and "cabecera que no es contenido" not in cuerpo
