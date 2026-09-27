"""Integridad del directorio (`identidades/hygiene.py`): dominios que NO identifican a una
organización no entran como identificador `domain` (prevención en `_insert_identifier`) y se
quitan en el barrido. Reglas: free-mail/relay salvo para el proveedor DUEÑO (PayPal/paypal.com);
plataforma compartida = ≥ 2 raíces sin dominante; el dominio institucional lo conserva la raíz
dominante aunque una oficina ajena tenga buzón ahí (FONDEX @salesiana); los emails se conservan."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.modules.identidades.hygiene import (
    domain_rejection_reason,
    domain_usage,
    drop_invalid_domain_identifiers,
    owns_domain,
    run_hygiene,
    shared_platform_domains,
)
from memex.modules.identidades.module import _insert_identifier


def _org(conn: Any, name: str, parent: int | None = None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name, parent_identity_id) "
                "VALUES (1, 'organizacion', :n, :p) RETURNING id"
            ),
            {"n": name, "p": parent},
        ).scalar_one()
    )


def _ident(conn: Any, ident: int, kind: str, value: str) -> None:
    conn.execute(
        text(
            "INSERT INTO mod_identidades_identifiers (user_id, identity_id, platform, kind, value, "
            "value_norm) VALUES (1, :i, :k, :k, :v, :v) ON CONFLICT DO NOTHING"
        ),
        {"i": ident, "k": kind, "v": value},
    )


def _domains(conn: Any, ident: int) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT value_norm FROM mod_identidades_identifiers WHERE identity_id = :i "
                "AND kind = 'domain' ORDER BY 1"
            ),
            {"i": ident},
        ).all()
    ]


def test_owns_domain() -> None:
    assert owns_domain("PayPal", "paypal.com")
    assert owns_domain("Apple", "privaterelay.appleid.com")  # dueño nominal…
    assert owns_domain("Stripe, Inc.", "stripe.com")
    assert owns_domain("Luma", "luma-mail.com")
    assert not owns_domain("Railway", "stripe.com")
    assert not owns_domain("Pontificia Universidad Salesiana", "salesiana.edu.co")  # otra regla
    assert not owns_domain("X", "x.com")  # núcleo < 3 chars no vale por ruido


def test_plataforma_compartida_vs_dominio_institucional(conn: Any) -> None:
    uni = _org(conn, "Universidad X")
    carrera = _org(conn, "Carrera Y", parent=uni)
    fondex = _org(conn, "FONDEX")  # oficina ajena con buzón institucional
    tienda_a = _org(conn, "Tienda A")
    tienda_b = _org(conn, "Tienda B")
    for i, who in enumerate((uni, uni, uni, carrera, carrera)):
        _ident(conn, who, "email", f"buzon{i}@unix.edu")
    _ident(conn, fondex, "email", "fondex@unix.edu")
    _ident(conn, tienda_a, "email", "noreply@tebex.io")
    _ident(conn, tienda_b, "email", "orders@tebex.io")
    use = domain_usage(conn, 1)
    assert use["unix.edu"].dominant == uni and not use["unix.edu"].shared  # 5 de 6 → dominante
    assert use["tebex.io"].dominant is None and use["tebex.io"].shared  # 1 y 1 → plataforma
    assert set(shared_platform_domains(conn, 1)) == {"tebex.io"}
    # la raíz dominante puede tener el dominio; la oficina ajena no; la plataforma nadie
    assert domain_rejection_reason(conn, 1, "unix.edu", identity_id=uni) is None
    assert domain_rejection_reason(conn, 1, "unix.edu", identity_id=carrera) is None  # mismo árbol
    assert domain_rejection_reason(conn, 1, "unix.edu", identity_id=fondex) == "not_dominant_root"
    assert domain_rejection_reason(conn, 1, "tebex.io", identity_id=tienda_a) == "shared_platform"
    assert domain_rejection_reason(conn, 1, "tebex.io") == "shared_platform"
    # relay/free-mail: rechazado salvo para el dueño
    paypal = _org(conn, "PayPal")
    apple = _org(conn, "Apple")
    assert domain_rejection_reason(conn, 1, "paypal.com", identity_id=tienda_a) == "relay"
    assert domain_rejection_reason(conn, 1, "paypal.com", identity_id=paypal) is None
    # …pero un relay PURO no se atribuye ni al dueño: los correos desde ahí son de terceros
    assert domain_rejection_reason(conn, 1, "appleid.com", identity_id=apple) == "relay"
    assert domain_rejection_reason(conn, 1, "gmail.com", identity_id=tienda_a) == "freemail"
    assert domain_rejection_reason(conn, 1, "notify.stripe.com") == "relay"  # registrable


def test_prevencion_y_barrido(conn: Any) -> None:
    uni = _org(conn, "Universidad X")
    tienda_a = _org(conn, "Tienda A")
    tienda_b = _org(conn, "Tienda B")
    paypal = _org(conn, "PayPal")
    _ident(conn, tienda_a, "email", "noreply@tebex.io")
    _ident(conn, tienda_b, "email", "orders@tebex.io")
    # prevención: el escritor no cuelga plataforma ni free-mail; sí el propio y el institucional
    _insert_identifier(conn, 1, tienda_a, "domain", "domain", "tebex.io", "tebex.io")
    _insert_identifier(conn, 1, tienda_a, "domain", "domain", "gmail.com", "gmail.com")
    _insert_identifier(conn, 1, uni, "domain", "domain", "unix.edu", "unix.edu")
    _insert_identifier(conn, 1, paypal, "domain", "domain", "paypal.com", "paypal.com")
    _insert_identifier(conn, 1, tienda_a, "email", "email", "urban@gmail.com", "urban@gmail.com")
    assert _domains(conn, tienda_a) == []
    assert _domains(conn, uni) == ["unix.edu"] and _domains(conn, paypal) == ["paypal.com"]
    # barrido: lo que entró antes de la regla (insert crudo) se quita con su razón
    _ident(conn, tienda_b, "domain", "tebex.io")
    _ident(conn, tienda_b, "domain", "privaterelay.appleid.com")
    dropped = drop_invalid_domain_identifiers(conn, 1)
    assert {(d.identity_id, d.domain, d.reason) for d in dropped} == {
        (tienda_b, "tebex.io", "shared_platform"),
        (tienda_b, "privaterelay.appleid.com", "relay"),
    }
    assert _domains(conn, tienda_b) == [] and _domains(conn, paypal) == ["paypal.com"]
    # los emails en esos dominios se conservan (identifican al remitente concreto)
    emails = conn.execute(
        text("SELECT count(*) FROM mod_identidades_identifiers WHERE kind = 'email'")
    ).scalar_one()
    assert emails == 3
    st = run_hygiene(conn, 1)
    assert st.domains == [] and st.aliases == []  # idempotente
