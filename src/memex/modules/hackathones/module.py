"""`HackathonModule` — extractor puro de hackatones. Satisface `InterestModule` estructuralmente.

Tercer módulo del orden de construcción (ADR-015 §11). En esta entrega es un EXTRACTOR PURO (como
finance): sin dependencias, sin dominio consolidador, sin servicios externos. `consumes_kinds`
incluye los tres kinds (email/chat/social) porque los hackatones se anuncian por correo (listas
universitarias, MLH/Devpost), comunidades dev (Discord/Telegram) y redes (Twitter/Instagram).

Forward-compat (cuando aterrice el seam inter-módulo de relaciones, slice 3 de ADR-015): este
módulo pasará a declarar `depends_on=("calendar",)` + `CAP_CONTRIBUTE_DOMAIN` y contribuirá los
hackatones agendables al dominio calendar vía `ctx.deps["calendar"].contribute(...)` (la ontología
ya admite `<módulo> → calendar : materializado_como`). Hoy el orquestador inyecta `ctx.deps={}`, así
que esa capacidad NO va todavía — agregarla ahora sería diseño especulativo (ADR-015 §4).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import ClassVar

from memex.core.source import HealthResult, SourceKind
from memex.logging import get_logger
from memex.modules.contract import CAP_EXTRACT, ExtractionItem, ModuleContext
from memex.modules.dedup import upsert_unique
from memex.modules.hackathones.prompt import HACKATHON_SYSTEM_PROMPT
from memex.modules.hackathones.schema import HackathonItem

_log = get_logger("memex.modules.hackathones")


class HackathonModule:
    """Extrae hackatones a `mod_hackathones_events`."""

    slug: ClassVar[str] = "hackathones"
    interest: ClassVar[str] = (
        "Hackatones y competencias de programación: hackathons, datathons, game jams, code "
        "challenges, CTF. Nombre, fechas, sede o modalidad online, premios, tecnologías y "
        "requisitos. NO cursos, talleres, webinars, ofertas de empleo ni publicidad genérica."
    )
    extraction_schema: ClassVar[type[ExtractionItem]] = HackathonItem
    extraction_prompt: ClassVar[str] = HACKATHON_SYSTEM_PROMPT
    capabilities: ClassVar[frozenset[str]] = frozenset({CAP_EXTRACT})
    consumes_kinds: ClassVar[frozenset[SourceKind]] = frozenset(
        {SourceKind.EMAIL, SourceKind.CHAT, SourceKind.SOCIAL}
    )
    depends_on: ClassVar[tuple[str, ...]] = ()
    #: business-key del vértice hackatón. `name` se compara normalizado (lower + colapso de
    #: whitespace) por la DB; el UNIQUE de negocio (índice funcional) vive en la migración 0030
    #: (con `starts_on` NULL = centinela, para anuncios sin fecha del evento).
    identity_fields: ClassVar[tuple[str, ...]] = ("name", "starts_on")

    async def persist(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        """Materializa cada hackatón como VÉRTICE ÚNICO (dedup por nombre normalizado + fecha):
        re-anunciar el mismo hackatón fusiona `source_inbox_ids` en vez de duplicar. Atómico en
        `ctx.conn`. Devuelve cuántos hackatones procesó."""
        hackathons = [i for i in items if isinstance(i, HackathonItem)]
        if not hackathons:
            return 0
        for h in hackathons:
            row = {
                "user_id": ctx.user_id,
                "source_inbox_ids": list(h.source_inbox_ids),
                "name": h.name,
                "starts_on": h.starts_on,
                "ends_on": h.ends_on,
                "registration_deadline": h.registration_deadline,
                "modality": h.modality,
                "location": h.location,
                "url": h.url,
                "organizer": h.organizer,
                "technologies": h.technologies,
                "prizes": h.prizes,
                "requirements": h.requirements,
                "description": h.description,
                "evidence": h.evidence,
            }
            identity = {"user_id": ctx.user_id, "name": h.name, "starts_on": h.starts_on}
            upsert_unique(
                ctx.conn,
                "mod_hackathones_events",
                identity=identity,
                row=row,
                merge_arrays=("source_inbox_ids",),
                norm_text=("name",),
            )
        return len(hackathons)

    async def health_check(self) -> HealthResult:
        return HealthResult(
            status="healthy", detail="hackathones module ready", checked_at=datetime.now(UTC)
        )
