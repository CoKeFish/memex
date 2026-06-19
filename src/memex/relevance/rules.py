r"""Reglas deterministas del gate (`relevance_gate_rules`) + dry run contra el histórico.

Una regla es COMPUESTA (un remitente Y/O un patrón, combinados con AND) y tiene una POLARIDAD
`effect`:
- `block`: matchea → veredicto `not_relevant` (el correo NO pasa, sin juez ni revisión).
- `allow`: matchea → veredicto `relevant` (el correo ENTRA sin pasar por el juez).

Predicados (al menos uno; las reglas MINEADAS por el LLM llevan los dos):
- remitente: `sender_kind` ('sender_email'|'sender_domain'|'list_id') + `sender_value` (exacto).
- patrón: `pattern` (REGEX) sobre `match_field` ('subject'|'body'|'subject_or_body').

El patrón es un REGEX (no substring) de un DIALECTO RESTRINGIDO que se comporta idéntico en Python
`re` y en Postgres ARE — esa igualdad es la garantía de seguridad (el dry run vive en Postgres, el
runtime en Python). Para lograrla:
- Case-insensitivity SIN la opción del motor: bajamos el haystack a minúscula en AMBOS lados
  (`str.lower()` ≡ `lower()`) y matcheamos case-sensitive (`~` / `re.ASCII` sin IGNORECASE). Los
  acentos pasan como bytes literales; los patrones DEBEN venir en minúscula (lo exige
  `validate_pattern`).
- Haystacks de UNA línea (CR/LF→espacio en asunto; `\s+`→espacio en cuerpo) para que `.`/`^`/`$`
  coincidan. El cuerpo además quita invisibles de preheader (U+00AD/U+034F) y se trunca.
- Dialecto restringido (`_scan_dialect`): prohíbe `\b`/`\w`/lookaround/backrefs/flags-inline/etc.
  (divergentes o vectores de ReDoS) + compila en `re` Y en Postgres + cap de longitud + rechaza
  el patrón que matchea "" (matchearía todo) + guarda anti cuantificador-anidado.

Las reglas las propone la minería LLM (segunda pasada) o el dueño a mano, pero NUNCA se activan a
ciegas: toda propuesta pasa por `dry_run_rule`, un matcher determinista contra TODOS los correos
históricos. Si una `block` matchearía un correo de relevancia efectiva TRUE (o una `allow`, uno
FALSE), está mal hecha → `rejected` CON su reporte. Si pasa, se auto-activa (`activated_at`) y es
reversible (active↔disabled).

El matcheo vive DOS veces a propósito y debe mantenerse en espejo EXACTO:
- `rule_matches`/`_norm_subject`/`_norm_body` (Python): runtime sobre los WorkRow pendientes.
- `_sender_match_sql`/`_NORM_SUBJECT_SQL`/`_NORM_BODY_SQL` + `~` (SQL): dry run sobre el histórico.
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, RowMapping, text
from sqlalchemy.exc import DBAPIError

from memex.logging import get_logger
from memex.processing.windows import WorkRow
from memex.relevance.verdicts import EMAIL_TYPES

_log = get_logger("memex.relevance.rules")

#: Tipos de predicado de remitente (el QUIÉN). El patrón (el QUÉ) es un slot aparte.
SENDER_KINDS = ("sender_email", "sender_domain", "list_id")
#: Campos contra los que se aplica el regex del patrón.
MATCH_FIELDS = ("subject", "body", "subject_or_body")
#: Polaridad de una regla.
EFFECTS = ("block", "allow")
RULE_STATUSES = ("active", "disabled", "rejected")

_ROW_COLS = (
    "id, effect, sender_kind, sender_value, pattern, match_field, status, proposed_by, rationale, "
    "dry_run_report, model, activated_at, deactivated_at, created_at, updated_at"
)

#: Tope de ids de ejemplo (correos contaminantes) que guarda el reporte del dry run.
_SAMPLE_IDS_MAX = 20
#: Cap de longitud del patrón (anti-ReDoS + fuerza patrones específicos, no fragmentos sueltos).
_PATTERN_MAX_LEN = 256
#: Tope del haystack de cuerpo (chars). Espejado en Python (`[:N]`) y SQL (`left(...,N)`).
_HAYSTACK_MAXLEN = 16384
#: Chars invisibles de preheader que se quitan del cuerpo (soft hyphen U+00AD, combining grapheme
#: joiner U+034F). Una sola fuente de verdad: se interpola en `_NORM_BODY_SQL` y se itera
#: en `_norm_body`.
_INVISIBLE_CHARS = "­͏"
#: Timeout del dry run en Postgres (backstop ReDoS del lado SQL).
_DRY_RUN_TIMEOUT = "5s"


# ----------------------------------------------------------------- normalización (espejo Py↔SQL)
_NL_RE = re.compile(r"[\r\n]+")
_WS_RE = re.compile(r"\s+", re.ASCII)  # ASCII para espejar el POSIX `\s` de Postgres


def _coalesce_body(payload: dict[str, Any]) -> str:
    """Primer valor no-None de body_text/text/media_caption (espejo del COALESCE SQL)."""
    for key in ("body_text", "text", "media_caption"):
        v = payload.get(key)
        if v is not None:
            return str(v)
    return ""


def _norm_subject(subject: str) -> str:
    """Asunto a una sola línea (CR/LF→espacio) + minúscula. Espejo de `_NORM_SUBJECT_SQL`."""
    return _NL_RE.sub(" ", subject).lower()


def _norm_body(payload: dict[str, Any]) -> str:
    """Cuerpo normalizado: sin invisibles, `\\s+`→espacio (ASCII), trim de espacios, minúscula,
    truncado. Espejo EXACTO de `_NORM_BODY_SQL`."""
    body = _coalesce_body(payload)
    for ch in _INVISIBLE_CHARS:
        body = body.replace(ch, "")
    body = _WS_RE.sub(" ", body).strip(" ")
    return body.lower()[:_HAYSTACK_MAXLEN]


def norm_subject_sql(payload_expr: str) -> str:
    """SQL espejo de `_norm_subject` para una expresión de payload dada (alias variable)."""
    return (
        f"lower(regexp_replace(COALESCE(({payload_expr})->>'subject', ''), '[\\r\\n]+', ' ', 'g'))"
    )


def norm_body_sql(payload_expr: str) -> str:
    """SQL espejo de `_norm_body` para una expresión de payload dada. Los invisibles van LITERALES
    en el bracket (Postgres no soporta escapes \\u en regex); '\\s+' es escape ARE; `left` trunca
    igual que `[:N]` en Python."""
    return (
        "left(lower(btrim(regexp_replace(regexp_replace("
        f"COALESCE(({payload_expr})->>'body_text', ({payload_expr})->>'text', "
        f"({payload_expr})->>'media_caption', ''), "
        f"'[{_INVISIBLE_CHARS}]', '', 'g'), "
        "'\\s+', ' ', 'g'))), "
        f"{_HAYSTACK_MAXLEN})"
    )


#: Espejo SQL de `_norm_subject`/`_norm_body` para el dry run (alias `i` = inbox).
_NORM_SUBJECT_SQL = norm_subject_sql("i.payload")
_NORM_BODY_SQL = norm_body_sql("i.payload")


# --------------------------------------------------------------------- validación del dialecto
#: Escapes de clase POSIX que coinciden en `re` (ASCII) y Postgres ARE.
_ALLOWED_ESCAPES = set("dDsS")
#: Metacaracteres que se pueden escapar para usar como literal (idéntico en ambos motores).
_ESCAPABLE_SPECIALS = set(r".^$*+?()[]{}|/\-")


def _scan_dialect(p: str) -> None:
    """Rechaza los constructos divergentes o peligrosos. Acepta solo escapes/grupos conocidos y
    minúsculas; lo estructural (paréntesis balanceados, etc.) lo validan `re.compile` y el probe de
    Postgres. Incluye una guarda anti-ReDoS: prohíbe un cuantificador NO acotado (`*`,`+`,`{n,}`)
    aplicado a un grupo cuyo cuerpo ya tiene otro cuantificador no acotado (ej. `(a+)+`), la forma
    clásica de backtracking catastrófico (Python `re` no tiene timeout en runtime)."""
    i, n = 0, len(p)
    in_class = False
    # Por grupo abierto, ¿su cuerpo tiene un cuantificador no acotado? El centinela [0] = top-level.
    group_unbounded = [False]
    # Flag del grupo recién cerrado (para chequear un cuantificador que lo siga), o None.
    closed_unbounded: bool | None = None
    while i < n:
        c = p[i]
        if in_class:
            if c == "\\":
                if i + 1 >= n:
                    raise ValueError("backslash al final del patrón")
                if p[i + 1] not in _ALLOWED_ESCAPES and p[i + 1] not in _ESCAPABLE_SPECIALS:
                    raise ValueError(rf"escape no permitido en clase: \{p[i + 1]}")
                i += 2
                continue
            if "A" <= c <= "Z":
                raise ValueError("los patrones deben ir en minúscula")
            if c == "]":
                in_class = False
            i += 1
            closed_unbounded = None
            continue
        if c == "\\":
            if i + 1 >= n:
                raise ValueError("backslash al final del patrón")
            if p[i + 1] not in _ALLOWED_ESCAPES and p[i + 1] not in _ESCAPABLE_SPECIALS:
                raise ValueError(
                    rf"escape no permitido: \{p[i + 1]} "
                    r"(prohibidos \w \b \y, lookaround, backreferences, \A \Z)"
                )
            i += 2
            closed_unbounded = None
            continue
        if c == "[":
            in_class = True
            i += 1
            closed_unbounded = None
            continue
        if c == "(":
            if p[i + 1 : i + 2] == "?":
                if p[i + 1 : i + 3] != "?:":
                    raise ValueError(
                        "solo se permite (?:...); prohibidos lookaround, grupos nombrados y flags "
                        "inline"
                    )
                i += 3
            else:
                i += 1
            group_unbounded.append(False)
            closed_unbounded = None
            continue
        if c == ")":
            closed_unbounded = group_unbounded.pop() if len(group_unbounded) > 1 else None
            i += 1
            continue
        # ¿Es un cuantificador? (None = no lo es; True/False = acotado o no)
        quant_unbounded: bool | None = None
        adv = 1
        if c in "*+":
            quant_unbounded = True
        elif c == "?":
            quant_unbounded = False
        elif c == "{":
            close = p.find("}", i)
            if close != -1:
                quant_unbounded = p[i + 1 : close].endswith(",")  # `{n,}` = sin cota superior
                adv = close - i + 1
        if quant_unbounded is not None:
            if quant_unbounded and closed_unbounded:
                raise ValueError(
                    "cuantificador no acotado sobre un grupo que ya tiene otro no acotado "
                    "(riesgo de backtracking catastrófico / ReDoS); usá repetición acotada `{n,m}`"
                )
            if quant_unbounded:
                group_unbounded[-1] = True
            closed_unbounded = None
            i += adv
            continue
        if "A" <= c <= "Z":
            raise ValueError("el patrón debe ir en minúscula (el texto va en minúscula)")
        closed_unbounded = None
        i += 1


@functools.lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern[str]:
    """Compila un patrón del gate (case-sensitive sobre haystack ya en minúscula; `re.ASCII` para
    que `\\d`/`\\s` coincidan con el POSIX de Postgres). Cacheado: los patrones son estables."""
    return re.compile(pattern, re.ASCII)


def validate_pattern(pattern: str) -> str:
    """Valida un regex del dialecto restringido (no normaliza su case). `ValueError` si es
    inseguro, divergente o no compila en `re`. La validación en Postgres la hace el dry run."""
    p = pattern.strip()
    if not p:
        raise ValueError("patrón vacío")
    if len(p) > _PATTERN_MAX_LEN:
        raise ValueError(f"patrón demasiado largo (>{_PATTERN_MAX_LEN} chars)")
    _scan_dialect(p)
    try:
        compiled = _compile(p)
    except re.error as e:
        raise ValueError(f"regex inválido: {e}") from e
    if compiled.search("") is not None:
        raise ValueError("el patrón matchea la cadena vacía (matchearía todos los correos)")
    return p


# ------------------------------------------------------------------------------- matcheo runtime
@dataclass(frozen=True)
class EmailFields:
    """Campos normalizados de un correo contra los que matchean las reglas (ya en minúscula)."""

    sender_email: str  # lower; "" si falta
    sender_domain: str  # lower, parte tras @; "" si falta
    list_id: str  # lower; "" si falta
    subject: str  # normalizado (1 línea) + lower
    body: str  # normalizado (sin invisibles, ws colapsado) + lower + truncado; "" si no se pidió


def extract_email_fields(payload: dict[str, Any], *, need_body: bool = True) -> EmailFields:
    """Normaliza los campos matcheables de un payload de correo (faltantes → ""). El cuerpo se
    computa solo si `need_body` (lo evita cuando ninguna regla activa usa body)."""
    from_ = payload.get("from") or {}
    email = str(from_.get("email") or "").strip().lower() if isinstance(from_, dict) else ""
    domain = email.split("@", 1)[1] if "@" in email else ""
    return EmailFields(
        sender_email=email,
        sender_domain=domain,
        list_id=str(payload.get("list_id") or "").strip().lower(),
        subject=_norm_subject(str(payload.get("subject") or "")),
        body=_norm_body(payload) if need_body else "",
    )


def match_sender(sender_kind: str, sender_value: str, fields: EmailFields) -> bool:
    """¿Matchea el predicado de remitente (kind, value)? Espejo de `_sender_match_sql`."""
    v = sender_value.strip().lower()
    if not v:
        return False
    if sender_kind == "sender_email":
        return fields.sender_email == v
    if sender_kind == "sender_domain":
        return fields.sender_domain == v
    if sender_kind == "list_id":
        return fields.list_id == v
    raise ValueError(f"sender_kind desconocido: {sender_kind!r}; válidos: {SENDER_KINDS}")


def match_pattern(compiled: re.Pattern[str], match_field: str, fields: EmailFields) -> bool:
    """¿Matchea el regex (ya compilado) sobre el/los campo(s)? Espejo de `_pattern_match_sql`.

    `match_field` inválido → False (defensivo; el esquema garantiza un valor válido).
    """
    if match_field == "subject":
        return compiled.search(fields.subject) is not None
    if match_field == "body":
        return compiled.search(fields.body) is not None
    if match_field == "subject_or_body":
        return (
            compiled.search(fields.subject) is not None or compiled.search(fields.body) is not None
        )
    return False


def rule_matches(
    sender_kind: str | None,
    sender_value: str | None,
    pattern: str | None,
    match_field: str | None,
    fields: EmailFields,
) -> bool:
    """¿Matchea la regla compuesta? AND de los predicados PRESENTES (≥1 por el esquema).

    Sin ningún predicado (no debería pasar) → False, defensivo: una regla vacía no matchea todo.
    Patrón que no compila → False (defensivo; el log del descarte vive en `apply_active_rules`).
    """
    has_predicate = False
    if sender_kind is not None:
        has_predicate = True
        if not match_sender(sender_kind, sender_value or "", fields):
            return False
    if pattern is not None:
        has_predicate = True
        try:
            compiled = _compile(pattern)
        except re.error:
            return False
        if not match_pattern(compiled, match_field or "", fields):
            return False
    return has_predicate


@dataclass(frozen=True)
class RuleDecision:
    """Cortocircuito determinista de una regla para un mensaje (sin conflicto de polaridad)."""

    inbox_id: int
    effect: str  # 'block' | 'allow'
    rule_id: int


@dataclass(frozen=True)
class RuleConflict:
    """Mensaje matcheado por reglas de AMBAS polaridades: no se cortocircuita, cae al juez."""

    inbox_id: int
    block_rule_id: int
    allow_rule_id: int


@dataclass(frozen=True)
class RuleApplication:
    """Resultado de aplicar las reglas activas a una ventana: decisiones + conflictos."""

    decisions: list[RuleDecision]
    conflicts: list[RuleConflict]


def _load_active_rules(conn: Connection, user_id: int) -> list[RowMapping]:
    """Reglas activas + descarte (con log) de las que tengan un patrón que ya no compila: un
    patrón corrupto (anterior a la validación, o drift) NO debe crashear la ventana ni el gate."""
    rules = (
        conn.execute(
            text(
                "SELECT id, effect, sender_kind, sender_value, pattern, match_field "
                "FROM relevance_gate_rules "
                "WHERE user_id = :uid AND status = 'active' ORDER BY id"
            ),
            {"uid": user_id},
        )
        .mappings()
        .all()
    )
    valid: list[RowMapping] = []
    for rule in rules:
        pat = rule["pattern"]
        if pat is not None:
            try:
                _compile(str(pat))
            except re.error as e:
                _log.warning(
                    "relevance.rules.bad_pattern",
                    rule_id=int(rule["id"]),
                    pattern=str(pat),
                    error=str(e),
                )
                continue
        valid.append(rule)
    return valid


def apply_active_rules(conn: Connection, user_id: int, rows: list[WorkRow]) -> RuleApplication:
    """Aplica las reglas activas a los mensajes pendientes, sin LLM.

    Por mensaje toma la PRIMERA regla block y la PRIMERA allow que matchean (más viejas primero,
    orden estable). Si matchean AMBAS polaridades → conflicto (va al juez, no se cortocircuita);
    si solo una → decisión de esa polaridad; si ninguna → el mensaje queda pendiente (juez).
    """
    if not rows:
        return RuleApplication([], [])
    rules = _load_active_rules(conn, user_id)
    if not rules:
        return RuleApplication([], [])
    #: El cuerpo solo se normaliza si alguna regla lo necesita (evita coste por mensaje en vano).
    needs_body = any(r["match_field"] in ("body", "subject_or_body") for r in rules)
    decisions: list[RuleDecision] = []
    conflicts: list[RuleConflict] = []
    for row in rows:
        fields = extract_email_fields(row.payload, need_body=needs_body)
        block_id: int | None = None
        allow_id: int | None = None
        for rule in rules:
            sk = None if rule["sender_kind"] is None else str(rule["sender_kind"])
            sv = None if rule["sender_value"] is None else str(rule["sender_value"])
            pat = None if rule["pattern"] is None else str(rule["pattern"])
            mf = None if rule["match_field"] is None else str(rule["match_field"])
            if not rule_matches(sk, sv, pat, mf, fields):
                continue
            if rule["effect"] == "block":
                if block_id is None:
                    block_id = int(rule["id"])
            elif allow_id is None:
                allow_id = int(rule["id"])
            if block_id is not None and allow_id is not None:
                break
        if block_id is not None and allow_id is not None:
            conflicts.append(RuleConflict(row.inbox_id, block_id, allow_id))
        elif block_id is not None:
            decisions.append(RuleDecision(row.inbox_id, "block", block_id))
        elif allow_id is not None:
            decisions.append(RuleDecision(row.inbox_id, "allow", allow_id))
    return RuleApplication(decisions, conflicts)


@dataclass(frozen=True)
class DryRunReport:
    """Resultado del dry run de una regla contra el histórico de correos del usuario.

    `passes` depende de la polaridad: una `block` no debe atrapar ningún correo de relevancia
    efectiva TRUE; una `allow`, ninguno de relevancia FALSE. La precisión la da el patrón
    (remitente+regex carva el subconjunto correcto), no una tolerancia de ratio.
    """

    effect: str
    matched: int
    matched_relevant: int
    matched_not_relevant: int
    matched_unverdicted: int
    relevant_sample_ids: tuple[int, ...]
    not_relevant_sample_ids: tuple[int, ...]

    @property
    def passes(self) -> bool:
        if self.effect == "allow":
            return self.matched_not_relevant == 0
        return self.matched_relevant == 0

    @property
    def contaminating_sample_ids(self) -> tuple[int, ...]:
        """Los ids que hacen la regla insegura: relevantes para block, no-relevantes para allow."""
        return self.not_relevant_sample_ids if self.effect == "allow" else self.relevant_sample_ids

    def as_dict(self) -> dict[str, Any]:
        return {
            "effect": self.effect,
            "matched": self.matched,
            "matched_relevant": self.matched_relevant,
            "matched_not_relevant": self.matched_not_relevant,
            "matched_unverdicted": self.matched_unverdicted,
            "relevant_sample_ids": list(self.relevant_sample_ids),
            "not_relevant_sample_ids": list(self.not_relevant_sample_ids),
            "contaminating_sample_ids": list(self.contaminating_sample_ids),
            "passes": self.passes,
        }


def _sender_match_sql(sender_kind: str) -> str:
    """Predicado SQL de remitente por kind (espejo de `match_sender`)."""
    if sender_kind == "sender_email":
        return "lower(COALESCE(i.payload->'from'->>'email', '')) = :sender_value"
    if sender_kind == "sender_domain":
        return (
            "split_part(lower(COALESCE(i.payload->'from'->>'email', '')), '@', 2) = :sender_value"
        )
    if sender_kind == "list_id":
        return "lower(COALESCE(i.payload->>'list_id', '')) = :sender_value"
    raise ValueError(f"sender_kind desconocido: {sender_kind!r}; válidos: {SENDER_KINDS}")


def _pattern_match_sql(match_field: str) -> str:
    """Predicado SQL del regex por campo (espejo de `match_pattern`). `~` case-sensitive sobre el
    haystack ya en minúscula (la normalización ya bajó el case)."""
    if match_field == "subject":
        return f"({_NORM_SUBJECT_SQL} ~ :pattern)"
    if match_field == "body":
        return f"({_NORM_BODY_SQL} ~ :pattern)"
    if match_field == "subject_or_body":
        return f"({_NORM_SUBJECT_SQL} ~ :pattern OR {_NORM_BODY_SQL} ~ :pattern)"
    raise ValueError(f"match_field desconocido: {match_field!r}; válidos: {MATCH_FIELDS}")


#: Relevancia EFECTIVA de un mensaje: mark manual si existe, si no veredicto `relevant`.
_EFFECTIVE_RELEVANT_SQL = "COALESCE(rm.is_relevant, rv.verdict = 'relevant', FALSE)"
#: ¿El mensaje tiene señal (mark o veredicto)? Sin señal = pendiente, no cuenta de ningún lado.
_HAS_SIGNAL_SQL = "(rm.is_relevant IS NOT NULL OR rv.verdict IS NOT NULL)"


def validate_predicates(
    sender_kind: str | None,
    sender_value: str | None,
    pattern: str | None,
    match_field: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Valida y NORMALIZA un set de predicados de regla (≥1; remitente+valor juntos; patrón+campo
    juntos). Devuelve `(sender_kind, sender_value, pattern, match_field)` normalizados:
    `sender_value` a lower (remitentes case-insensitive); `pattern` validado por el dialecto (NO se
    baja el case: corrompería `\\D`→`\\d`). `ValueError` si el set es inválido."""
    sk = (sender_kind or "").strip() or None
    sv = (sender_value or "").strip()
    if sk is not None and sk not in SENDER_KINDS:
        raise ValueError(f"sender_kind inválido: {sk!r}; válidos: {SENDER_KINDS}")
    if sk is not None and not sv:
        raise ValueError("sender_value requerido cuando hay sender_kind")
    if sk is None and sv:
        raise ValueError("sender_kind requerido cuando hay sender_value")
    sv_norm = sv.lower() if sk is not None else None

    p = (pattern or "").strip()
    mf = (match_field or "").strip() or None
    pattern_norm: str | None = None
    match_field_norm: str | None = None
    if p:
        if mf is None:
            raise ValueError("match_field requerido cuando hay patrón")
        if mf not in MATCH_FIELDS:
            raise ValueError(f"match_field inválido: {mf!r}; válidos: {MATCH_FIELDS}")
        pattern_norm = validate_pattern(p)
        match_field_norm = mf
    elif mf is not None:
        raise ValueError("patrón requerido cuando hay match_field")

    if sv_norm is None and pattern_norm is None:
        raise ValueError("una regla necesita al menos un predicado (remitente o patrón)")
    return sk, sv_norm, pattern_norm, match_field_norm


def dry_run_rule(
    conn: Connection,
    user_id: int,
    *,
    effect: str,
    sender_kind: str | None = None,
    sender_value: str | None = None,
    pattern: str | None = None,
    match_field: str | None = None,
) -> DryRunReport:
    """Corre la regla compuesta contra TODOS los correos históricos del usuario, sin efectos.

    Clasifica cada match por relevancia efectiva (mark manual > veredicto del gate). Según la
    polaridad, un solo correo del lado contaminante → la regla está mal hecha (`passes=False`).
    El query corre en un SAVEPOINT con `statement_timeout`: un regex inválido para Postgres o un
    backtracking catastrófico revierten SOLO ese savepoint y salen como `ValueError` (no envenenan
    la transacción compartida del minado, que valida muchas propuestas en una sola `connection()`).
    """
    if effect not in EFFECTS:
        raise ValueError(f"effect inválido: {effect!r}; válidos: {EFFECTS}")
    sk, sv, pat, mf = validate_predicates(sender_kind, sender_value, pattern, match_field)

    clauses: list[str] = []
    params: dict[str, Any] = {"uid": user_id, "email_types": EMAIL_TYPES}
    if sk is not None:
        clauses.append(_sender_match_sql(sk))
        params["sender_value"] = sv
    if pat is not None:
        assert mf is not None  # garantizado por validate_predicates (patrón+campo juntos)
        clauses.append(_pattern_match_sql(mf))
        params["pattern"] = pat
    predicate_sql = " AND ".join(f"({c})" for c in clauses)

    not_relevant_sql = f"({_HAS_SIGNAL_SQL} AND NOT {_EFFECTIVE_RELEVANT_SQL})"
    query = text(
        f"""
        SELECT
            COUNT(*) AS matched,
            COUNT(*) FILTER (WHERE {_EFFECTIVE_RELEVANT_SQL}) AS matched_relevant,
            COUNT(*) FILTER (WHERE NOT {_HAS_SIGNAL_SQL}) AS matched_unverdicted,
            (ARRAY_AGG(i.id ORDER BY i.id)
                FILTER (WHERE {_EFFECTIVE_RELEVANT_SQL})
            )[1:{_SAMPLE_IDS_MAX}] AS relevant_sample_ids,
            (ARRAY_AGG(i.id ORDER BY i.id)
                FILTER (WHERE {not_relevant_sql})
            )[1:{_SAMPLE_IDS_MAX}] AS not_relevant_sample_ids
        FROM inbox i
        JOIN sources s ON s.id = i.source_id
        LEFT JOIN relevance_marks rm ON rm.inbox_id = i.id
        LEFT JOIN relevance_verdicts rv ON rv.inbox_id = i.id
        WHERE i.user_id = :uid
          AND s.type = ANY(:email_types)
          AND {predicate_sql}
        """
    )
    nested = conn.begin_nested()
    try:
        conn.execute(text(f"SET LOCAL statement_timeout = '{_DRY_RUN_TIMEOUT}'"))
        row = conn.execute(query, params).mappings().one()
        nested.commit()
    except DBAPIError as e:
        nested.rollback()
        raise ValueError(
            f"regex inválido o demasiado costoso para Postgres: {getattr(e, 'orig', e)}"
        ) from e

    matched = int(row["matched"])
    relevant = int(row["matched_relevant"])
    unverdicted = int(row["matched_unverdicted"])
    return DryRunReport(
        effect=effect,
        matched=matched,
        matched_relevant=relevant,
        matched_not_relevant=matched - relevant - unverdicted,
        matched_unverdicted=unverdicted,
        relevant_sample_ids=tuple(int(i) for i in (row["relevant_sample_ids"] or [])),
        not_relevant_sample_ids=tuple(int(i) for i in (row["not_relevant_sample_ids"] or [])),
    )


def create_rule(
    conn: Connection,
    user_id: int,
    *,
    effect: str,
    sender_kind: str | None = None,
    sender_value: str | None = None,
    pattern: str | None = None,
    match_field: str | None = None,
    proposed_by: str,
    report: DryRunReport,
    rationale: str = "",
    model: str | None = None,
) -> dict[str, Any] | None:
    """Persiste una regla compuesta con su reporte de dry run: `active` si pasa, `rejected` si no.

    El reporte se guarda SIEMPRE (también el de las rechazadas: es la auditoría del porqué).
    Duplicada (mismo user/effect/predicados) → None (el caller decide: skip en minería, 409 en API).
    El `ON CONFLICT` debe coincidir EXACTO con el índice único `relevance_gate_rules_dedupe` de la
    migración 0077 (el patrón NO se baja a lower: el case del regex es significativo, `\\D`≠`\\d`).
    """
    if effect not in EFFECTS:
        raise ValueError(f"effect inválido: {effect!r}; válidos: {EFFECTS}")
    sk, sv, pat, mf = validate_predicates(sender_kind, sender_value, pattern, match_field)
    status = "active" if report.passes else "rejected"
    row = (
        conn.execute(
            text(
                f"""
                INSERT INTO relevance_gate_rules
                    (user_id, effect, sender_kind, sender_value, pattern, match_field, status,
                     proposed_by, rationale, dry_run_report, model, activated_at)
                VALUES (:uid, :effect, :sender_kind, :sender_value, :pattern, :match_field, :status,
                        :proposed_by, :rationale, CAST(:report AS JSONB), :model,
                        CASE WHEN :status = 'active' THEN NOW() END)
                ON CONFLICT (
                    user_id, effect, lower(coalesce(sender_kind, '')),
                    lower(coalesce(sender_value, '')), coalesce(pattern, ''),
                    coalesce(match_field, '')
                ) DO NOTHING
                RETURNING {_ROW_COLS}
                """
            ),
            {
                "uid": user_id,
                "effect": effect,
                "sender_kind": sk,
                "sender_value": sv,
                "pattern": pat,
                "match_field": mf,
                "status": status,
                "proposed_by": proposed_by,
                "rationale": rationale[:1000],
                "report": json.dumps(report.as_dict()),
                "model": model,
            },
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def set_rule_status(
    conn: Connection, rule_id: int, user_id: int, status: str
) -> dict[str, Any] | None:
    """Toggle reversible active↔disabled. Una regla `rejected` no se puede activar (falló su
    dry run; si el dueño la quiere, la crea a mano y el dry run corre de nuevo). None si no
    existe, ValueError si la transición no es válida."""
    if status not in ("active", "disabled"):
        raise ValueError(f"status inválido: {status!r}; válidos: ('active', 'disabled')")
    row = (
        conn.execute(
            text(
                f"""
                UPDATE relevance_gate_rules
                SET status = :status,
                    activated_at = CASE WHEN :status = 'active' THEN NOW() ELSE activated_at END,
                    deactivated_at = CASE WHEN :status = 'disabled' THEN NOW()
                                          ELSE deactivated_at END,
                    updated_at = NOW()
                WHERE id = :id AND user_id = :uid AND status <> 'rejected'
                RETURNING {_ROW_COLS}
                """
            ),
            {"status": status, "id": rule_id, "uid": user_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def list_rules(
    conn: Connection, user_id: int, *, status: str | None = None, effect: str | None = None
) -> list[dict[str, Any]]:
    """Reglas del usuario (todas o por status/effect), más nuevas primero."""
    if status is not None and status not in RULE_STATUSES:
        raise ValueError(f"status inválido: {status!r}; válidos: {RULE_STATUSES}")
    if effect is not None and effect not in EFFECTS:
        raise ValueError(f"effect inválido: {effect!r}; válidos: {EFFECTS}")
    where = "user_id = :uid"
    if status is not None:
        where += " AND status = :status"
    if effect is not None:
        where += " AND effect = :effect"
    rows = (
        conn.execute(
            text(f"SELECT {_ROW_COLS} FROM relevance_gate_rules WHERE {where} ORDER BY id DESC"),
            {"uid": user_id, "status": status, "effect": effect},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]
