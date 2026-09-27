"""unpack_zip: clasificación de entradas, pool de contraseñas y topes anti-zip-bomb (puro)."""

from __future__ import annotations

import pytest

from memex.ocr.zip import ZipCaps, ZipCorruptError, ZipEncryptedError, unpack_zip
from tests.ocr._pdf_fixtures import digital_pdf, make_png, make_zip


def test_classifies_supported_entries_and_skips_rest() -> None:
    z = make_zip(
        {
            "a.png": make_png(80, (0.2, 0.2, 0.6)),
            "b.pdf": digital_pdf(),
            "c.txt": b"nota plana",
            "d.exe": b"binario",
            "nested.zip": make_zip({"x.txt": b"y"}),  # sin recursión → se saltea
        }
    )
    up = unpack_zip(z, caps=ZipCaps())
    kinds = {e.name: e.kind for e in up.entries}
    assert kinds == {"a.png": "image", "b.pdf": "pdf", "c.txt": "text"}
    assert set(up.skipped) == {"d.exe", "nested.zip"}
    assert up.truncated is False


def test_image_entry_carries_content_type() -> None:
    up = unpack_zip(make_zip({"a.jpeg": make_png(80, (0.1, 0.5, 0.5))}), caps=ZipCaps())
    img = up.entries[0]
    assert img.kind == "image"
    assert img.content_type == "image/jpeg"
    assert img.data is not None


def test_text_entry_decoded_utf8() -> None:
    up = unpack_zip(make_zip({"c.txt": "hola café\n".encode()}), caps=ZipCaps())
    assert up.entries[0].kind == "text"
    assert up.entries[0].text is not None and "café" in up.entries[0].text


def test_encrypted_unlocked_with_password_pool() -> None:
    z = make_zip({"s.txt": b"protegido"}, password="docID42")
    up = unpack_zip(z, caps=ZipCaps(), passwords=("wrong", "docID42"))
    assert up.entries[0].text == "protegido"


def test_encrypted_wrong_pool_raises() -> None:
    z = make_zip({"s.txt": b"protegido"}, password="docID42")
    with pytest.raises(ZipEncryptedError):
        unpack_zip(z, caps=ZipCaps(), passwords=("nope",))


def test_encrypted_no_pool_raises() -> None:
    z = make_zip({"s.txt": b"protegido"}, password="docID42")
    with pytest.raises(ZipEncryptedError):
        unpack_zip(z, caps=ZipCaps())


def test_max_entries_truncates() -> None:
    z = make_zip({f"f{i}.txt": b"x" for i in range(5)})
    up = unpack_zip(z, caps=ZipCaps(max_entries=2))
    assert len(up.entries) == 2
    assert up.truncated is True


def test_entry_over_cap_skipped() -> None:
    z = make_zip({"big.txt": b"x" * 5000, "ok.txt": b"chico"})
    up = unpack_zip(z, caps=ZipCaps(max_entry_bytes=1000))
    assert [e.name for e in up.entries] == ["ok.txt"]
    assert "big.txt" in up.skipped


def test_total_cap_truncates() -> None:
    z = make_zip({"a.txt": b"x" * 800, "b.txt": b"y" * 800})
    up = unpack_zip(z, caps=ZipCaps(max_total_bytes=1000))
    assert len(up.entries) == 1  # la 1ª entra (800<=1000); la 2ª excedería → corte
    assert up.truncated is True


def test_corrupt_bytes_raise() -> None:
    with pytest.raises(ZipCorruptError):
        unpack_zip(b"esto no es un ZIP", caps=ZipCaps())
