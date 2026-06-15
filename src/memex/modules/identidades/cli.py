"""CLI `memex-identidades` — alta por tarjeta + consulta/jerarquía/resolución + sync + merges.

Subcomandos del AGENTE (expuestos vía `memex identidad <cmd>`):
  add          — resolve-or-create de una identidad desde una tarjeta de contacto: misma
                 resolución que la extracción, sin LLM.
  list         — enumera el directorio con filtros (kind/no-parent/interest/no-desc): el camino
                 para iterar todo sin un texto de búsqueda.
  search       — busca identidades por nombre, alias o identificador.
  show         — ficha completa: identificadores, jerarquía, afiliaciones, candidatos pendientes.
  relations    — aristas que tocan una identidad (sus relaciones en el grafo) y su estado.
  tree         — jerarquía de pertenencia («sub»: programa→universidad, producto→empresa).
  set-parent   — cuelga una identidad de su padre (o lo quita con --clear); marca
                 `parent_source='agent'` para que el organizador LLM no lo pise.
  confirm-parent — consolida el padre actual como confirmado (parent_source=agent) sin re-tipearlo.
  set-kind     — reclasifica el tipo (persona/organizacion/producto).
  add-id       — agrega un identificador (email/phone/handle/domain/url).
  affiliate    — teje una persona con una organización (afiliación).
  unify        — funde dos identidades del mismo kind sin pasar por la cola de candidatos.
  annotate     — agrega alias y/o descripción (nota); la VE el desempate LLM.
  relate       — confirma una relación entre dos identidades (no reaparece como pista).
  confirm-relation — promueve una pista existente a confirmada.
  unrelate     — descarta una arista (la marca rechazada).
  candidates   — lista los candidatos de merge pendientes (zona gris del difuso).
  resolve      — decide un candidato: --same fusiona (id menor sobrevive), --distinct coexisten.
  help         — resumen de los comandos.

Subcomandos de MANTENIMIENTO (solo `memex-identidades` directo):
  sync         — una pasada de sync (ingress) de una cuenta de proveedor (Google People).
  add-account  — registra (upsert) una cuenta de proveedor, vinculada a la cuenta del dashboard
                 (`accounts.id`) cuyo vault tiene el token Google.
  accounts     — lista las cuentas de proveedor de un user.
  interest     — `add` / `list` / `remove` de la lista manual de organizaciones de interés.
  merge        — desempate LLM (FASE 2) de los candidatos de merge de la zona gris del difuso.
  backfill-productos — reclasifica orgs→producto por voto de menciones (DRY-RUN por default;
                 `--apply` escribe). Determinista, sin LLM.

La AUTORIZACIÓN de Google NO se hace acá: se conecta la cuenta desde el dashboard (/cuenta), que
guarda el token cifrado en el vault (Decisión 6). Server-side: habla con la DB vía `connection()`,
igual que `memex-calendar-sync`. `sync`/`merge` necesitan la DB y (sync) la master key del vault
(MEMEX_SECRET_KEY); `merge` usa el LLM (DEEPSEEK_API_KEY) — inyectados por `doppler run`.

Exit code 0 si OK; 1 si error fatal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.cli.provider_flags import add_provider_flags, client_from_flags
from memex.db import connection
from memex.logging import get_logger, setup_logging
from memex.modules.identidades.backfill import apply_reclassification, find_product_candidates
from memex.modules.identidades.dedup_llm import run_merge_phase2
from memex.modules.identidades.hierarchy import would_create_cycle
from memex.modules.identidades.merge import merge_identities
from memex.modules.identidades.module import register_card
from memex.modules.identidades.normalize import norm_identifier
from memex.modules.identidades.providers import known_providers
from memex.modules.identidades.providers.base import ContactsProviderError
from memex.modules.identidades.senders import backfill_senders
from memex.modules.identidades.sync import run_sync
from memex.relations.deterministic import weave_pertenencia
from memex.relations.edges import (
    PRODUCER_HUMANO,
    PROVENANCE_EXTRACTED,
    VERDICT_AMBIGUOUS,
    VERDICT_CONFIRMED,
    VERDICT_REJECTED,
    Ref,
    edges_touching,
    propose_edge,
    resolve_edge,
)
from memex.relations.vertices import IDENTITY_SLUG_BY_KIND


def _safe(text_: str) -> str:
    """Sanea un string para el encoding de la consola actual (cp1252 en Windows)."""
    enc = sys.stdout.encoding or "utf-8"
    return text_.encode(enc, errors="replace").decode(enc, errors="replace")


def _say(msg: str, *, err: bool = False) -> None:
    print(_safe(msg), file=sys.stderr if err else sys.stdout)


def _emit_json(obj: Any) -> None:
    """La fila pública como ÚLTIMA línea de stdout (las previas son logs). `default=str` serializa
    datetimes sin acoplar a un encoder propio (igual que bienestar/finance)."""
    print(_safe(json.dumps(obj, default=str, ensure_ascii=False)))


_HELP = """memex-identidades — directorio de personas, organizaciones y productos (resolución
determinista).

Comandos del agente:
  add          registra/resuelve una tarjeta de contacto (resolve-or-create, no duplica)
  list         enumera el directorio con filtros (--kind/--no-parent/--interest/--no-desc/--limit)
  search       busca por nombre, alias o identificador (--q, opcional --kind)
  show         ficha completa: identificadores, jerarquía, afiliaciones, candidatos (--id)
  relations    aristas que tocan una identidad: qué relaciones tiene y su estado (--id)
  tree         jerarquía de pertenencia: quién pertenece a quién (opcional --id como raíz)
  set-parent   cuelga --id de --parent («pertenece a»), o quita el padre con --clear
  confirm-parent  consolida el padre actual como confirmado (parent_source=agent)
  set-kind     reclasifica --id a --kind (persona|organizacion|producto)
  add-id       agrega un identificador (--kind email|phone|handle|domain|url --value)
  affiliate    teje una persona (--person) con una organización (--org), opcional --role
  unify        funde dos identidades del mismo kind: --into sobrevive, --from se absorbe
  annotate     agrega --alias y/o --note a --id (la nota = descripción; la ve el desempate LLM)
  relate       confirma una relación entre --from y --to (--type libre); no reaparece como pista
  confirm-relation  promueve una pista existente a confirmada (--edge)
  unrelate     descarta una arista (--edge): la marca rechazada
  candidates   pares dudosos pendientes de decisión (¿misma identidad real?)
  resolve      decide un candidato: --same fusiona / --distinct coexisten (--why auditoría)
  help         muestra esta ayuda

Mantenimiento (no del agente; usar 'memex-identidades' directo, no 'memex identidad'):
  sync · add-account · accounts · interest · merge · backfill-productos · backfill-senders

add — desde una tarjeta de contacto / vCard (la lee el agente, no memex):
  memex-identidades add --name "<nombre>" --kind <persona|organizacion|producto> [--email <e>]
      [--phone <t>] [--handle <@>] [--org "<empresa>"] [--role "<rol>"] [--json]
  - resuelve contra el directorio (señales fuertes + difuso) y crea si no existe; idempotente.
  - --org (solo personas) teje la afiliación persona↔organización.

resolve — OJO: --same fusiona de verdad (la identidad de id mayor se absorbe y desaparece;
  sus alias/identificadores pasan a la superviviente). Ante la duda, --distinct (recuperable).

--json: la fila pública es la ÚLTIMA línea de stdout (las previas son logs).
Flags de cada comando: memex-identidades <comando> -h"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-identidades")
    sub = parser.add_subparsers(dest="cmd", required=True)

    add_id = sub.add_parser("add", help="Registra/resuelve una identidad desde una tarjeta.")
    add_id.add_argument("--user", type=int, default=1, help="User id (default 1).")
    add_id.add_argument(
        "--name", required=True, help="Nombre de la persona, organización o producto."
    )
    add_id.add_argument(
        "--kind",
        required=True,
        choices=["persona", "organizacion", "producto"],
        help="Tipo de identidad.",
    )
    add_id.add_argument("--email", help="Email de contacto.")
    add_id.add_argument("--phone", help="Teléfono de contacto.")
    add_id.add_argument("--handle", help="Handle/usuario (ej. @ada).")
    add_id.add_argument(
        "--org", help="Empresa de la persona (teje la afiliación; solo --kind persona)."
    )
    add_id.add_argument("--role", help="Rol/cargo en la empresa (con --org).")
    add_id.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite la fila pública como JSON."
    )

    sub.add_parser("help", help="Resumen de los comandos (para descubrir la CLI).")

    list_p = sub.add_parser("list", help="Enumera el directorio con filtros.")
    list_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    list_p.add_argument(
        "--kind",
        choices=["persona", "organizacion", "producto"],
        help="Filtra por tipo de identidad.",
    )
    list_p.add_argument(
        "--no-parent", action="store_true", help="Solo las que NO cuelgan de un padre (jerarquía)."
    )
    list_p.add_argument("--interest", action="store_true", help="Solo las marcadas como interés.")
    list_p.add_argument(
        "--no-desc", action="store_true", help="Solo las SIN descripción (notes vacío)."
    )
    list_p.add_argument("--limit", type=int, default=50, help="Máximo de resultados (default 50).")
    list_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite los resultados como JSON."
    )

    search_p = sub.add_parser("search", help="Busca identidades por nombre, alias o identificador.")
    search_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    search_p.add_argument(
        "--q", required=True, help="Texto a buscar (nombre, alias o id. tipo email)."
    )
    search_p.add_argument(
        "--kind",
        choices=["persona", "organizacion", "producto"],
        help="Filtra por tipo de identidad.",
    )
    search_p.add_argument(
        "--limit", type=int, default=20, help="Máximo de resultados (default 20)."
    )
    search_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite los resultados como JSON."
    )

    show_p = sub.add_parser("show", help="Ficha completa de una identidad.")
    show_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    show_p.add_argument("--id", type=int, required=True, help="Id de la identidad.")
    show_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite la ficha como JSON."
    )

    tree_p = sub.add_parser(
        "tree", help="Jerarquía de pertenencia («sub»: quién pertenece a quién)."
    )
    tree_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    tree_p.add_argument(
        "--id", type=int, help="Raíz del subárbol (default: todos los árboles con hijos)."
    )
    tree_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite el árbol como JSON."
    )

    sp_p = sub.add_parser("set-parent", help="Cuelga una identidad de su padre («pertenece a»).")
    sp_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    sp_p.add_argument("--id", type=int, required=True, help="Identidad hija.")
    sp_group = sp_p.add_mutually_exclusive_group(required=True)
    sp_group.add_argument("--parent", type=int, help="Id del padre.")
    sp_group.add_argument("--clear", action="store_true", help="Quita el padre actual.")
    sp_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite el resultado como JSON."
    )

    ann_p = sub.add_parser(
        "annotate", help="Agrega alias y/o nota a una identidad (contexto para la resolución)."
    )
    ann_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    ann_p.add_argument("--id", type=int, required=True, help="Id de la identidad.")
    ann_p.add_argument("--alias", action="append", default=[], help="Alias a agregar (repetible).")
    ann_p.add_argument(
        "--note", help="Descripción/nota a anexar a la identidad (la ve el desempate LLM)."
    )
    ann_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite la fila como JSON."
    )

    rel_p = sub.add_parser("relations", help="Aristas que tocan una identidad (sus relaciones).")
    rel_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    rel_p.add_argument("--id", type=int, required=True, help="Id de la identidad.")
    rel_p.add_argument("--verdict", help="Filtra por veredicto (ambiguous/confirmed/rejected).")
    rel_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite las aristas como JSON."
    )

    cp_p = sub.add_parser(
        "confirm-parent", help="Consolida el padre actual como confirmado (parent_source=agent)."
    )
    cp_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    cp_p.add_argument("--id", type=int, required=True, help="Identidad cuyo padre se confirma.")
    cp_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite el resultado como JSON."
    )

    sk_p = sub.add_parser("set-kind", help="Reclasifica el tipo de una identidad.")
    sk_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    sk_p.add_argument("--id", type=int, required=True, help="Id de la identidad.")
    sk_p.add_argument(
        "--kind",
        required=True,
        choices=["persona", "organizacion", "producto"],
        help="Nuevo tipo.",
    )
    sk_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite la fila como JSON."
    )

    aid_p = sub.add_parser("add-id", help="Agrega un identificador a una identidad.")
    aid_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    aid_p.add_argument("--id", type=int, required=True, help="Id de la identidad.")
    aid_p.add_argument(
        "--kind",
        required=True,
        choices=["email", "phone", "handle", "domain", "url"],
        help="Tipo de identificador.",
    )
    aid_p.add_argument("--value", required=True, help="Valor del identificador.")
    aid_p.add_argument(
        "--platform", help="Plataforma (default: el kind; ej. 'instagram' para un handle)."
    )
    aid_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite la fila como JSON."
    )

    aff_p = sub.add_parser("affiliate", help="Teje una persona con una organización.")
    aff_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    aff_p.add_argument("--person", type=int, required=True, help="Id de la persona.")
    aff_p.add_argument("--org", type=int, required=True, help="Id de la organización.")
    aff_p.add_argument("--role", help="Rol/cargo (opcional).")
    aff_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite el resultado como JSON."
    )

    uni_p = sub.add_parser("unify", help="Funde dos identidades del mismo kind (sin candidato).")
    uni_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    uni_p.add_argument("--into", type=int, required=True, help="Identidad que SOBREVIVE.")
    uni_p.add_argument("--from", dest="from_id", type=int, required=True, help="La que se absorbe.")
    uni_p.add_argument("--why", default="", help="Justificación (auditoría).")
    uni_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite el resultado como JSON."
    )

    rlt_p = sub.add_parser("relate", help="Confirma una relación entre dos identidades.")
    rlt_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    rlt_p.add_argument("--from", dest="from_id", type=int, required=True, help="Identidad origen.")
    rlt_p.add_argument("--to", dest="to_id", type=int, required=True, help="Identidad destino.")
    rlt_p.add_argument(
        "--type", dest="rel_type", default="relacionado_con", help="Tipo de relación (libre)."
    )
    rlt_p.add_argument("--why", default="", help="Evidencia/justificación (auditoría).")
    rlt_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite el resultado como JSON."
    )

    crl_p = sub.add_parser(
        "confirm-relation", help="Promueve una pista existente a relación confirmada."
    )
    crl_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    crl_p.add_argument("--edge", type=int, required=True, help="Id de la arista (ver 'relations').")
    crl_p.add_argument("--why", default="", help="Evidencia/justificación (auditoría).")
    crl_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite el resultado como JSON."
    )

    url_p = sub.add_parser("unrelate", help="Descarta una arista (la marca rechazada).")
    url_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    url_p.add_argument("--edge", type=int, required=True, help="Id de la arista (ver 'relations').")
    url_p.add_argument("--why", default="", help="Justificación (auditoría).")
    url_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite el resultado como JSON."
    )

    res_p = sub.add_parser(
        "resolve", help="Decide un candidato de merge: misma identidad o distintas."
    )
    res_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    res_p.add_argument(
        "--candidate", type=int, required=True, help="Id del candidato (ver 'candidates')."
    )
    res_group = res_p.add_mutually_exclusive_group(required=True)
    res_group.add_argument(
        "--same", action="store_true", help="Misma identidad: fusiona (la de id mayor se absorbe)."
    )
    res_group.add_argument(
        "--distinct", action="store_true", help="Distintas: coexisten (candidato rechazado)."
    )
    res_p.add_argument("--why", default="", help="Justificación (queda en la auditoría).")
    res_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite el resultado como JSON."
    )

    sync_p = sub.add_parser("sync", help="Sincroniza (ingress) una cuenta de contactos externa.")
    sync_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    sync_p.add_argument("--account", type=int, required=True, help="Id de la cuenta de proveedor.")
    sync_p.add_argument(
        "--full", action="store_true", help="Ignora el cursor y trae todo (full resync)."
    )

    add_p = sub.add_parser("add-account", help="Registra (upsert) una cuenta de proveedor.")
    add_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    add_p.add_argument("--provider", default="google", help=f"Proveedor ({known_providers()}).")
    add_p.add_argument("--label", required=True, help="Etiqueta visible (ej. email de la cuenta).")
    add_p.add_argument(
        "--account-id",
        type=int,
        required=True,
        help="Id de la cuenta del dashboard (accounts.id) cuyo vault tiene el token Google.",
    )

    acc_p = sub.add_parser("accounts", help="Lista las cuentas de proveedor de un user.")
    acc_p.add_argument("--user", type=int, default=1, help="User id (default 1).")

    int_p = sub.add_parser("interest", help="Gestiona la lista de organizaciones de interés.")
    int_sub = int_p.add_subparsers(dest="interest_cmd", required=True)

    iadd = int_sub.add_parser("add", help="Agrega/actualiza una organización de interés.")
    iadd.add_argument("--user", type=int, default=1, help="User id (default 1).")
    iadd.add_argument("--name", required=True, help="Nombre canónico (ej. 'Unity', 'Claude').")
    iadd.add_argument(
        "--alias", action="append", default=[], help="Alias (repetible). Ej. --alias claude.ai."
    )
    iadd.add_argument(
        "--domain", action="append", default=[], help="Dominio de email (repetible). Ej. unity.com."
    )
    iadd.add_argument("--description", default="", help="Nota corta opcional.")

    ilist = int_sub.add_parser("list", help="Lista la lista de interés de un user.")
    ilist.add_argument("--user", type=int, default=1, help="User id (default 1).")

    irm = int_sub.add_parser("remove", help="Quita una identidad de interés por id.")
    irm.add_argument("--user", type=int, default=1, help="User id (default 1).")
    irm.add_argument("--id", type=int, required=True, help="Id de la identidad a quitar.")

    merge_p = sub.add_parser("merge", help="Desempate LLM de los candidatos de merge (FASE 2).")
    merge_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    merge_p.add_argument("--limit", type=int, default=200, help="Máximo de pares a resolver.")
    add_provider_flags(merge_p)

    cand_p = sub.add_parser("candidates", help="Lista los candidatos de merge pendientes.")
    cand_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    cand_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite la lista como JSON."
    )

    bf_p = sub.add_parser(
        "backfill-productos",
        help="Reclasifica orgs→producto por voto de menciones (dry-run sin --apply).",
    )
    bf_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    bf_p.add_argument(
        "--apply",
        action="store_true",
        help="Aplica la reclasificación. Sin esto solo imprime la lista (dry-run).",
    )

    bs_p = sub.add_parser(
        "backfill-senders",
        help="Resuelve+persiste el remitente (sin LLM) de mensajes ya procesados pre-Fase-2.",
    )
    bs_p.add_argument("--user", type=int, default=1, help="User id (default 1).")

    return parser


def register_add_from_args(
    conn: Connection, user_id: int, args: argparse.Namespace, *, event_id: str | None = None
) -> dict[str, Any]:
    """Mapea `args` (ya parseados) → `register_card` sobre un `conn` DADO. Lo reusan `_cmd_add` (que
    abre su propia tx) y el cierre de evento del agente (tx compartida, que pasa su `event_id` para
    que la identidad correlacione vía `mismo_evento` con los otros hechos del evento)."""
    return register_card(
        conn,
        user_id,
        name=args.name,
        kind=args.kind,
        email=args.email,
        handle=args.handle,
        phone=args.phone,
        org=args.org,
        role=args.role,
        event_id=event_id,
    )


def _cmd_add(args: argparse.Namespace) -> int:
    """Resolve-or-create de una identidad desde una tarjeta de contacto. Delega en
    `register_card` (misma resolución que la extracción; sin LLM). Idempotente."""
    if args.org and args.kind != "persona":
        _say("--org solo aplica a --kind persona (la afiliación es persona↔org).", err=True)
        return 1
    with connection() as conn:
        row = register_add_from_args(conn, args.user, args)
    if args.as_json:
        _emit_json(row)
    else:
        verbo = "creada" if row["method"] == "created" else f"resuelta ({row['method']})"
        org = row.get("org")
        afil = f" · afiliada a #{org['id']} {org['display_name']!r}" if org else ""
        _say(f"\n{verbo}: id={row['id']} {row['display_name']!r} ({row['kind']}){afil}.\n")
    return 0


def _fmt_brief(r: dict[str, Any]) -> str:
    """Una identidad en una línea (para search/listas): id, tipo, nombre, alias, padre, ids."""
    alias = f" alias=[{', '.join(r['aliases'])}]" if r.get("aliases") else ""
    parent = f" padre=#{r['parent_id']} {r['parent_name']!r}" if r.get("parent_id") else ""
    idf = f" ids=[{', '.join(r['identifiers'])}]" if r.get("identifiers") else ""
    interest = " · interés" if r.get("interest") else ""
    return f"  [{r['id']}] ({r['kind']}) {r['display_name']}{alias}{parent}{idf}{interest}"


def _cmd_list(args: argparse.Namespace) -> int:
    """Enumera el directorio con filtros (el camino para iterar todo el directorio sin un texto de
    búsqueda). Mismas columnas/formato que `search`."""
    where = ["i.user_id = :uid"]
    params: dict[str, Any] = {"uid": args.user, "limit": args.limit}
    if args.kind:
        where.append("i.kind = :kind")
        params["kind"] = args.kind
    if args.no_parent:
        where.append("i.parent_identity_id IS NULL")
    if args.interest:
        where.append("i.interest")
    if args.no_desc:
        where.append("btrim(i.notes) = ''")
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    f"""
                    SELECT i.id, i.kind, i.display_name, i.aliases, i.interest,
                           i.parent_identity_id AS parent_id, p.display_name AS parent_name,
                           (SELECT array_agg(platform || ':' || value_norm)
                              FROM mod_identidades_identifiers
                             WHERE identity_id = i.id) AS identifiers
                    FROM mod_identidades i
                    LEFT JOIN mod_identidades p ON p.id = i.parent_identity_id
                    WHERE {" AND ".join(where)}
                    ORDER BY i.kind, i.display_name LIMIT :limit
                    """
                ),
                params,
            )
            .mappings()
            .all()
        )
    items = [dict(r) for r in rows]
    if args.as_json:
        _emit_json({"count": len(items), "items": items})
        return 0
    if not items:
        _say(f"\nSin identidades que cumplan el filtro (user {args.user}).\n")
        return 0
    _say(f"\n{len(items)} identidad(es) (user {args.user}):")
    for r in items:
        _say(_fmt_brief(r))
    _say("")
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    """Busca por nombre/alias (ILIKE) o identificador normalizado (email, handle, dominio…)."""
    where = [
        "i.user_id = :uid",
        "(i.display_name ILIKE :q OR array_to_string(i.aliases, ' ') ILIKE :q "
        "OR EXISTS (SELECT 1 FROM mod_identidades_identifiers f "
        "           WHERE f.identity_id = i.id AND f.value_norm ILIKE :q))",
    ]
    params: dict[str, Any] = {"uid": args.user, "q": f"%{args.q}%", "limit": args.limit}
    if args.kind:
        where.append("i.kind = :kind")
        params["kind"] = args.kind
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    f"""
                    SELECT i.id, i.kind, i.display_name, i.aliases, i.interest,
                           i.parent_identity_id AS parent_id, p.display_name AS parent_name,
                           (SELECT array_agg(platform || ':' || value_norm)
                              FROM mod_identidades_identifiers
                             WHERE identity_id = i.id) AS identifiers
                    FROM mod_identidades i
                    LEFT JOIN mod_identidades p ON p.id = i.parent_identity_id
                    WHERE {" AND ".join(where)}
                    ORDER BY i.id LIMIT :limit
                    """
                ),
                params,
            )
            .mappings()
            .all()
        )
    items = [dict(r) for r in rows]
    if args.as_json:
        _emit_json({"count": len(items), "items": items})
        return 0
    if not items:
        _say(f"\nSin resultados para {args.q!r} (user {args.user}).\n")
        return 0
    _say(f"\n{len(items)} resultado(s) para {args.q!r}:")
    for r in items:
        _say(_fmt_brief(r))
    _say("")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    """Ficha completa: fila + identificadores + padre/subs + afiliaciones + candidatos +
    menciones."""
    with connection() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT i.id, i.kind, i.display_name, i.aliases, i.interest, i.source, i.notes,
                           i.given_name, i.family_name, i.birthday,
                           i.parent_identity_id AS parent_id, p.display_name AS parent_name,
                           i.metadata->>'parent_source' AS parent_source,
                           i.created_at, i.updated_at
                    FROM mod_identidades i
                    LEFT JOIN mod_identidades p ON p.id = i.parent_identity_id
                    WHERE i.id = :id AND i.user_id = :uid
                    """
                ),
                {"id": args.id, "uid": args.user},
            )
            .mappings()
            .first()
        )
        if row is None:
            _say(f"\nNo existe la identidad id={args.id} para el user {args.user}.\n", err=True)
            return 1
        identifiers = (
            conn.execute(
                text(
                    "SELECT platform, kind, value, is_primary FROM mod_identidades_identifiers "
                    "WHERE identity_id = :id ORDER BY id"
                ),
                {"id": args.id},
            )
            .mappings()
            .all()
        )
        children = (
            conn.execute(
                text(
                    "SELECT id, kind, display_name FROM mod_identidades "
                    "WHERE parent_identity_id = :id AND user_id = :uid ORDER BY display_name"
                ),
                {"id": args.id, "uid": args.user},
            )
            .mappings()
            .all()
        )
        affiliations = (
            conn.execute(
                text(
                    """
                    SELECT po.role, per.id AS person_id, per.display_name AS person_name,
                           org.id AS org_id, org.display_name AS org_name
                    FROM mod_identidades_person_orgs po
                    JOIN mod_identidades per ON per.id = po.person_id
                    JOIN mod_identidades org ON org.id = po.org_id
                    WHERE po.user_id = :uid AND (po.person_id = :id OR po.org_id = :id)
                    ORDER BY po.id
                    """
                ),
                {"id": args.id, "uid": args.user},
            )
            .mappings()
            .all()
        )
        candidates = (
            conn.execute(
                text(
                    """
                    SELECT c.id, c.score,
                           CASE WHEN c.identity_a_id = :id
                                THEN c.identity_b_id ELSE c.identity_a_id END AS other_id,
                           CASE WHEN c.identity_a_id = :id
                                THEN b.display_name ELSE a.display_name END AS other_name
                    FROM mod_identidades_merge_candidates c
                    JOIN mod_identidades a ON a.id = c.identity_a_id
                    JOIN mod_identidades b ON b.id = c.identity_b_id
                    WHERE c.user_id = :uid AND c.status = 'candidate'
                      AND (c.identity_a_id = :id OR c.identity_b_id = :id)
                    ORDER BY c.id
                    """
                ),
                {"id": args.id, "uid": args.user},
            )
            .mappings()
            .all()
        )
        mention_count = conn.execute(
            text(
                "SELECT count(*) FROM mod_identidades_mentions "
                "WHERE user_id = :uid AND resolved_identity_id = :id"
            ),
            {"id": args.id, "uid": args.user},
        ).scalar_one()
    ficha: dict[str, Any] = {
        **dict(row),
        "identifiers": [dict(r) for r in identifiers],
        "children": [dict(r) for r in children],
        "affiliations": [dict(r) for r in affiliations],
        "merge_candidates": [dict(r) for r in candidates],
        "mention_count": int(mention_count),
    }
    if args.as_json:
        _emit_json(ficha)
        return 0
    interest = " · interés" if row["interest"] else ""
    _say(f"\n#{row['id']} {row['display_name']} ({row['kind']}, fuente={row['source']}){interest}")
    if row["aliases"]:
        _say(f"  alias: {', '.join(row['aliases'])}")
    if row["notes"]:
        _say(f"  notas: {row['notes']}")
    if identifiers:
        idf = ", ".join(
            f"{r['platform']}:{r['value']}{' (primario)' if r['is_primary'] else ''}"
            for r in identifiers
        )
        _say(f"  identificadores: {idf}")
    if row["parent_id"]:
        src = f" [{row['parent_source']}]" if row["parent_source"] else ""
        _say(f"  pertenece a: #{row['parent_id']} {row['parent_name']!r}{src}")
    if children:
        subs = ", ".join(f"#{r['id']} {r['display_name']!r}" for r in children)
        _say(f"  subs: {subs}")
    for a in affiliations:
        role = f" ({a['role']})" if a["role"] else ""
        if int(a["person_id"]) == args.id:
            _say(f"  afiliada a: #{a['org_id']} {a['org_name']!r}{role}")
        else:
            _say(f"  miembro: #{a['person_id']} {a['person_name']!r}{role}")
    for c in candidates:
        score = f"{float(c['score']):.2f}" if c["score"] is not None else "?"
        _say(
            f"  ¿misma que? candidato [{c['id']}] vs #{c['other_id']} {c['other_name']!r} "
            f"(score={score}) — decidir con 'resolve'"
        )
    _say(f"  menciones: {mention_count}\n")
    return 0


def _cmd_tree(args: argparse.Namespace) -> int:
    """Bosque de pertenencia. Sin --id: solo los árboles con hijos (+ conteo de sueltas); con --id:
    el subárbol de esa identidad. Incluye personas si participan de la jerarquía (raro)."""
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT id, kind, display_name, parent_identity_id AS parent_id,
                           metadata->>'parent_source' AS parent_source
                    FROM mod_identidades
                    WHERE user_id = :uid
                      AND (kind IN ('organizacion','producto')
                           OR parent_identity_id IS NOT NULL
                           OR id IN (SELECT parent_identity_id FROM mod_identidades
                                     WHERE user_id = :uid AND parent_identity_id IS NOT NULL))
                    ORDER BY display_name
                    """
                ),
                {"uid": args.user},
            )
            .mappings()
            .all()
        )
    by_id = {int(r["id"]): r for r in rows}
    children: dict[int, list[int]] = {}
    for r in rows:
        if r["parent_id"] is not None and int(r["parent_id"]) in by_id:
            children.setdefault(int(r["parent_id"]), []).append(int(r["id"]))

    def _node(nid: int) -> dict[str, Any]:
        r = by_id[nid]
        return {
            "id": nid,
            "kind": r["kind"],
            "display_name": r["display_name"],
            "parent_source": r["parent_source"],
            "children": [_node(c) for c in children.get(nid, [])],
        }

    def _print(nid: int, depth: int) -> None:
        r = by_id[nid]
        tag = " [producto]" if r["kind"] == "producto" else ""
        tag += " [persona]" if r["kind"] == "persona" else ""
        src = f" ({r['parent_source']})" if depth > 0 and r["parent_source"] else ""
        _say(f"  {'    ' * depth}{'└─ ' if depth else ''}#{nid} {r['display_name']}{tag}{src}")
        for c in children.get(nid, []):
            _print(c, depth + 1)

    if args.id is not None:
        if args.id not in by_id:
            _say(
                f"\nLa identidad id={args.id} no existe o no participa de la jerarquía "
                f"(user {args.user}).\n",
                err=True,
            )
            return 1
        if args.as_json:
            _emit_json(_node(args.id))
            return 0
        _say("")
        _print(args.id, 0)
        _say("")
        return 0

    roots = [
        int(r["id"]) for r in rows if (r["parent_id"] is None or int(r["parent_id"]) not in by_id)
    ]
    with_children = [nid for nid in roots if children.get(nid)]
    loose = len(roots) - len(with_children)
    if args.as_json:
        _emit_json({"trees": [_node(nid) for nid in with_children], "sin_jerarquia": loose})
        return 0
    if not with_children:
        _say(f"\nSin jerarquía de pertenencia todavía (user {args.user}; {loose} sueltas).\n")
        return 0
    _say(f"\nJerarquía de pertenencia (user {args.user}):")
    for nid in with_children:
        _print(nid, 0)
    _say(f"\n  ({loose} entradas sin jerarquía)\n")
    return 0


def _cmd_set_parent(args: argparse.Namespace) -> int:
    """Setea/quita el padre con las MISMAS validaciones que el PATCH del API (existencia, no
    self-parent, anti-ciclo multinivel) y marca `parent_source='agent'`: el organizador LLM
    (`run_organize`) no pisa los padres manual/agent."""
    with connection() as conn:
        child = conn.execute(
            text("SELECT display_name FROM mod_identidades WHERE id = :id AND user_id = :uid"),
            {"id": args.id, "uid": args.user},
        ).scalar()
        if child is None:
            _say(f"\nNo existe la identidad id={args.id} para el user {args.user}.\n", err=True)
            return 1
        if args.clear:
            parent_name = None
        else:
            parent_name = conn.execute(
                text("SELECT display_name FROM mod_identidades WHERE id = :p AND user_id = :uid"),
                {"p": args.parent, "uid": args.user},
            ).scalar()
            if parent_name is None:
                _say(f"\nNo existe el padre id={args.parent} (user {args.user}).\n", err=True)
                return 1
            if args.parent == args.id:
                _say("\nUna identidad no puede ser su propio padre.\n", err=True)
                return 1
            if would_create_cycle(conn, args.user, args.id, args.parent):
                _say(
                    f"\nColgar #{args.id} de #{args.parent} crearía un ciclo de pertenencia.\n",
                    err=True,
                )
                return 1
        conn.execute(
            text(
                """
                UPDATE mod_identidades
                SET parent_identity_id = :p,
                    metadata = jsonb_set(metadata, '{parent_source}',
                                         to_jsonb(CAST('agent' AS TEXT))),
                    updated_at = NOW()
                WHERE id = :id AND user_id = :uid
                """
            ),
            {"p": None if args.clear else args.parent, "id": args.id, "uid": args.user},
        )
        # Tejer «pertenece_a» al setear el padre (paso 5, misma tx). Quitar el padre deja la arista
        # vieja stale: la limpia `reconcile_graph`, no este punto.
        if not args.clear:
            weave_pertenencia(conn, args.user, args.id)
    result = {
        "id": args.id,
        "display_name": child,
        "parent_id": None if args.clear else args.parent,
        "parent_name": parent_name,
        "parent_source": "agent",
    }
    if args.as_json:
        _emit_json(result)
    elif args.clear:
        _say(f"\nPadre quitado: #{args.id} {child!r} queda sin pertenencia.\n")
    else:
        _say(f"\nListo: #{args.id} {child!r} pertenece a #{args.parent} {parent_name!r}.\n")
    return 0


def _cmd_annotate(args: argparse.Namespace) -> int:
    """Agrega alias (dedup, sin repetir el display_name) y/o anexa una nota. Las notas entran a la
    vista del desempate LLM: es el canal para dejar contexto persistente de resolución."""
    aliases = [a.strip() for a in args.alias if a.strip()]
    note = (args.note or "").strip()
    if not aliases and not note:
        _say("\nNada para anotar: pasá --alias y/o --note.\n", err=True)
        return 1
    with connection() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT display_name, aliases, notes FROM mod_identidades "
                    "WHERE id = :id AND user_id = :uid"
                ),
                {"id": args.id, "uid": args.user},
            )
            .mappings()
            .first()
        )
        if row is None:
            _say(f"\nNo existe la identidad id={args.id} para el user {args.user}.\n", err=True)
            return 1
        new_aliases = list(row["aliases"] or ())
        for a in aliases:
            if a != row["display_name"] and a not in new_aliases:
                new_aliases.append(a)
        new_notes = str(row["notes"] or "")
        if note:
            new_notes = f"{new_notes}\n{note}" if new_notes.strip() else note
        conn.execute(
            text(
                "UPDATE mod_identidades SET aliases = :aliases, notes = :notes, "
                "updated_at = NOW() WHERE id = :id AND user_id = :uid"
            ),
            {"aliases": new_aliases, "notes": new_notes, "id": args.id, "uid": args.user},
        )
    result = {
        "id": args.id,
        "display_name": row["display_name"],
        "aliases": new_aliases,
        "notes": new_notes,
    }
    if args.as_json:
        _emit_json(result)
    else:
        _say(f"\nAnotada #{args.id} {row['display_name']!r}: alias=[{', '.join(new_aliases)}]")
        if new_notes:
            _say(f"  notas: {new_notes}")
        _say("")
    return 0


def _identity_ref(conn: Connection, user_id: int, identity_id: int) -> tuple[Ref, str] | None:
    """`(Ref del vértice, display_name)` de una identidad, o None si no existe. El slug del vértice
    sale del kind (`IDENTITY_SLUG_BY_KIND`)."""
    row = conn.execute(
        text("SELECT kind, display_name FROM mod_identidades WHERE id = :id AND user_id = :u"),
        {"id": identity_id, "u": user_id},
    ).first()
    if row is None:
        return None
    return Ref(IDENTITY_SLUG_BY_KIND[str(row[0])], identity_id), str(row[1])


def _cmd_relations(args: argparse.Namespace) -> int:
    """Lista las aristas que tocan la identidad (en cualquier dirección), resolviendo el nombre del
    otro extremo cuando es una identidad. La superficie para ver/auditar las relaciones de algo."""
    with connection() as conn:
        ref = _identity_ref(conn, args.user, args.id)
        if ref is None:
            _say(f"\nNo existe la identidad id={args.id} para el user {args.user}.\n", err=True)
            return 1
        self_ref, self_name = ref
        edges = edges_touching(conn, args.user, self_ref, verdict=args.verdict)
        # Resolver nombres de los extremos identidad (lookup batch).
        ident_ids = {e.src.id for e in edges if e.src.slug.startswith("identidades:")} | {
            e.dst.id for e in edges if e.dst.slug.startswith("identidades:")
        }
        names: dict[int, str] = {}
        if ident_ids:
            for r in conn.execute(
                text("SELECT id, display_name FROM mod_identidades WHERE id = ANY(:ids)"),
                {"ids": sorted(ident_ids)},
            ).mappings():
                names[int(r["id"])] = str(r["display_name"])

    def _endpoint(r: Ref) -> str:
        if r.slug.startswith("identidades:") and r.id in names:
            return f"#{r.id} {names[r.id]!r}"
        return f"{r.slug}:{r.id}"

    items = []
    for e in edges:
        outgoing = (e.src.slug, e.src.id) == (self_ref.slug, self_ref.id)
        other = e.dst if outgoing else e.src
        items.append(
            {
                "edge_id": e.id,
                "direction": "→" if outgoing else "←",
                "relation_type": e.relation_type,
                "producer": e.producer,
                "provenance": e.provenance,
                "verdict": e.verdict,
                "label": e.label,
                "relation": e.relation,
                "other": _endpoint(other),
            }
        )
    if args.as_json:
        _emit_json({"identity": {"id": args.id, "display_name": self_name}, "edges": items})
        return 0
    if not items:
        _say(f"\n#{args.id} {self_name!r} no tiene relaciones todavía.\n")
        return 0
    _say(f"\nRelaciones de #{args.id} {self_name!r}:")
    for it in items:
        _say(
            f"  [{it['edge_id']}] {it['direction']} {it['other']} "
            f"· {it['relation_type'] or '(sin tipo)'} ({it['producer']}/{it['status']})"
        )
    _say("")
    return 0


def _cmd_confirm_parent(args: argparse.Namespace) -> int:
    """Re-asienta el padre ACTUAL marcándolo `parent_source='agent'`: consolida como confirmada una
    jerarquía que puso el LLM (sin tener que tipear el id del padre). El organizador LLM ya no la
    pisa. No-op con mensaje si la identidad no tiene padre."""
    with connection() as conn:
        row = conn.execute(
            text(
                "SELECT i.display_name, i.parent_identity_id, p.display_name AS parent_name "
                "FROM mod_identidades i LEFT JOIN mod_identidades p ON p.id = i.parent_identity_id "
                "WHERE i.id = :id AND i.user_id = :u"
            ),
            {"id": args.id, "u": args.user},
        ).first()
        if row is None:
            _say(f"\nNo existe la identidad id={args.id} para el user {args.user}.\n", err=True)
            return 1
        if row[1] is None:
            _say(
                f"\n#{args.id} {row[0]!r} no tiene padre que confirmar "
                f"(usá set-parent --parent).\n",
                err=True,
            )
            return 1
        conn.execute(
            text(
                "UPDATE mod_identidades SET metadata = jsonb_set(metadata, '{parent_source}', "
                "to_jsonb(CAST('agent' AS TEXT))), updated_at = NOW() "
                "WHERE id = :id AND user_id = :u"
            ),
            {"id": args.id, "u": args.user},
        )
    result = {
        "id": args.id,
        "display_name": row[0],
        "parent_id": int(row[1]),
        "parent_name": row[2],
        "parent_source": "agent",
    }
    if args.as_json:
        _emit_json(result)
    else:
        _say(f"\nConfirmada: #{args.id} {row[0]!r} pertenece a #{row[1]} {row[2]!r} (agent).\n")
    return 0


def _cmd_set_kind(args: argparse.Namespace) -> int:
    """Reclasifica el `kind` de una identidad. Útil para alinear antes de `unify` (que exige mismo
    kind) o para corregir una mala clasificación. El grafo se re-deriva en el próximo build (el
    vértice pasa a proyectar con el slug nuevo; las aristas al slug viejo caen por orphan-prune)."""
    with connection() as conn:
        row = conn.execute(
            text(
                "UPDATE mod_identidades SET kind = :k, updated_at = NOW() "
                "WHERE id = :id AND user_id = :u RETURNING display_name, kind"
            ),
            {"k": args.kind, "id": args.id, "u": args.user},
        ).first()
    if row is None:
        _say(f"\nNo existe la identidad id={args.id} para el user {args.user}.\n", err=True)
        return 1
    result = {"id": args.id, "display_name": row[0], "kind": row[1]}
    if args.as_json:
        _emit_json(result)
    else:
        _say(f"\nReclasificada: #{args.id} {row[0]!r} ahora es {row[1]}.\n")
    return 0


def _cmd_add_id(args: argparse.Namespace) -> int:
    """Agrega un identificador (email/phone/handle/domain/url) a una identidad. `platform` default =
    el kind. Normaliza con `norm_identifier`; idempotente (ON CONFLICT). Espejo del endpoint
    `/{id}/identifiers`."""
    platform = args.platform or args.kind
    with connection() as conn:
        owns = conn.execute(
            text("SELECT display_name FROM mod_identidades WHERE id = :id AND user_id = :u"),
            {"id": args.id, "u": args.user},
        ).scalar()
        if owns is None:
            _say(f"\nNo existe la identidad id={args.id} para el user {args.user}.\n", err=True)
            return 1
        conn.execute(
            text(
                """
                INSERT INTO mod_identidades_identifiers
                  (user_id, identity_id, platform, kind, value, value_norm, source)
                VALUES (:u, :id, :pl, :k, :v, :vn, 'manual')
                ON CONFLICT (identity_id, platform, kind, value_norm) DO NOTHING
                """
            ),
            {
                "u": args.user,
                "id": args.id,
                "pl": platform,
                "k": args.kind,
                "v": args.value,
                "vn": norm_identifier(args.kind, args.value),
            },
        )
    result = {
        "id": args.id,
        "display_name": owns,
        "platform": platform,
        "kind": args.kind,
        "value": args.value,
    }
    if args.as_json:
        _emit_json(result)
    else:
        _say(
            f"\nIdentificador agregado a #{args.id} {owns!r}: "
            f"{platform}/{args.kind}={args.value}.\n"
        )
    return 0


def _cmd_affiliate(args: argparse.Namespace) -> int:
    """Teje una persona con una organización (afiliación). Valida los kinds (persona↔organizacion),
    igual que `/{id}/orgs`. Idempotente; materializa una arista `afiliado` en el próximo build."""
    with connection() as conn:
        rows = {
            int(r["id"]): str(r["kind"])
            for r in conn.execute(
                text("SELECT id, kind FROM mod_identidades WHERE id = ANY(:ids) AND user_id = :u"),
                {"ids": [args.person, args.org], "u": args.user},
            ).mappings()
        }
        if args.person not in rows or args.org not in rows:
            _say("\nLa persona y/o la organización no existen para este user.\n", err=True)
            return 1
        if rows[args.person] != "persona":
            _say(f"\n#{args.person} no es una persona (es {rows[args.person]}).\n", err=True)
            return 1
        if rows[args.org] != "organizacion":
            _say(f"\n#{args.org} no es una organización (es {rows[args.org]}).\n", err=True)
            return 1
        conn.execute(
            text(
                """
                INSERT INTO mod_identidades_person_orgs AS po
                  (user_id, person_id, org_id, role, source)
                VALUES (:u, :p, :o, :r, 'manual')
                ON CONFLICT (person_id, org_id)
                  DO UPDATE SET role = COALESCE(EXCLUDED.role, po.role)
                """
            ),
            {"u": args.user, "p": args.person, "o": args.org, "r": args.role},
        )
    result = {"person_id": args.person, "org_id": args.org, "role": args.role}
    if args.as_json:
        _emit_json(result)
    else:
        role = f" como {args.role!r}" if args.role else ""
        _say(f"\nAfiliada: #{args.person} ↔ #{args.org}{role}.\n")
    return 0


def _cmd_unify(args: argparse.Namespace) -> int:
    """Funde dos identidades del MISMO kind sin pasar por la cola de candidatos (cuando el agente/
    dueño ya SABE que son la misma y el difuso no las encoló). `--into` sobrevive, `--from` se
    absorbe. Reusa `merge_identities` (re-apunta aristas/menciones/finanzas/jerarquía/cúmulos)."""
    log = get_logger("memex.modules.identidades.cli")
    if args.into == args.from_id:
        _say("\n--into y --from no pueden ser la misma identidad.\n", err=True)
        return 1
    with connection() as conn:
        from_name = conn.execute(
            text("SELECT display_name FROM mod_identidades WHERE id = :id AND user_id = :u"),
            {"id": args.from_id, "u": args.user},
        ).scalar()
        if not merge_identities(conn, args.user, args.into, args.from_id):
            _say(
                f"\nNo se pudo fundir #{args.from_id} en #{args.into} "
                f"(no existen, distinto kind, o mismo id).\n",
                err=True,
            )
            return 1
        surv_name = conn.execute(
            text("SELECT display_name FROM mod_identidades WHERE id = :id"), {"id": args.into}
        ).scalar()
    log.info("identidades.unify.agent", into=args.into, absorbed=args.from_id, why=args.why.strip())
    result = {"survivor": {"id": args.into, "display_name": surv_name}, "absorbed_id": args.from_id}
    if args.as_json:
        _emit_json(result)
    else:
        _say(
            f"\nFundidas: #{args.from_id} {from_name!r} se absorbió en "
            f"#{args.into} {surv_name!r}.\n"
        )
    return 0


def _cmd_relate(args: argparse.Namespace) -> int:
    """Crea una relación CONFIRMADA entre dos identidades (producer='humano'). Una vez confirmada,
    no reaparece como pista (la supresión de `confirmed_pairs` la poda) ni la re-evalúa el partidor
    de cúmulos. Idempotente (`propose_edge` no duplica). Ej.: TylerTemp→Unity 'mantiene_asset'."""
    log = get_logger("memex.modules.identidades.cli")
    if args.from_id == args.to_id:
        _say("\n--from y --to no pueden ser la misma identidad.\n", err=True)
        return 1
    with connection() as conn:
        a = _identity_ref(conn, args.user, args.from_id)
        b = _identity_ref(conn, args.user, args.to_id)
        if a is None or b is None:
            _say("\nLa identidad origen y/o destino no existen para este user.\n", err=True)
            return 1
        (src_ref, a_name), (dst_ref, b_name) = a, b
        edge_id = propose_edge(
            conn,
            args.user,
            src_ref,
            dst_ref,
            producer=PRODUCER_HUMANO,
            relation_type=args.rel_type,
            verdict=VERDICT_CONFIRMED,
            provenance=PROVENANCE_EXTRACTED,
            relation=args.why.strip(),
            evidence=args.why.strip(),
        )
    log.info(
        "identidades.relate.agent",
        edge_id=edge_id,
        src=args.from_id,
        dst=args.to_id,
        rel_type=args.rel_type,
    )
    result = {
        "edge_id": edge_id,
        "from": {"id": args.from_id, "display_name": a_name},
        "to": {"id": args.to_id, "display_name": b_name},
        "relation_type": args.rel_type,
        "verdict": "confirmed",
    }
    if args.as_json:
        _emit_json(result)
    else:
        _say(
            f"\nRelación confirmada: #{args.from_id} {a_name!r} → #{args.to_id} {b_name!r} "
            f"({args.rel_type}).\n"
        )
    return 0


def _cmd_confirm_relation(args: argparse.Namespace) -> int:
    """Promueve una arista AMBIGUA (p.ej. co-ocurrencia) a `confirmed`. Reusa `resolve_edge`
    (monótono). La arista deja de ser candidata a re-análisis."""
    with connection() as conn:
        edge = conn.execute(
            text(
                "SELECT verdict, src_slug, src_id, dst_slug, dst_id, relation_type "
                "FROM relation_edges WHERE id = :e AND user_id = :u"
            ),
            {"e": args.edge, "u": args.user},
        ).first()
        if edge is None:
            _say(f"\nNo existe la arista id={args.edge} para el user {args.user}.\n", err=True)
            return 1
        if str(edge[0]) != VERDICT_AMBIGUOUS:
            _say(
                f"\nLa arista {args.edge} ya está {edge[0]} (solo se confirman ambiguas).\n",
                err=True,
            )
            return 1
        changed = resolve_edge(
            conn,
            args.edge,
            verdict=VERDICT_CONFIRMED,
            provenance=PROVENANCE_EXTRACTED,
            relation=(args.why.strip() or None),
            evidence=(args.why.strip() or None),
        )
    result = {"edge_id": args.edge, "verdict": "confirmed", "changed": changed}
    if args.as_json:
        _emit_json(result)
    else:
        _say(f"\nPista {args.edge} confirmada (relación {edge[5] or 'sin tipo'}).\n")
    return 0


def _cmd_unrelate(args: argparse.Namespace) -> int:
    """Descarta una arista: la marca `rejected` (terminal). El humano puede rechazar una ambigua O
    una confirmed (override; UPDATE directo acotado al user); fija `provenance='extracted'` (es una
    aserción del dueño, no del LLM) y `dirty=TRUE`. Idempotente: una ya rechazada no cambia."""
    with connection() as conn:
        n = conn.execute(
            text(
                """
                UPDATE relation_edges
                SET verdict = :rej, provenance = 'extracted', decided_at = NOW(), dirty = TRUE,
                    evidence = COALESCE(NULLIF(:why, ''), evidence),
                    relation = COALESCE(NULLIF(:why, ''), relation)
                WHERE id = :e AND user_id = :u AND verdict <> :rej
                """
            ),
            {"rej": VERDICT_REJECTED, "why": args.why.strip(), "e": args.edge, "u": args.user},
        ).rowcount
        exists = conn.execute(
            text("SELECT 1 FROM relation_edges WHERE id = :e AND user_id = :u"),
            {"e": args.edge, "u": args.user},
        ).first()
    if exists is None:
        _say(f"\nNo existe la arista id={args.edge} para el user {args.user}.\n", err=True)
        return 1
    result = {"edge_id": args.edge, "verdict": "rejected", "changed": n > 0}
    if args.as_json:
        _emit_json(result)
    else:
        msg = "rechazada" if n > 0 else "ya estaba rechazada"
        _say(f"\nArista {args.edge} {msg}.\n")
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    """Decide un candidato de la zona gris SIN LLM (la decide el agente/dueño con su contexto).
    --distinct → `rejected` (decided_by='agent'); --same → `merge_identities` (la superviviente es
    la de id menor; el candidato cae por FK CASCADE, igual que en la FASE 2 LLM)."""
    log = get_logger("memex.modules.identidades.cli")
    why = (args.why or "").strip()
    with connection() as conn:
        cand = (
            conn.execute(
                text(
                    """
                    SELECT c.id, c.status, c.identity_a_id, c.identity_b_id,
                           a.display_name AS a_name, b.display_name AS b_name
                    FROM mod_identidades_merge_candidates c
                    JOIN mod_identidades a ON a.id = c.identity_a_id
                    JOIN mod_identidades b ON b.id = c.identity_b_id
                    WHERE c.id = :cid AND c.user_id = :uid
                    """
                ),
                {"cid": args.candidate, "uid": args.user},
            )
            .mappings()
            .first()
        )
        if cand is None:
            _say(
                f"\nNo existe el candidato id={args.candidate} (user {args.user}). "
                f"Ver los pendientes con 'candidates'.\n",
                err=True,
            )
            return 1
        if cand["status"] != "candidate":
            _say(f"\nEl candidato {args.candidate} ya fue decidido ({cand['status']}).\n", err=True)
            return 1
        a_id, b_id = int(cand["identity_a_id"]), int(cand["identity_b_id"])
        if args.distinct:
            conn.execute(
                text(
                    """
                    UPDATE mod_identidades_merge_candidates
                    SET status = 'rejected', decided_by = 'agent', rationale = :why,
                        decided_at = NOW()
                    WHERE id = :cid AND status = 'candidate'
                    """
                ),
                {"cid": args.candidate, "why": why or None},
            )
            result: dict[str, Any] = {
                "candidate_id": args.candidate,
                "decision": "distinct",
                "a": {"id": a_id, "display_name": cand["a_name"]},
                "b": {"id": b_id, "display_name": cand["b_name"]},
            }
            msg = (
                f"\nQuedan separadas: #{a_id} {cand['a_name']!r} y #{b_id} {cand['b_name']!r} "
                f"(candidato {args.candidate} rechazado).\n"
            )
        else:
            survivor, absorbed = sorted((a_id, b_id))
            if not merge_identities(conn, args.user, survivor, absorbed):
                _say(f"\nNo se pudo fusionar el par del candidato {args.candidate}.\n", err=True)
                return 1
            surv_name = conn.execute(
                text("SELECT display_name FROM mod_identidades WHERE id = :id"), {"id": survivor}
            ).scalar()
            result = {
                "candidate_id": args.candidate,
                "decision": "same",
                "survivor": {"id": survivor, "display_name": surv_name},
                "absorbed_id": absorbed,
            }
            msg = (
                f"\nFusionadas: #{absorbed} se absorbió en #{survivor} {surv_name!r} "
                f"(alias e identificadores conservados).\n"
            )
    log.info(
        "identidades.resolve.agent",
        candidate_id=args.candidate,
        decision="distinct" if args.distinct else "same",
        why=why,
    )
    if args.as_json:
        _emit_json(result)
    else:
        _say(msg)
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    stats = asyncio.run(run_sync(args.user, args.account, full=args.full))
    _say(
        f"\nidentidades sync: pulled={stats.pulled} created={stats.created} "
        f"modified={stats.modified} deleted={stats.deleted} unchanged={stats.unchanged} "
        f"errores={stats.errors}\n"
    )
    return 1 if stats.errors else 0


def _cmd_add_account(args: argparse.Namespace) -> int:
    if args.provider not in known_providers():
        _say(
            f"\nProveedor desconocido {args.provider!r}. Conocidos: {known_providers()}\n", err=True
        )
        return 1
    with connection() as conn:
        owner = conn.execute(
            text("SELECT user_id FROM accounts WHERE id = :aid"), {"aid": args.account_id}
        ).scalar()
        if owner != args.user:
            _say(
                f"\nLa cuenta del dashboard id={args.account_id} no existe o no es del user "
                f"{args.user}.\n",
                err=True,
            )
            return 1
        account_id = conn.execute(
            text(
                """
                INSERT INTO mod_identidades_provider_accounts
                  (user_id, provider, account_label, account_id)
                VALUES (:uid, :provider, :label, :acc)
                ON CONFLICT (user_id, provider, account_label)
                  DO UPDATE SET account_id = EXCLUDED.account_id
                RETURNING id
                """
            ),
            {
                "uid": args.user,
                "provider": args.provider,
                "label": args.label,
                "acc": args.account_id,
            },
        ).scalar_one()
    _say(
        f"\nCuenta lista: id={account_id} ({args.provider}/{args.label}), "
        f"vault accounts.id={args.account_id}."
    )
    _say(f"Sincronizar con: memex-identidades sync --account {account_id}\n")
    return 0


def _cmd_accounts(args: argparse.Namespace) -> int:
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT id, provider, account_label, account_id, enabled, last_sync_at,
                           sync_token IS NOT NULL AS has_cursor
                    FROM mod_identidades_provider_accounts
                    WHERE user_id = :uid
                    ORDER BY id
                    """
                ),
                {"uid": args.user},
            )
            .mappings()
            .all()
        )
    if not rows:
        _say(f"\nSin cuentas de contactos para el user {args.user}.\n")
        return 0
    _say(f"\nCuentas de contactos (user {args.user}):")
    for r in rows:
        _say(
            f"  [{r['id']}] {r['provider']}/{r['account_label']} vault_acc={r['account_id']} "
            f"enabled={r['enabled']} cursor={'si' if r['has_cursor'] else 'no'} "
            f"last_sync={r['last_sync_at']}"
        )
    _say("")
    return 0


def _cmd_interest_add(args: argparse.Namespace) -> int:
    """Upsert por nombre normalizado (no hay UNIQUE de negocio en mod_identidades): busca la org del
    user por `name_norm`; actualiza o inserta; los dominios van a identificadores."""
    with connection() as conn:
        # El lookup admite productos (una entidad ya reclasificada actualiza su interés en vez de
        # duplicarse como org); el INSERT de abajo sigue creando organizaciones.
        row = conn.execute(
            text(
                "SELECT id FROM mod_identidades "
                "WHERE user_id = :u AND kind IN ('organizacion','producto') "
                "AND name_norm = memex_norm(:n)"
            ),
            {"u": args.user, "n": args.name},
        ).first()
        if row is not None:
            org_id = int(row[0])
            conn.execute(
                text(
                    "UPDATE mod_identidades SET aliases = :aliases, interest = TRUE, "
                    "notes = :notes, updated_at = NOW() WHERE id = :id"
                ),
                {"aliases": list(args.alias), "notes": args.description, "id": org_id},
            )
        else:
            org_id = int(
                conn.execute(
                    text(
                        """
                        INSERT INTO mod_identidades
                          (user_id, kind, display_name, aliases, interest, source, notes)
                        VALUES (:u, 'organizacion', :n, :aliases, TRUE, 'manual', :notes)
                        RETURNING id
                        """
                    ),
                    {
                        "u": args.user,
                        "n": args.name,
                        "aliases": list(args.alias),
                        "notes": args.description,
                    },
                ).scalar_one()
            )
        for d in args.domain:
            if not d.strip():
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO mod_identidades_identifiers
                      (user_id, identity_id, platform, kind, value, value_norm, source)
                    VALUES (:u, :id, 'domain', 'domain', :v, :vn, 'manual')
                    ON CONFLICT (identity_id, platform, kind, value_norm) DO NOTHING
                    """
                ),
                {"u": args.user, "id": org_id, "v": d, "vn": norm_identifier("domain", d)},
            )
    _say(f"\nEn la lista de interés: id={org_id} {args.name!r}.\n")
    return 0


def _cmd_interest_list(args: argparse.Namespace) -> int:
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT mi.id, mi.display_name, mi.aliases,
                           (SELECT array_agg(value) FROM mod_identidades_identifiers
                            WHERE identity_id = mi.id AND kind = 'domain') AS domains
                    FROM mod_identidades mi
                    WHERE mi.user_id = :uid AND mi.kind IN ('organizacion','producto')
                      AND mi.interest
                    ORDER BY mi.display_name
                    """
                ),
                {"uid": args.user},
            )
            .mappings()
            .all()
        )
    if not rows:
        _say(f"\nLista de interés vacía para el user {args.user}.\n")
        return 0
    _say(f"\nLista de interés (user {args.user}):")
    for r in rows:
        aliases = ", ".join(r["aliases"]) if r["aliases"] else "-"
        domains = ", ".join(r["domains"]) if r["domains"] else "-"
        _say(f"  [{r['id']}] {r['display_name']} aliases=[{aliases}] domains=[{domains}]")
    _say("")
    return 0


def _cmd_interest_remove(args: argparse.Namespace) -> int:
    with connection() as conn:
        row = conn.execute(
            text(
                "DELETE FROM mod_identidades WHERE id = :id AND user_id = :uid "
                "RETURNING display_name"
            ),
            {"id": args.id, "uid": args.user},
        ).first()
    if row is None:
        _say(f"\nNo existe la identidad id={args.id} para el user {args.user}.\n", err=True)
        return 1
    _say(f"\nQuitada de la lista: {row[0]!r} (id={args.id}).\n")
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    stats = asyncio.run(
        run_merge_phase2(args.user, limit=args.limit, client=client_from_flags(args))
    )
    _say(
        f"\nidentidades merge: pares={stats.pairs} fusionados={stats.merged} "
        f"rechazados={stats.rejected} errores={stats.errors}\n"
    )
    return 1 if stats.errors else 0


def _cmd_candidates(args: argparse.Namespace) -> int:
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT c.id, c.score, c.identity_a_id AS a_id, c.identity_b_id AS b_id,
                           a.display_name AS a_name, b.display_name AS b_name
                    FROM mod_identidades_merge_candidates c
                    JOIN mod_identidades a ON a.id = c.identity_a_id
                    JOIN mod_identidades b ON b.id = c.identity_b_id
                    WHERE c.user_id = :uid AND c.status = 'candidate'
                    ORDER BY c.score DESC NULLS LAST, c.id
                    """
                ),
                {"uid": args.user},
            )
            .mappings()
            .all()
        )
    if getattr(args, "as_json", False):
        _emit_json({"count": len(rows), "items": [dict(r) for r in rows]})
        return 0
    if not rows:
        _say(f"\nSin candidatos de merge pendientes para el user {args.user}.\n")
        return 0
    _say(f"\nCandidatos de merge (user {args.user}):")
    for r in rows:
        score = f"{float(r['score']):.2f}" if r["score"] is not None else "?"
        _say(
            f"  [{r['id']}] #{r['a_id']} {r['a_name']!r} ~ #{r['b_id']} {r['b_name']!r} "
            f"(score={score})"
        )
    _say("")
    return 0


def _cmd_backfill_productos(args: argparse.Namespace) -> int:
    """Dry-run por default: imprime la lista de orgs que el voto reclasificaría y NO escribe.
    `--apply` aplica todo (kind + menciones + aristas + membresías + candidatos) en una tx."""
    with connection() as conn:
        cands = find_product_candidates(conn, args.user)
        if not cands:
            _say(f"\nSin candidatos org→producto para el user {args.user}.\n")
            return 0
        _say(f"\nCandidatos a reclasificar org→producto (user {args.user}):")
        for c in cands:
            _say(f"  [{c.id}] {c.display_name} votos={c.votos_producto}/{c.votos_total}")
        if not args.apply:
            _say(f"\nDRY-RUN: {len(cands)} candidatos; no se escribió nada. Aplicá con --apply.\n")
            return 0
        stats = apply_reclassification(conn, args.user, [c.id for c in cands])
    _say(
        f"\nReclasificadas {stats.reclassified} identidades a producto; "
        f"menciones={stats.mentions} aristas={stats.edges} "
        f"membresías={stats.cluster_members} "
        f"candidatos de merge rechazados={stats.merge_candidates_rejected}.\n"
    )
    return 0


def _cmd_backfill_senders(args: argparse.Namespace) -> int:
    """Backfill determinista (sin LLM) del remitente de mensajes ya procesados pre-Fase-2: crea/
    resuelve la identidad del remitente por medio y persiste su avistamiento. Idempotente. Después
    correr `memex-graph confirm --no-llm` para que los remitentes co-ocurran."""
    with connection() as conn:
        out = backfill_senders(conn, args.user)
    if not out:
        _say(f"\nSin mensajes procesados para backfillar (user {args.user}).\n")
        return 0
    breakdown = " ".join(f"{kind}={n}" for kind, n in sorted(out.items()))
    _say(
        f"\nbackfill-senders (user {args.user}): {breakdown}. "
        f"Correr `memex-graph confirm --no-llm` para regenerar la co-ocurrencia.\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.modules.identidades.cli")

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "help":
        _say(_HELP)
        return 0
    log.info("identidades.cli.start", cmd=args.cmd)

    try:
        if args.cmd == "add":
            return _cmd_add(args)
        if args.cmd == "list":
            return _cmd_list(args)
        if args.cmd == "search":
            return _cmd_search(args)
        if args.cmd == "show":
            return _cmd_show(args)
        if args.cmd == "relations":
            return _cmd_relations(args)
        if args.cmd == "tree":
            return _cmd_tree(args)
        if args.cmd == "set-parent":
            return _cmd_set_parent(args)
        if args.cmd == "confirm-parent":
            return _cmd_confirm_parent(args)
        if args.cmd == "set-kind":
            return _cmd_set_kind(args)
        if args.cmd == "add-id":
            return _cmd_add_id(args)
        if args.cmd == "affiliate":
            return _cmd_affiliate(args)
        if args.cmd == "unify":
            return _cmd_unify(args)
        if args.cmd == "annotate":
            return _cmd_annotate(args)
        if args.cmd == "relate":
            return _cmd_relate(args)
        if args.cmd == "confirm-relation":
            return _cmd_confirm_relation(args)
        if args.cmd == "unrelate":
            return _cmd_unrelate(args)
        if args.cmd == "resolve":
            return _cmd_resolve(args)
        if args.cmd == "sync":
            return _cmd_sync(args)
        if args.cmd == "add-account":
            return _cmd_add_account(args)
        if args.cmd == "accounts":
            return _cmd_accounts(args)
        if args.cmd == "merge":
            return _cmd_merge(args)
        if args.cmd == "candidates":
            return _cmd_candidates(args)
        if args.cmd == "backfill-productos":
            return _cmd_backfill_productos(args)
        if args.cmd == "backfill-senders":
            return _cmd_backfill_senders(args)
        if args.cmd == "interest":
            if args.interest_cmd == "add":
                return _cmd_interest_add(args)
            if args.interest_cmd == "list":
                return _cmd_interest_list(args)
            if args.interest_cmd == "remove":
                return _cmd_interest_remove(args)
        log.error("identidades.cli.unknown_command", cmd=args.cmd)
        return 1
    except ContactsProviderError as e:
        log.error("identidades.cli.provider_error", status_code=e.status_code, msg=str(e))
        print(
            "\nERROR del proveedor de contactos. ¿Conectaste Google en /cuenta (re-consent con el "
            "scope de Contacts) y corriste con `doppler run -- ...`?\n",
            file=sys.stderr,
        )
        return 1
    except Exception as e:
        log.exception("identidades.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
