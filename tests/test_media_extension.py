"""extension_for: extensión normalizada del adjunto desde filename (o content_type) — puro."""

from __future__ import annotations

import pytest

from memex.core.media import extension_for


@pytest.mark.parametrize(
    ("filename", "content_type", "expected"),
    [
        ("factura.PDF", "application/pdf", "pdf"),  # del filename, lowercase
        ("foto.jpeg", "image/jpeg", "jpeg"),
        ("backup.tar.gz", "application/gzip", "gz"),  # último segmento
        (None, "image/png", "png"),  # sin nombre → del content_type
        ("sinpunto", "application/zip", "zip"),  # sin extensión en el nombre → content_type
        ("raro.weird?name", "application/x-unknown-xyz", None),  # no alfanumérico y ct desconocido
    ],
)
def test_extension_for(filename: str | None, content_type: str, expected: str | None) -> None:
    assert extension_for(filename, content_type) == expected
