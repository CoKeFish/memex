"""API read-only de bienestar: registros / summary / daily / habits (+ TZ inválida)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from memex.db import connection
from memex.modules.bienestar.habits import add_habit
from memex.modules.bienestar.module import register

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def _seed_fixed() -> None:
    """Dos registros el 2026-06-10 (Bogota), uno con event_id. Fechas fijas: las vistas de
    registros/summary/daily no dependen de 'hoy'. bienestar es para hábitos → sembramos los que
    cubren las categorías registradas (comida/ejercicio)."""
    with connection() as c:
        for cat in ("comida", "ejercicio"):
            add_habit(c, 1, name=cat, cadence="daily", category=cat)
        register(
            c,
            1,
            category="comida",
            activity="almuerzo",
            occurred_at=datetime(2026, 6, 10, 17, 0, tzinfo=UTC),
            precision="datetime",
            event_id="E1",
        )
        register(
            c,
            1,
            category="ejercicio",
            activity="gimnasio",
            occurred_at=datetime(2026, 6, 10, 18, 0, tzinfo=UTC),
            precision="datetime",
        )


def test_registros_endpoint(client: TestClient) -> None:
    _seed_fixed()
    res = client.get("/bienestar/registros")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 2
    assert {i["category"] for i in items} == {"comida", "ejercicio"}
    assert any(i["event_id"] == "E1" for i in items)


def test_registros_filter_category(client: TestClient) -> None:
    _seed_fixed()
    res = client.get("/bienestar/registros?category=comida")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["activity"] == "almuerzo"


def test_summary_endpoint(client: TestClient) -> None:
    _seed_fixed()
    body = client.get("/bienestar/summary").json()
    assert body["total"] == 2
    assert body["by_category"] == {"comida": 1, "ejercicio": 1}


def test_daily_endpoint(client: TestClient) -> None:
    _seed_fixed()
    res = client.get("/bienestar/daily?tz=America/Bogota")
    assert res.status_code == 200
    days = res.json()["days"]
    assert len(days) == 1  # ambos el 6/10 Bogota
    assert days[0]["total"] == 2
    assert days[0]["by_category"]["comida"] == 1


def test_habits_endpoint(client: TestClient) -> None:
    # 'hoy' relativo al reloj real: sembramos en now() para que caiga en el período en curso.
    now = datetime.now(UTC)
    with connection() as c:
        add_habit(c, 1, name="Gym", cadence="daily", activity="gimnasio")
        register(
            c, 1, category="ejercicio", activity="gimnasio", occurred_at=now, precision="datetime"
        )
    res = client.get("/bienestar/habits?tz=America/Bogota&periods=7")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["habit"]["name"] == "Gym"
    assert items[0]["current"] >= 1


def test_invalid_tz_422(client: TestClient) -> None:
    assert client.get("/bienestar/daily?tz=Nowhere/Nope").status_code == 422


def test_create_habit_endpoint(client: TestClient) -> None:
    res = client.post(
        "/bienestar/habits",
        json={"name": "Gimnasio", "cadence": "weekly", "target_count": 3, "activity": "gimnasio"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["name"] == "Gimnasio"
    assert body["cadence"] == "weekly"
    assert body["target_count"] == 3
    assert isinstance(body["id"], int)
    # aparece en el GET de hábitos
    items = client.get("/bienestar/habits?tz=America/Bogota&periods=7").json()["items"]
    assert any(i["habit"]["name"] == "Gimnasio" for i in items)


def test_create_habit_without_activity_or_category_422(client: TestClient) -> None:
    # el dominio exige activity O category → 422
    res = client.post("/bienestar/habits", json={"name": "Vacío", "cadence": "daily"})
    assert res.status_code == 422


def test_delete_habit_endpoint(client: TestClient) -> None:
    created = client.post(
        "/bienestar/habits", json={"name": "Yoga", "cadence": "daily", "activity": "yoga"}
    )
    habit_id = created.json()["id"]
    res = client.delete(f"/bienestar/habits/{habit_id}")
    assert res.status_code == 200
    assert res.json() == {"deleted": True}
    items = client.get("/bienestar/habits?tz=America/Bogota").json()["items"]
    assert all(i["habit"]["id"] != habit_id for i in items)


def test_delete_habit_not_found_404(client: TestClient) -> None:
    assert client.delete("/bienestar/habits/99999").status_code == 404
