"""Integridad del directorio: reglas deterministas que mantienen sanos los datos de los que
dependen la resolución por nombre/dominio y la jerarquía. Se aplican en los escritores (prevención)
y como barrido (`run_hygiene`, CLI `memex identidad hygiene`).

1. **Alias**: un alias no puede ser el nombre canónico de otra ficha viva
   (`merge.drop_colliding_aliases`).
2. **Dominios**: un identificador de tipo `domain` solo vale si el dominio identifica a UNA
   organización:
   - free-mail (gmail…) y relays (stripe, luma-mail, appleid…) NO identifican a quien escribe
     desde ellos… salvo al PROVEEDOR mismo: `paypal.com` sí es de PayPal, `appleid.com` de Apple
     (la org es DUEÑA del dominio cuando el núcleo de su nombre está en la etiqueta del dominio);
   - una PLATAFORMA compartida tampoco: dominio registrable desde el que escriben organizaciones de
     ≥ 2 raíces distintas SIN una raíz dominante (tebex.io vende para muchas tiendas). El dominio
     institucional de una universidad lo comparten sus sub-unidades y hasta alguna oficina ajena
     con buzón institucional (FONDEX en la Salesiana): una raíz concentra la gran mayoría de los
     remitentes → es SU dominio, atribuible a esa raíz (y a nadie más).
   Los EMAILS en esos dominios se conservan: identifican a ese remitente concreto
   (`billing+acct_x@stripe.com` ES Railway), no al dominio.

La detección es por datos, no por lista: la lista de relays/free-mail (`normalize`) cubre lo obvio;
las reglas de dueño y de dominancia cubren lo que nadie listó.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.modules.identidades.merge import drop_colliding_aliases
from memex.modules.identidades.normalize import (
    is_freemail,
    is_relay_domain,
    org_core,
    registrable_domain,
)

_log = get_logger("memex.modules.identidades.hygiene")

REASON_FREEMAIL = "freemail"
REASON_RELAY = "relay"
REASON_SHARED = "shared_platform"
REASON_NOT_DOMINANT = "not_dominant_root"
#: Relays PUROS: solo reenvían por cuenta de terceros (Hide My Email de Apple, ESPs). Un correo
#: desde ahí nunca es del proveedor → ni siquiera el proveedor los lleva como identificador
#: (atribuir `appleid.com` a Apple mandaba a Apple los relays de Rappi). paypal.com/stripe.com sí
#: son duales (la empresa también escribe desde ahí) y quedan bajo la regla de dueño.
PURE_RELAY_DOMAINS: frozenset[str] = frozenset(
    {
        "appleid.com",
        "sendgrid.net",
        "amazonses.com",
        "mailgun.org",
        "mcsv.net",
        "customeriomail.com",
        "hubspotemail.net",
        "intercom-mail.com",
        "luma-mail.com",
    }
)
#: Fracción de los emails de un dominio que debe concentrar UNA raíz para que el dominio sea suyo
#: (y no una plataforma compartida). 19 de 20 remitentes @salesiana son de la Salesiana → suyo.
DOMINANCE = 0.67


def _roots(conn: Connection, user_id: int) -> dict[int, int]:
    """identidad → raíz de su árbol de pertenencia (ella misma si no cuelga de nadie)."""
    rows = conn.execute(
        text(
            """
            WITH RECURSIVE up AS (
              SELECT id, parent_identity_id AS parent, id AS root, 0 AS d
              FROM mod_identidades WHERE user_id = :u
              UNION ALL
              SELECT up.id, i.parent_identity_id, i.id, up.d + 1
              FROM up JOIN mod_identidades i ON i.id = up.parent
              WHERE up.d < 12
            )
            SELECT DISTINCT ON (id) id, root FROM up ORDER BY id, d DESC
            """
        ),
        {"u": user_id},
    ).all()
    return {int(r[0]): int(r[1]) for r in rows}


@dataclass(frozen=True)
class DomainUse:
    """Quién escribe desde un dominio registrable: emails distintos por raíz."""

    by_root: dict[int, int]

    @property
    def total(self) -> int:
        return sum(self.by_root.values())

    @property
    def dominant(self) -> int | None:
        """La raíz que concentra ≥ `DOMINANCE` de los emails del dominio, o None."""
        if not self.by_root:
            return None
        root, n = max(self.by_root.items(), key=lambda kv: kv[1])
        return root if n >= DOMINANCE * self.total else None

    @property
    def shared(self) -> bool:
        return len(self.by_root) >= 2 and self.dominant is None


def domain_usage(conn: Connection, user_id: int) -> dict[str, DomainUse]:
    """`{dominio registrable → uso}` a partir de los EMAILS de organizaciones/productos."""
    roots = _roots(conn, user_id)
    acc: dict[str, dict[int, int]] = {}
    rows = conn.execute(
        text(
            """
            SELECT x.identity_id, x.value_norm FROM mod_identidades_identifiers x
            JOIN mod_identidades i ON i.id = x.identity_id
            WHERE x.user_id = :u AND x.kind = 'email' AND i.kind IN ('organizacion','producto')
            """
        ),
        {"u": user_id},
    ).all()
    for ident, email in rows:
        host = str(email).rsplit("@", 1)[-1] if "@" in str(email) else ""
        dom = registrable_domain(host) if host else ""
        if not dom:
            continue
        root = roots.get(int(ident), int(ident))
        d = acc.setdefault(dom, {})
        d[root] = d.get(root, 0) + 1
    return {dom: DomainUse(by_root) for dom, by_root in acc.items()}


def shared_platform_domains(conn: Connection, user_id: int) -> dict[str, set[int]]:
    """Dominios compartidos por ≥ 2 raíces sin una dominante: plataformas, no identidad de nadie."""
    return {d: set(u.by_root) for d, u in domain_usage(conn, user_id).items() if u.shared}


def owns_domain(name: str, domain: str) -> bool:
    """¿El núcleo del nombre de la org está en la etiqueta del dominio? `PayPal`/paypal.com,
    `Apple`/appleid.com, `Stripe`/stripe.com: el proveedor es dueño de su propio relay."""
    core = org_core(name).replace(" ", "")
    label = (registrable_domain(domain) or domain).split(".", 1)[0].replace("-", "")
    return bool(core) and len(core) >= 3 and core in label


def domain_rejection_reason(
    conn: Connection,
    user_id: int,
    domain: str,
    *,
    identity_id: int | None = None,
    usage: dict[str, DomainUse] | None = None,
    roots: dict[int, int] | None = None,
) -> str | None:
    """Por qué `domain` NO debe ser identificador `domain` (de `identity_id`, si se conoce), o None
    si puede. `usage`/`roots` precalculados para el barrido."""
    host = (domain or "").strip().lower()
    if not host:
        return None
    dom = registrable_domain(host) or host
    owner_name: str | None = None
    if identity_id is not None:
        owner_name = conn.execute(
            text("SELECT display_name FROM mod_identidades WHERE id = :i AND user_id = :u"),
            {"i": identity_id, "u": user_id},
        ).scalar()
    is_owner = (
        bool(owner_name) and dom not in PURE_RELAY_DOMAINS and owns_domain(str(owner_name), dom)
    )
    if (is_freemail(dom) or is_freemail(host)) and not is_owner:
        return REASON_FREEMAIL
    if is_relay_domain(dom) and not is_owner:
        return REASON_RELAY
    use = (usage if usage is not None else domain_usage(conn, user_id)).get(dom)
    if use is None:
        return None
    if use.shared:
        return REASON_SHARED
    if identity_id is not None and use.dominant is not None and len(use.by_root) >= 2:
        r = (roots if roots is not None else _roots(conn, user_id)).get(identity_id, identity_id)
        if r != use.dominant:
            return REASON_NOT_DOMINANT  # el dominio es de otra raíz (FONDEX con buzón @salesiana)
    return None


@dataclass(frozen=True)
class DroppedDomain:
    identity_id: int
    domain: str
    reason: str


def drop_invalid_domain_identifiers(conn: Connection, user_id: int) -> list[DroppedDomain]:
    """Barrido: quita los identificadores `domain` que no identifican a su organización (ver
    `domain_rejection_reason`). Los emails se conservan. Devuelve lo quitado."""
    usage = domain_usage(conn, user_id)
    roots = _roots(conn, user_id)
    rows = conn.execute(
        text(
            "SELECT id, identity_id, value_norm FROM mod_identidades_identifiers "
            "WHERE user_id = :u AND kind = 'domain' ORDER BY id"
        ),
        {"u": user_id},
    ).all()
    dropped: list[DroppedDomain] = []
    for xid, ident, value in rows:
        reason = domain_rejection_reason(
            conn, user_id, str(value), identity_id=int(ident), usage=usage, roots=roots
        )
        if reason is None:
            continue
        conn.execute(
            text("DELETE FROM mod_identidades_identifiers WHERE id = :id AND user_id = :u"),
            {"id": int(xid), "u": user_id},
        )
        dropped.append(DroppedDomain(int(ident), str(value), reason))
        _log.info(
            "identidades.domain.invalid_dropped",
            identity_id=int(ident),
            domain=str(value),
            reason=reason,
        )
    return dropped


@dataclass(frozen=True)
class RenamedSender:
    identity_id: int
    old_name: str
    new_name: str
    org_hint: str


def clean_sender_display_names(conn: Connection, user_id: int) -> list[RenamedSender]:
    """Principio «una cosa del mundo, un registro» sobre las fichas de REMITENTES de chat que ya
    existen: la que lleva el nombre visible tal cual («Nombre | Org», emojis, tipografías raras)
    pasa al nombre limpio, con el visible como alias y la organización pegada como pista
    (`metadata.org_hint`). Solo toca fichas de extracción cuyo nombre es EXACTAMENTE el visible de
    alguno de sus mensajes (prueba de que nadie lo editó a mano). Idempotente."""
    from memex.modules.identidades.normalize import split_display_name
    from memex.modules.identidades.senders import SENDER_PID_SQL, apply_display_name

    rows = conn.execute(
        text(
            f"""
            SELECT DISTINCT x.id, x.display_name
            FROM mod_identidades x
            JOIN mod_identidades_identifiers f
              ON f.user_id = x.user_id AND f.identity_id = x.id AND f.kind = 'platform_id'
            JOIN sources s ON s.user_id = x.user_id AND s.type = f.platform
            JOIN inbox i ON i.user_id = x.user_id AND i.source_id = s.id
             AND {SENDER_PID_SQL} = f.value_norm
             AND i.payload->'sender'->>'display_name' = x.display_name
            WHERE x.user_id = :u AND x.kind = 'persona' AND x.source = 'extraction'
            ORDER BY x.id
            """
        ),
        {"u": user_id},
    ).all()
    out: list[RenamedSender] = []
    for iid, raw in rows:
        name, org_hint = split_display_name(str(raw))
        if not name or name == str(raw):
            continue
        apply_display_name(conn, user_id, int(iid), str(raw))
        out.append(RenamedSender(int(iid), str(raw), name, org_hint))
    if out:
        _log.info("identidades.hygiene.senders_renamed", user_id=user_id, n=len(out))
    return out


@dataclass(frozen=True)
class DroppedOrphan:
    identity_id: int
    kind: str
    display_name: str


def drop_orphan_extraction_identities(conn: Connection, user_id: int) -> list[DroppedOrphan]:
    """Principio «propio o para todos» sobre el directorio: una ficha nacida por EXTRACCIÓN que ya
    no tiene NINGUNA mención que la sostenga (su evidencia se olvidó al reprocesar el mensaje con
    otra regla, y la nueva lectura la dejó como mención sin ficha), ni identificadores, ni hijos,
    ni afiliaciones, ni pagos, ni la marca de interés del dueño, vuelve a no existir. Las manuales
    y las de proveedores no se tocan; `drop_identity` además se niega si algo cuelga de ella."""
    from memex.modules.identidades.verify_llm import drop_identity

    rows = conn.execute(
        text(
            """
            SELECT i.id, i.kind, i.display_name
            FROM mod_identidades i
            WHERE i.user_id = :u AND i.source = 'extraction' AND i.provider IS NULL
              AND NOT i.interest
              AND NOT EXISTS (SELECT 1 FROM mod_identidades_mentions m
                              WHERE m.resolved_identity_id = i.id)
              AND NOT EXISTS (SELECT 1 FROM mod_identidades_identifiers f
                              WHERE f.identity_id = i.id)
              AND NOT EXISTS (SELECT 1 FROM mod_identidades c WHERE c.parent_identity_id = i.id)
              AND NOT EXISTS (SELECT 1 FROM mod_identidades_person_orgs po
                              WHERE po.person_id = i.id OR po.org_id = i.id)
              AND NOT EXISTS (SELECT 1 FROM mod_finance_consolidated f
                              WHERE f.counterparty_identity_id = i.id)
            ORDER BY i.id
            """
        ),
        {"u": user_id},
    ).all()
    out: list[DroppedOrphan] = []
    for iid, kind, name in rows:
        if drop_identity(conn, user_id, int(iid)):
            out.append(DroppedOrphan(int(iid), str(kind), str(name)))
    if out:
        _log.info("identidades.hygiene.orphans_dropped", user_id=user_id, n=len(out))
    return out


def drop_mention_only_identities(conn: Connection, user_id: int) -> list[DroppedOrphan]:
    """Principio «propio o para todos», segunda mitad: una ficha nacida por EXTRACCIÓN que hoy se
    sostiene SOLO por menciones sin vínculo (`metadata.link = 'mencion'` en TODAS sus menciones:
    fue nombrada, nunca le escribió, le cobró, la atendió ni la organizó nada) no es del
    directorio: sus menciones vuelven a avistamiento y la ficha se elimina. Es la regla de los
    chats no íntimos (`chat_rules.prune_chat_mention_identities`) para cualquier medio, posible
    desde que cada mención guarda el vínculo declarado; las menciones viejas sin `link` cuentan
    como vínculo (no se adivina). Sin identificadores, hijos, afiliaciones, pagos ni interés."""
    from memex.modules.identidades.verify_llm import drop_identity

    rows = conn.execute(
        text(
            """
            SELECT i.id, i.kind, i.display_name
            FROM mod_identidades i
            WHERE i.user_id = :u AND i.source = 'extraction' AND i.provider IS NULL
              AND NOT i.interest
              AND EXISTS (SELECT 1 FROM mod_identidades_mentions m
                          WHERE m.resolved_identity_id = i.id)
              AND NOT EXISTS (SELECT 1 FROM mod_identidades_mentions m
                              WHERE m.resolved_identity_id = i.id
                                AND COALESCE(m.metadata->>'link', '') <> 'mencion')
              AND NOT EXISTS (SELECT 1 FROM mod_identidades_identifiers f
                              WHERE f.identity_id = i.id)
              AND NOT EXISTS (SELECT 1 FROM mod_identidades c WHERE c.parent_identity_id = i.id)
              AND NOT EXISTS (SELECT 1 FROM mod_identidades_person_orgs po
                              WHERE po.person_id = i.id OR po.org_id = i.id)
              AND NOT EXISTS (SELECT 1 FROM mod_finance_consolidated f
                              WHERE f.counterparty_identity_id = i.id)
            ORDER BY i.id
            """
        ),
        {"u": user_id},
    ).all()
    out: list[DroppedOrphan] = []
    for iid, kind, name in rows:
        if drop_identity(conn, user_id, int(iid)):
            out.append(DroppedOrphan(int(iid), str(kind), str(name)))
    if out:
        _log.info("identidades.hygiene.mention_only_dropped", user_id=user_id, n=len(out))
    return out


@dataclass
class HygieneStats:
    aliases: list[tuple[int, str, int]] = field(default_factory=list)
    domains: list[DroppedDomain] = field(default_factory=list)
    senders: list[RenamedSender] = field(default_factory=list)
    orphans: list[DroppedOrphan] = field(default_factory=list)
    mention_only: list[DroppedOrphan] = field(default_factory=list)


def run_hygiene(conn: Connection, user_id: int) -> HygieneStats:
    """Todas las reglas de integridad, en una tx. Idempotente. Los remitentes se limpian ANTES de
    revisar alias: el nombre visible que pasa a alias puede chocar con otra ficha viva. Las
    fichas sin sostén (huérfanas, solo-mencionadas) van al final, con el directorio ya saneado."""
    senders = clean_sender_display_names(conn, user_id)
    aliases = drop_colliding_aliases(conn, user_id)
    domains = drop_invalid_domain_identifiers(conn, user_id)
    mention_only = drop_mention_only_identities(conn, user_id)
    return HygieneStats(
        aliases=aliases,
        domains=domains,
        senders=senders,
        orphans=drop_orphan_extraction_identities(conn, user_id),
        mention_only=mention_only,
    )
