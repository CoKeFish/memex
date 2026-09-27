"""Rubro por contraparte y reclasificación de «otros».

Dos piezas, una detrás de la otra:

1. **Regla por contraparte** (`mod_finance_counterparty_rules`): «Nacanisaje es un restaurante».
   La palabra del dueño (`source='user'`) o la clasificación del modelo (`source='llm'`), por
   contraparte normalizada (`memex_norm`, la misma llave de identidades). Se aplica al insertar
   (extracción y `register`), al consolidar y hacia atrás sobre lo ya cargado. El dueño pisa al
   modelo; el modelo nunca pisa al dueño.

2. **Reclasificación** (`recategorize`): relee lo que quedó en «otros» agrupado por contraparte
   Y descripción sin montos (un banco o una billetera tiene recargas, intereses, gravámenes y
   comisiones: no son lo mismo), resuelve determinista lo obvio (una recarga desde el propio
   banco es un traspaso, «intereses» son rendimientos) y le pregunta al modelo el resto en
   lotes, con la guía de rubros. Cada decisión se aplica a las filas de SU grupo. La regla
   `llm` por contraparte (para que el próximo cargo del mismo comercio salga bien de una) solo
   queda cuando todo lo de esa contraparte dice lo mismo; una contraparte heterogénea no gana
   regla. «Otros» sigue siendo «otros» solo cuando de verdad no se sabe.

Prompt por categorías, sin marcas ni casos del dueño.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.observability import record_llm_call
from memex.db import connection
from memex.llm import ChatMessage, LLMClient
from memex.llm.registry import aclose_llm, build_llm_client
from memex.logging import get_logger
from memex.modules.finance.schema import FINANCE_CATEGORIES
from memex.modules.identidades.normalize import normalize_match

_log = get_logger("memex.modules.finance.rubros")

PURPOSE = "finance_categorize"
SOURCE_USER = "user"
SOURCE_LLM = "llm"
OTROS = "otros"
MIN_CONFIDENCE = 0.7
BATCH = 60
_MAX_TOKENS = 3000

#: Qué es cada rubro. La comparte el extractor (prompt de finanzas) y la reclasificación, así
#: los dos clasifican igual. Categorías, no marcas.
CATEGORY_GUIDE = (
    "- comida: restaurantes, cafés, panaderías, mercado y supermercado, domicilios de comida.\n"
    "- transporte: taxis y apps de transporte, buses, peajes, gasolina, parqueaderos, vuelos.\n"
    "- software: suscripciones y compras de software, nube, APIs, dominios, herramientas de\n"
    "  desarrollo, tiendas de apps y de juegos para desarrollar.\n"
    "- servicios: servicios públicos, telefonía e internet, recargas y paquetes de celular,\n"
    "  seguros, comisiones bancarias, trámites y tasas.\n"
    "- educacion: matrículas, cursos, certificaciones, libros de estudio.\n"
    "- salud: médicos, farmacia, gimnasio, EPS, exámenes.\n"
    "- entretenimiento: streaming, videojuegos, cine, conciertos, bares, salidas, eventos.\n"
    "- compras: tiendas y productos físicos (ropa, hogar, tecnología, papelería, regalos).\n"
    "- transferencia: plata enviada a o recibida de una PERSONA (transferencias, llaves,\n"
    "  envíos entre cuentas de personas) sin un comercio de por medio.\n"
    "- traspaso: movimiento entre cuentas o billeteras de la MISMA persona (recarga de la\n"
    "  billetera desde su banco, envío a su otra cuenta).\n"
    "- ingreso: plata recibida por trabajo o servicios prestados, de una organización, beca,\n"
    "  premio o pago de un cliente.\n"
    "- rendimientos: intereses, rendimientos, dividendos, devoluciones por cashback.\n"
    "- otros: SOLO cuando no se sabe qué es el comercio ni el motivo del movimiento."
)

RECATEGORIZE_SYSTEM_PROMPT = (
    "Sos un clasificador de RUBROS de movimientos de plata personales. Te doy una lista de\n"
    "contrapartes (comercios, personas, organizaciones) con una descripción de ejemplo y el\n"
    "sentido del movimiento; todas están hoy en «otros». Para cada una decidí el rubro con esta\n"
    f"guía:\n{CATEGORY_GUIDE}\n\n"
    "Reglas:\n"
    "- Usá lo que sabés del comercio o la plataforma si el nombre es reconocible; si el nombre no\n"
    "  dice nada (una razón social desconocida, iniciales), respondé 'otros' con confianza baja.\n"
    "  NO adivines.\n"
    "- Un nombre de PERSONA con «transferencia», «envío», «recibido» o «ingreso de» es\n"
    "  'transferencia', salvo que la descripción diga que es pago por trabajo ('ingreso').\n"
    "- Una organización que PAGA a la persona es 'ingreso'.\n"
    "- `confianza`: 0.0-1.0, qué tan seguro estás.\n\n"
    'Respondé SOLO con JSON: {"rubros": [{"k": <número de la fila>, "rubro": "<rubro>",'
    ' "confianza": 0.0-1.0}]}'
)

#: Lo obvio no pasa por el modelo: la descripción lo dice.
_DETERMINISTIC: tuple[tuple[re.Pattern[str], str], ...] = (
    # la billetera se carga desde el propio banco; sacar efectivo es mover plata propia
    (re.compile(r"^recarga desde\b|^retiro en cajero\b"), "traspaso"),
    (re.compile(r"\bintereses\b|\brendimientos\b"), "rendimientos"),
    # plata que llega o sale por llave, Bre-B o Transfiya: es de o para una persona
    (
        re.compile(
            r"^recibi a mi llave|^recibido por bre-?b|^envio con bre-?b|^envio de .* por bre-?b"
            r"|^envio transfiya|^envio por bre-?b"
        ),
        "transferencia",
    ),
)
_DIGITS_RE = re.compile(r"[\d.,$]+")
_PUNCT_RE = re.compile(r"[^\w\s]+")


# --- reglas ----------------------------------------------------------------------------- #


def norm(counterparty: str) -> str:
    return normalize_match(counterparty or "")


def rules_map(conn: Connection, user_id: int) -> dict[str, str]:
    """contraparte normalizada → rubro (todas las reglas del user, user y llm)."""
    rows = conn.execute(
        text(
            "SELECT counterparty_norm, category FROM mod_finance_counterparty_rules "
            "WHERE user_id = :u"
        ),
        {"u": user_id},
    ).all()
    return {str(r[0]): str(r[1]) for r in rows}


def with_rule(rules: Mapping[str, str], counterparty: str, category: str) -> str:
    """El rubro que manda: la regla de la contraparte si hay; si no, el que vino."""
    if not counterparty:
        return category
    return rules.get(norm(counterparty), category)


def rule_for(conn: Connection, user_id: int, counterparty: str) -> str | None:
    if not counterparty.strip():
        return None
    row = conn.execute(
        text(
            "SELECT category FROM mod_finance_counterparty_rules "
            "WHERE user_id = :u AND counterparty_norm = :n"
        ),
        {"u": user_id, "n": norm(counterparty)},
    ).first()
    return str(row[0]) if row else None


def list_rules(conn: Connection, user_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT id, counterparty, category, source, note, created_at, updated_at "
            "FROM mod_finance_counterparty_rules WHERE user_id = :u "
            "ORDER BY lower(counterparty)"
        ),
        {"u": user_id},
    ).mappings()
    return [dict(r) for r in rows]


def set_rule(
    conn: Connection,
    user_id: int,
    counterparty: str,
    category: str,
    *,
    source: str = SOURCE_USER,
    note: str = "",
) -> dict[str, Any] | None:
    """Crea o actualiza la regla. `user` pisa cualquier cosa; `llm` no toca una regla `user`
    (devuelve None sin cambiar nada). Lanza ValueError con contraparte vacía o rubro inválido."""
    cp = " ".join((counterparty or "").split())
    if not cp:
        raise ValueError("la contraparte está vacía")
    cat = (category or "").strip().lower()
    if cat not in FINANCE_CATEGORIES:
        raise ValueError(f"rubro inválido: {category!r}; válidos: {', '.join(FINANCE_CATEGORIES)}")
    if source not in (SOURCE_USER, SOURCE_LLM):
        raise ValueError(f"source inválido: {source!r}")
    key = norm(cp)
    existing = conn.execute(
        text(
            "SELECT id, source FROM mod_finance_counterparty_rules "
            "WHERE user_id = :u AND counterparty_norm = :n"
        ),
        {"u": user_id, "n": key},
    ).first()
    if existing is not None and source == SOURCE_LLM and str(existing[1]) == SOURCE_USER:
        return None
    row = (
        conn.execute(
            text(
                """
            INSERT INTO mod_finance_counterparty_rules
                (user_id, counterparty_norm, counterparty, category, source, note)
            VALUES (:u, :n, :cp, :c, :s, :note)
            ON CONFLICT (user_id, counterparty_norm) DO UPDATE
               SET counterparty = EXCLUDED.counterparty, category = EXCLUDED.category,
                   source = EXCLUDED.source, note = EXCLUDED.note, updated_at = NOW()
            RETURNING id, counterparty, category, source, note, created_at, updated_at
            """
            ),
            {"u": user_id, "n": key, "cp": cp, "c": cat, "s": source, "note": note or ""},
        )
        .mappings()
        .one()
    )
    return dict(row)


def remove_rule(conn: Connection, user_id: int, counterparty: str) -> bool:
    res = conn.execute(
        text(
            "DELETE FROM mod_finance_counterparty_rules "
            "WHERE user_id = :u AND counterparty_norm = :n"
        ),
        {"u": user_id, "n": norm(counterparty)},
    )
    return bool(res.rowcount)


def apply_rule(conn: Connection, user_id: int, counterparty: str, category: str) -> int:
    """Pone el rubro a TODO movimiento de esa contraparte (crudas y consolidadas). Devuelve
    cuántos consolidados cambiaron."""
    key = norm(counterparty)
    conn.execute(
        text(
            "UPDATE mod_finance_transactions SET category = :c "
            "WHERE user_id = :u AND memex_norm(counterparty) = :n AND category <> :c"
        ),
        {"u": user_id, "n": key, "c": category},
    )
    res = conn.execute(
        text(
            "UPDATE mod_finance_consolidated SET category = :c, updated_at = NOW() "
            "WHERE user_id = :u AND NOT deleted AND memex_norm(counterparty) = :n "
            "AND category <> :c"
        ),
        {"u": user_id, "n": key, "c": category},
    )
    return int(res.rowcount or 0)


def apply_all(conn: Connection, user_id: int) -> int:
    """Re-aplica todas las reglas (mantenimiento). Devuelve consolidados cambiados."""
    total = 0
    for cp_norm, cat in rules_map(conn, user_id).items():
        total += apply_rule(conn, user_id, cp_norm, cat)
    return total


# --- reclasificación ------------------------------------------------------------------- #


@dataclass
class Grupo:
    """Movimientos en «otros» con la misma contraparte Y la misma descripción (sin montos). Sin
    contraparte, la descripción sola."""

    key: str
    counterparty: str
    descriptions: list[str]
    ingresos: int = 0
    egresos: int = 0
    cons_ids: list[int] = field(default_factory=list)
    tx_ids: list[int] = field(default_factory=list)
    rubro: str | None = None
    confianza: float = 0.0
    por: str = ""  # 'regla' | 'determinista' | 'modelo' | ''

    @property
    def n(self) -> int:
        return self.ingresos + self.egresos

    @property
    def sentido(self) -> str:
        if self.ingresos and self.egresos:
            return f"mixto ({self.ingresos} ingresos, {self.egresos} egresos)"
        return "ingreso" if self.ingresos else "egreso"


@dataclass
class RecategorizeStats:
    grupos: int = 0
    movimientos: int = 0
    por_regla: int = 0
    deterministas: int = 0
    por_modelo: int = 0
    sin_cambio: int = 0
    consolidados_cambiados: int = 0
    llamadas: int = 0
    cost_usd: float = 0.0
    dry_run: bool = False
    decisiones: list[dict[str, Any]] = field(default_factory=list)


def _desc_key(description: str) -> str:
    """Descripción sin montos ni números: «Envío de 30.000 COP por Bre-B a X» y «Envío de 80.000
    COP por Bre-B a Y» son el mismo grupo."""
    limpio = _PUNCT_RE.sub(" ", _DIGITS_RE.sub(" ", description or ""))
    return normalize_match(limpio)[:80]


def load_groups(conn: Connection, user_id: int, *, only: Sequence[str] = (OTROS,)) -> list[Grupo]:
    rows = conn.execute(
        text(
            "SELECT id, winner_transaction_id, direction, counterparty, description "
            "FROM mod_finance_consolidated "
            "WHERE user_id = :u AND NOT deleted AND category = ANY(:cats) ORDER BY id"
        ),
        {"u": user_id, "cats": list(only)},
    ).mappings()
    groups: dict[str, Grupo] = {}
    for r in rows:
        cp = " ".join(str(r["counterparty"] or "").split())
        desc = " ".join(str(r["description"] or "").split())
        dk = _desc_key(desc)
        if not cp and not dk:
            continue  # ni contraparte ni descripción: no hay con qué clasificar
        key = f"cp:{norm(cp)}|{dk}" if cp else f"desc:{dk}"
        g = groups.get(key)
        if g is None:
            g = groups[key] = Grupo(key=key, counterparty=cp, descriptions=[])
        if desc and desc not in g.descriptions and len(g.descriptions) < 2:
            g.descriptions.append(desc)
        if r["direction"] == "ingreso":
            g.ingresos += 1
        else:
            g.egresos += 1
        g.cons_ids.append(int(r["id"]))
        if r["winner_transaction_id"] is not None:
            g.tx_ids.append(int(r["winner_transaction_id"]))
    return sorted(groups.values(), key=lambda g: (-g.n, g.key))


def decide_deterministic(g: Grupo) -> str | None:
    for d in g.descriptions:
        low = normalize_match(d)
        for rx, cat in _DETERMINISTIC:
            if rx.search(low):
                return cat
    return None


def render_batch(groups: Sequence[Grupo]) -> str:
    lines = []
    for k, g in enumerate(groups, 1):
        who = g.counterparty or "(sin contraparte)"
        desc = " / ".join(g.descriptions) or "(sin descripción)"
        lines.append(
            f"{k}. contraparte: {who} · descripción: {desc} · sentido: {g.sentido} · {g.n} mov."
        )
    return "\n".join(lines)


def parse_batch(content: str, size: int) -> dict[int, tuple[str, float]]:
    """k → (rubro, confianza). Tolerante: filas rotas o fuera de rango se ignoran."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {}
    if isinstance(data, list):
        data = {"rubros": data}
    if not isinstance(data, dict):
        return {}
    out: dict[int, tuple[str, float]] = {}
    for it in data.get("rubros") or []:
        if not isinstance(it, dict):
            continue
        try:
            k = int(str(it.get("k", "")))
            conf = float(it.get("confianza", 0.0))
        except (TypeError, ValueError):
            continue
        rubro = str(it.get("rubro") or "").strip().lower()
        if 1 <= k <= size and rubro in FINANCE_CATEGORIES:
            out[k] = (rubro, max(0.0, min(1.0, conf)))
    return out


def _otras_categorias(
    conn: Connection, user_id: int, counterparty: str, only: Sequence[str]
) -> set[str]:
    """Rubros que ya tienen los movimientos de esa contraparte FUERA de lo releído."""
    rows = conn.execute(
        text(
            "SELECT DISTINCT category FROM mod_finance_consolidated "
            "WHERE user_id = :u AND NOT deleted AND memex_norm(counterparty) = :n "
            "AND NOT (category = ANY(:only))"
        ),
        {"u": user_id, "n": norm(counterparty), "only": list(only)},
    ).all()
    return {str(r[0]) for r in rows}


def _apply_group(conn: Connection, user_id: int, g: Grupo, category: str) -> int:
    """Aplica el rubro a las filas del grupo (crudas ganadoras y consolidadas), y solo a esas."""
    conn.execute(
        text("UPDATE mod_finance_transactions SET category = :c WHERE id = ANY(:ids)"),
        {"c": category, "ids": g.tx_ids},
    )
    res = conn.execute(
        text(
            "UPDATE mod_finance_consolidated SET category = :c, updated_at = NOW() "
            "WHERE id = ANY(:ids) AND category <> :c"
        ),
        {"c": category, "ids": g.cons_ids},
    )
    return int(res.rowcount or 0)


async def recategorize(
    user_id: int,
    *,
    only: Sequence[str] = (OTROS,),
    dry_run: bool = False,
    llm: LLMClient | None = None,
    batch: int = BATCH,
) -> RecategorizeStats:
    """Relee lo que está en `only` (default «otros») y lo reclasifica. Orden: regla existente →
    determinista → modelo. Con `dry_run` decide pero no escribe. `llm` reemplaza al cliente del
    consumer (tests)."""
    stats = RecategorizeStats(dry_run=dry_run)
    with connection() as conn:
        groups = load_groups(conn, user_id, only=only)
        rules = rules_map(conn, user_id)
    stats.grupos = len(groups)
    stats.movimientos = sum(g.n for g in groups)

    pendientes: list[Grupo] = []
    for g in groups:
        if g.counterparty and norm(g.counterparty) in rules:
            g.rubro, g.confianza, g.por = rules[norm(g.counterparty)], 1.0, "regla"
            continue
        det = decide_deterministic(g)
        if det:
            g.rubro, g.confianza, g.por = det, 1.0, "determinista"
            continue
        pendientes.append(g)

    if pendientes:
        client = llm
        own = client is None
        try:
            if client is None:
                client = build_llm_client(PURPOSE, user_id=user_id)
            for i in range(0, len(pendientes), batch):
                lote = pendientes[i : i + batch]
                msgs = [
                    ChatMessage("system", RECATEGORIZE_SYSTEM_PROMPT),
                    ChatMessage("user", render_batch(lote)),
                ]
                result = await client.complete(
                    msgs, response_format="json_object", temperature=0.0, max_tokens=_MAX_TOKENS
                )
                stats.llamadas += 1
                stats.cost_usd += float(result.cost_usd)
                record_llm_call(
                    user_id=user_id,
                    purpose=PURPOSE,
                    model=result.model,
                    prompt_tokens=result.usage.prompt_tokens,
                    completion_tokens=result.usage.completion_tokens,
                    cost_usd=result.cost_usd,
                    latency_ms=result.latency_ms,
                    status="ok",
                    cache_hit_tokens=result.usage.cache_hit_tokens,
                    metadata={"grupos": len(lote), "dry_run": dry_run},
                )
                for k, (rubro, conf) in parse_batch(result.content, len(lote)).items():
                    g = lote[k - 1]
                    g.rubro, g.confianza, g.por = rubro, conf, "modelo"
        finally:
            if own and client is not None:
                await aclose_llm(client)

    # ¿Qué decidió el modelo para cada contraparte, sumando todos sus grupos? Una regla solo
    # si todos dicen lo mismo (y lo ya clasificado fuera de «otros» no dice otra cosa).
    def _aplica(g: Grupo) -> bool:
        return (
            g.rubro is not None
            and g.rubro != OTROS
            and g.rubro not in only
            and (g.por != "modelo" or g.confianza >= MIN_CONFIDENCE)
        )

    por_cp: dict[str, set[str]] = {}
    for g in groups:
        if g.counterparty:
            por_cp.setdefault(norm(g.counterparty), set()).add(
                str(g.rubro) if _aplica(g) else OTROS
            )
    reglas_hechas: set[str] = set()

    for g in groups:
        aplica = _aplica(g)
        stats.decisiones.append(
            {
                "contraparte": g.counterparty,
                "descripcion": g.descriptions[0] if g.descriptions else "",
                "sentido": g.sentido,
                "movimientos": g.n,
                "rubro": g.rubro if aplica else OTROS,
                "confianza": round(g.confianza, 2),
                "por": g.por if aplica else "",
            }
        )
        if not aplica:
            stats.sin_cambio += 1
            continue
        if g.por == "regla":
            stats.por_regla += 1
        elif g.por == "determinista":
            stats.deterministas += 1
        else:
            stats.por_modelo += 1
        if dry_run:
            continue
        assert g.rubro is not None
        with connection() as conn:
            cp_norm = norm(g.counterparty)
            if (
                g.counterparty
                and g.por == "modelo"
                and cp_norm not in reglas_hechas
                and por_cp.get(cp_norm) == {g.rubro}
                and _otras_categorias(conn, user_id, g.counterparty, only) <= {g.rubro}
            ):
                set_rule(conn, user_id, g.counterparty, g.rubro, source=SOURCE_LLM)
                reglas_hechas.add(cp_norm)
            stats.consolidados_cambiados += _apply_group(conn, user_id, g, g.rubro)

    _log.info(
        "finance.recategorize.done",
        user_id=user_id,
        grupos=stats.grupos,
        por_regla=stats.por_regla,
        deterministas=stats.deterministas,
        por_modelo=stats.por_modelo,
        sin_cambio=stats.sin_cambio,
        consolidados=stats.consolidados_cambiados,
        llamadas=stats.llamadas,
        cost_usd=round(stats.cost_usd, 6),
        dry_run=dry_run,
    )
    return stats


__all__ = [
    "BATCH",
    "CATEGORY_GUIDE",
    "MIN_CONFIDENCE",
    "OTROS",
    "PURPOSE",
    "RECATEGORIZE_SYSTEM_PROMPT",
    "SOURCE_LLM",
    "SOURCE_USER",
    "Grupo",
    "RecategorizeStats",
    "apply_all",
    "apply_rule",
    "decide_deterministic",
    "list_rules",
    "load_groups",
    "norm",
    "parse_batch",
    "recategorize",
    "remove_rule",
    "render_batch",
    "rule_for",
    "rules_map",
    "set_rule",
    "with_rule",
]
