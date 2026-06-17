"""EntityProfile — el formato de salida GARANTIZADO del subsistema webcontext.

Pydantic frozen `BaseModel` con `extra="forbid"`: el perfil corto de una org/producto, genérico y
transversal (no atado a ningún módulo). Es la ÚNICA fuente del JSON Schema — `entity_profile_schema`
alimenta tanto el `--output-schema` de codex como el `schema` del scrape de firecrawl, y
`validate_profile` valida el retorno de AMBOS proveedores contra él (calca el patrón Pydantic de
`memex.modules.identidades.schema.IdentityItem`).

Los validators normalizan sin rechazar (strings vacíos → None, listas sucias → tuple limpia+dedup),
salvo `kind`, que es del caller (no del LLM): en `validate_profile` se RE-INYECTA antes de validar.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from memex.llm._json import normalize_json_output
from memex.webcontext.client import EntityKind, WebContextFormatError

_BODY_PREVIEW_MAX = 500

#: Sinónimos frecuentes del LLM → kind canónico (lista cerrada: org/producto, nunca persona).
_KIND_SYNONYMS: dict[str, EntityKind] = {
    "organizacion": "organizacion",
    "organización": "organizacion",
    "organization": "organizacion",
    "org": "organizacion",
    "empresa": "organizacion",
    "company": "organizacion",
    "compañía": "organizacion",
    "compania": "organizacion",
    "producto": "producto",
    "product": "producto",
    "app": "producto",
    "aplicación": "producto",
    "aplicacion": "producto",
    "software": "producto",
    "servicio": "producto",
    "service": "producto",
}


class EntityProfile(BaseModel):
    """Perfil corto y verificable de una entidad (org/producto) + procedencia (URLs)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: EntityKind
    one_liner: str = ""
    sector: str | None = None
    country: str | None = None
    founded: str | None = None  # str, no int: "2015" / "ago-2015" / "2015-08"
    key_facts: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()  # procedencia: URLs reales consultadas

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, v: object) -> str:
        """Sinónimo → kind canónico. Un valor irreconocible ROMPE: el caller manda el `kind` y en el
        path de proveedor se re-inyecta antes de validar, así que esto solo protege la construcción
        directa de un `EntityProfile`."""
        s = str(v or "").strip().lower()
        if s not in _KIND_SYNONYMS:
            raise ValueError(f"kind no reconocido: {v!r}")
        return _KIND_SYNONYMS[s]

    @field_validator("one_liner", mode="before")
    @classmethod
    def _clean_one_liner(cls, v: object) -> str:
        return str(v or "").strip()

    @field_validator("sector", "country", "founded", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> str | None:
        s = str(v or "").strip()
        return s or None

    @field_validator("key_facts", "sources", mode="before")
    @classmethod
    def _clean_str_tuple(cls, v: object) -> tuple[str, ...]:
        """None → (); str → (str,); list/tuple → strings no vacíos, stripped, dedup en orden."""
        if v is None:
            return ()
        if isinstance(v, str):
            raw_items: list[object] = [v]
        elif isinstance(v, (list, tuple)):
            raw_items = list(v)
        else:
            return ()
        out: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            s = str(item).strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return tuple(out)


def _strict_schema(node: Any) -> Any:
    """Adapta el schema Pydantic al subset STRICT de OpenAI structured outputs (lo exige el
    `--output-schema` de codex): cada object lleva `required` = TODAS sus properties +
    `additionalProperties: false`, y se quita `default` (keyword no soportado). Recursivo."""
    if isinstance(node, dict):
        out = {k: _strict_schema(v) for k, v in node.items() if k != "default"}
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out
    if isinstance(node, list):
        return [_strict_schema(item) for item in node]
    return node


def entity_profile_schema() -> dict[str, Any]:
    """JSON Schema STRICT (subset de OpenAI structured outputs) que alimenta el `--output-schema`
    de codex y el `schema` del scrape de firecrawl, y contra el que se valida el retorno. STRICT
    exige `required` = todas las propiedades (codex rechaza lo contrario; firecrawl lo tolera)."""
    schema = _strict_schema(EntityProfile.model_json_schema())
    assert isinstance(schema, dict)
    return schema


def validate_profile_data(data: object, *, expected_kind: EntityKind) -> EntityProfile:
    """Valida un objeto YA PARSEADO (dict) contra `EntityProfile` (lo usa firecrawl, que recibe el
    JSON parseado del scrape). Re-inyecta el `kind`; `WebContextFormatError` si no valida.
    """
    if not isinstance(data, dict):
        raise WebContextFormatError(0, f"se esperaba un objeto JSON, no {type(data).__name__}")
    payload = dict(data)
    payload["kind"] = expected_kind  # el caller manda; se ignora lo que el LLM haya puesto
    try:
        return EntityProfile.model_validate(payload)
    except ValidationError as e:
        raise WebContextFormatError(
            0, "el perfil no cumple el schema", body=str(e)[:_BODY_PREVIEW_MAX]
        ) from e


def validate_profile(raw_text: str, *, expected_kind: EntityKind) -> EntityProfile:
    """Parsea + valida la salida cruda de TEXTO de un proveedor (codex) contra `EntityProfile`.

    `normalize_json_output` (reuso de la capa LLM) extrae el JSON de fences/prosa, luego delega en
    `validate_profile_data`. Fallo de parseo → `WebContextFormatError`; el proveedor decide
    el retry.
    """
    cleaned = normalize_json_output(raw_text)
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as e:
        raise WebContextFormatError(
            0, "salida no parseable como JSON", body=cleaned[:_BODY_PREVIEW_MAX]
        ) from e
    return validate_profile_data(data, expected_kind=expected_kind)
