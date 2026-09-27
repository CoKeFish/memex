from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from memex.db import connection


def _seed_consolidated(
    user_id: int,
    *,
    direction: str = "egreso",
    amount: float = 100.0,
    currency: str = "MXN",
    category: str = "comida",
    counterparty: str = "OXXO",
    place: str = "",
    occurred_at: datetime = datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    precision: str = "datetime",
    deleted: bool = False,
    status: str = "completada",
    kind: str = "pago",
) -> None:
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO mod_finance_consolidated
                  (user_id, direction, status, kind, amount, currency, category, counterparty,
                   place, occurred_at, occurred_at_precision, description, deleted)
                VALUES
                  (:uid, :dir, :status, :kind, :amt, :cur, :cat, :cp, :place, :at, :prec, '',
                   :deleted)
                """
            ),
            {
                "uid": user_id,
                "dir": direction,
                "status": status,
                "kind": kind,
                "amt": amount,
                "cur": currency,
                "cat": category,
                "cp": counterparty,
                "place": place,
                "at": occurred_at,
                "prec": precision,
                "deleted": deleted,
            },
        )


def test_list_transactions_returns_user_rows(client: Any) -> None:
    _seed_consolidated(1, amount=42.5, currency="MXN", direction="ingreso")
    _seed_consolidated(1, amount=10.0, currency="USD")
    r = client.get("/finance/transactions")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is None
    first = body["items"][0]
    assert first["amount"] == 42.5  # NUMERIC → float
    assert isinstance(first["amount"], float)
    assert first["direction"] == "ingreso"
    assert first["currency"] == "MXN"
    assert first["occurred_at_precision"] == "datetime"


def test_list_transactions_cross_tenant_scoped(client: Any, seed_user2: int) -> None:
    _seed_consolidated(1, counterparty="mine")
    _seed_consolidated(seed_user2, counterparty="theirs")
    items = client.get("/finance/transactions").json()["items"]
    assert len(items) == 1
    assert items[0]["counterparty"] == "mine"


def test_list_transactions_excludes_deleted(client: Any) -> None:
    _seed_consolidated(1, counterparty="vivo")
    _seed_consolidated(1, counterparty="tombstone", deleted=True)
    items = client.get("/finance/transactions").json()["items"]
    assert len(items) == 1
    assert items[0]["counterparty"] == "vivo"


def test_list_transactions_filter_by_currency(client: Any) -> None:
    _seed_consolidated(1, currency="MXN", amount=1.0)
    _seed_consolidated(1, currency="USD", amount=2.0)
    _seed_consolidated(1, currency="USD", amount=3.0)
    assert len(client.get("/finance/transactions?currency=USD").json()["items"]) == 2
    assert len(client.get("/finance/transactions?currency=MXN").json()["items"]) == 1


def test_list_transactions_filter_by_direction(client: Any) -> None:
    _seed_consolidated(1, direction="ingreso", amount=1.0)
    _seed_consolidated(1, direction="egreso", amount=2.0)
    assert len(client.get("/finance/transactions?direction=ingreso").json()["items"]) == 1


def test_list_transactions_filter_by_date_range(client: Any) -> None:
    _seed_consolidated(1, occurred_at=datetime(2026, 3, 15, tzinfo=UTC))
    _seed_consolidated(1, occurred_at=datetime(2026, 4, 15, tzinfo=UTC))
    _seed_consolidated(1, occurred_at=datetime(2026, 5, 15, tzinfo=UTC))
    items = client.get("/finance/transactions?since=2026-04-01&until=2026-05-01").json()["items"]
    assert len(items) == 1


def test_list_transactions_pagination(client: Any) -> None:
    for i in range(5):
        _seed_consolidated(1, amount=float(100 + i))
    body1 = client.get("/finance/transactions?limit=2").json()
    assert len(body1["items"]) == 2
    assert body1["next_cursor"] is not None
    body2 = client.get(f"/finance/transactions?limit=2&cursor={body1['next_cursor']}").json()
    assert len(body2["items"]) == 2
    assert body2["items"][0]["id"] > body1["items"][-1]["id"]


def test_list_transactions_empty(client: Any) -> None:
    body = client.get("/finance/transactions").json()
    assert body == {"items": [], "next_cursor": None}


def test_list_transactions_exposes_evidence_and_source_inbox_ids(client: Any) -> None:
    """`evidence` (de la cruda GANADORA) y `source_inbox_ids` (unión de los mensajes de las crudas
    enlazadas) se traen con el join — no son columnas de `mod_finance_consolidated`."""
    with connection() as c:
        txn_id = c.execute(
            text(
                """
                INSERT INTO mod_finance_transactions
                  (user_id, source_inbox_ids, direction, amount, currency, occurred_at, evidence)
                VALUES (1, ARRAY[7, 9]::bigint[], 'egreso', 100, 'MXN', :at, 'pago OXXO $100')
                RETURNING id
                """
            ),
            {"at": datetime(2026, 5, 10, 12, 0, tzinfo=UTC)},
        ).scalar_one()
        cons_id = c.execute(
            text(
                """
                INSERT INTO mod_finance_consolidated
                  (user_id, direction, amount, currency, occurred_at, winner_transaction_id)
                VALUES (1, 'egreso', 100, 'MXN', :at, :w)
                RETURNING id
                """
            ),
            {"at": datetime(2026, 5, 10, 12, 0, tzinfo=UTC), "w": txn_id},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO mod_finance_transaction_links "
                "(user_id, consolidated_id, transaction_id) VALUES (1, :c, :t)"
            ),
            {"c": cons_id, "t": txn_id},
        )
    item = client.get("/finance/transactions").json()["items"][0]
    assert item["evidence"] == "pago OXXO $100"
    assert sorted(item["source_inbox_ids"]) == [7, 9]


def test_list_transactions_evidence_defaults_without_raw(client: Any) -> None:
    """Una fila consolidada SIN winner ni links (sembrada directa) degrada limpio: '' / []."""
    _seed_consolidated(1, counterparty="solo-consolidado")
    item = client.get("/finance/transactions").json()["items"][0]
    assert item["evidence"] == ""
    assert item["source_inbox_ids"] == []


# ----- status / kind (0090) ------------------------------------------------------- #


def test_list_transactions_defaults_to_settled_only(client: Any) -> None:
    _seed_consolidated(1, counterparty="OK")
    _seed_consolidated(1, counterparty="FAIL", status="fallida")
    _seed_consolidated(1, counterparty="DUE", status="pendiente")
    r = client.get("/finance/transactions")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["counterparty"] for i in items] == ["OK"]
    assert (items[0]["status"], items[0]["kind"]) == ("completada", "pago")


def test_list_transactions_status_all_and_specific(client: Any) -> None:
    _seed_consolidated(1, counterparty="OK")
    _seed_consolidated(1, counterparty="FAIL", status="fallida")
    assert len(client.get("/finance/transactions?status=all").json()["items"]) == 2
    only_failed = client.get("/finance/transactions?status=fallida").json()["items"]
    assert [i["counterparty"] for i in only_failed] == ["FAIL"]


def test_list_transactions_filter_by_kind(client: Any) -> None:
    _seed_consolidated(1, counterparty="Tienda", direction="egreso")
    _seed_consolidated(1, counterparty="Tienda", direction="ingreso", kind="reembolso")
    refunds = client.get("/finance/transactions?kind=reembolso").json()["items"]
    assert len(refunds) == 1
    assert (refunds[0]["direction"], refunds[0]["kind"]) == ("ingreso", "reembolso")


def _seed_subscription(
    user_id: int,
    *,
    provider: str = "Nubix",
    amount: float = 29.11,
    currency: str = "USD",
    recurrence: str = "RRULE:FREQ=MONTHLY;BYMONTHDAY=1",
    status: str = "activa",
    next_renewal: str = "2026-10-01",
) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    """
                    INSERT INTO mod_finance_subscriptions
                      (user_id, provider, provider_key, currency, amount_typical, amount_varies,
                       recurrence, next_renewal_on, status, source, confidence, charges)
                    VALUES (:uid, :p, :k, :cur, :amt, FALSE, ARRAY[:rule], CAST(:next AS DATE),
                            :status, 'inferida', 0.83, 6)
                    RETURNING id
                    """
                ),
                {
                    "uid": user_id,
                    "p": provider,
                    "k": provider.lower(),
                    "cur": currency,
                    "amt": amount,
                    "rule": recurrence,
                    "next": next_renewal,
                    "status": status,
                },
            ).scalar_one()
        )


def test_list_subscriptions_with_monthly_commitment(client: Any) -> None:
    """Las que se repiten, con el compromiso mensual por moneda: lo anual se divide por 12 y las
    monedas no se mezclan."""
    _seed_subscription(1)
    _seed_subscription(1, provider="Dominio", amount=1200.0, recurrence="RRULE:FREQ=YEARLY")
    _seed_subscription(1, provider="Telegram", amount=18500.0, currency="COP")
    _seed_subscription(1, provider="Cancelada", amount=999.0, status="cancelada")
    body = client.get("/finance/subscriptions").json()
    assert {i["provider"] for i in body["items"]} == {"Nubix", "Dominio", "Telegram"}
    nubix = next(i for i in body["items"] if i["provider"] == "Nubix")
    assert nubix["amount_typical"] == 29.11
    assert nubix["cadence_human"]
    assert nubix["next_renewal_on"] == "2026-10-01"
    por_moneda = {c["currency"]: c for c in body["monthly_commitment"]}
    assert por_moneda["USD"]["monthly"] == 129.11  # 29,11 mensual + 1.200 anual / 12
    assert por_moneda["COP"]["monthly"] == 18500.0
    assert por_moneda["COP"]["subscriptions"] == 1
    con_todas = client.get("/finance/subscriptions?include_inactive=true").json()
    assert "Cancelada" in {i["provider"] for i in con_todas["items"]}


def test_list_subscriptions_cross_tenant_scoped(client: Any, seed_user2: int) -> None:
    _seed_subscription(seed_user2, provider="De otro")
    assert client.get("/finance/subscriptions").json()["items"] == []


def test_from_document_marks_movements_that_came_from_a_statement(client: Any) -> None:
    """Un mes sin extracto está incompleto: la fila dice de dónde vino para poder avisarlo."""
    with connection() as c:
        correo = int(
            c.execute(
                text(
                    "INSERT INTO sources (user_id, name, type) VALUES (1, 'imap-x', 'imap') "
                    "RETURNING id"
                )
            ).scalar_one()
        )
        extracto = int(
            c.execute(
                text(
                    "INSERT INTO sources (user_id, name, type) VALUES (1, 'docs-x', 'documentos') "
                    "RETURNING id"
                )
            ).scalar_one()
        )
        ids = {}
        for etiqueta, src in (("correo", correo), ("extracto", extracto)):
            ids[etiqueta] = int(
                c.execute(
                    text(
                        "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                        "VALUES (1, :s, :e, NOW(), '{}'::jsonb) RETURNING id"
                    ),
                    {"s": src, "e": f"x-{etiqueta}"},
                ).scalar_one()
            )
        por_consolidado = {}
        for etiqueta in ("correo", "extracto"):
            cid = int(
                c.execute(
                    text(
                        "INSERT INTO mod_finance_consolidated (user_id, direction, status, kind, "
                        "amount, currency, category, counterparty, place, occurred_at, "
                        "occurred_at_precision, description) VALUES (1, 'egreso', 'completada', "
                        "'pago', 1000, 'COP', 'otros', :cp, '', NOW(), 'date', '') RETURNING id"
                    ),
                    {"cp": etiqueta},
                ).scalar_one()
            )
            tid = int(
                c.execute(
                    text(
                        "INSERT INTO mod_finance_transactions "
                        "(user_id, direction, amount, currency, category, counterparty, place, "
                        "occurred_at, occurred_at_precision, description, evidence, "
                        "source_inbox_ids) "
                        "VALUES (1, 'egreso', 1000, 'COP', 'otros', :cp, '', NOW(), 'date', "
                        "'', '', ARRAY[:iid]::bigint[]) RETURNING id"
                    ),
                    {"cp": etiqueta, "iid": ids[etiqueta]},
                ).scalar_one()
            )
            c.execute(
                text(
                    "INSERT INTO mod_finance_transaction_links (user_id, consolidated_id, "
                    "transaction_id) VALUES (1, :c, :t)"
                ),
                {"c": cid, "t": tid},
            )
            por_consolidado[etiqueta] = cid
    filas = {i["counterparty"]: i for i in client.get("/finance/transactions").json()["items"]}
    assert filas["extracto"]["from_document"] is True
    assert filas["correo"]["from_document"] is False
    assert filas["extracto"]["source_inbox_ids"] == [ids["extracto"]]
