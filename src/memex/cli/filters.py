"""CLI `memex-filters` — administra reglas de filter_rules contra la DB de memex.

Subcomandos:

  add     — crea una regla nueva.
  list    — lista reglas (filtros opcionales por user/source_type/enabled).
  enable  — marca una regla como activa.
  disable — marca una regla como inactiva (no la borra — preserva audit).
  remove  — borra una regla.
  test    — dry-run: dado un payload JSON, muestra qué regla matchearía.

Conecta directo a Postgres usando `memex.db.connection` — no pasa por HTTP.
Pensado para usarse desde el server (mismo host que la DB).

Ejemplos:

    memex-filters add --user-id 1 --source-type imap \\
        --scope '{"from": {"equals": "spam@x.com"}}' \\
        --action ignore --priority 200

    memex-filters list --user-id 1 --source-type imap --enabled-only

    memex-filters test --user-id 1 --source-type imap \\
        --payload '{"from": "spam@x.com", "subject": "hi"}'
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sqlalchemy import text

from memex.core import filters
from memex.db import connection
from memex.logging import setup_logging


def _parse_scope(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"--scope is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise SystemExit("--scope must be a JSON object")
    return data


def _parse_payload(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"--payload is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise SystemExit("--payload must be a JSON object")
    return data


def cmd_add(args: argparse.Namespace) -> int:
    scope = _parse_scope(args.scope)
    if args.action not in ("keep", "ignore", "archive"):
        raise SystemExit(f"--action must be keep|ignore|archive, got {args.action!r}")
    with connection() as conn:
        new_id = conn.execute(
            text(
                """
                INSERT INTO filter_rules
                    (user_id, source_type, source_id, scope, action, priority, enabled)
                VALUES
                    (:uid, :stype, :sid, CAST(:scope AS JSONB), :action, :prio, TRUE)
                RETURNING id
                """
            ),
            {
                "uid": args.user_id,
                "stype": args.source_type,
                "sid": args.source_id,
                "scope": json.dumps(scope),
                "action": args.action,
                "prio": args.priority,
            },
        ).scalar()
    print(f"created rule id={new_id}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    sql = """
        SELECT id, user_id, source_type, source_id, scope, action, priority, enabled
        FROM filter_rules
        WHERE TRUE
    """
    params: dict[str, Any] = {}
    if args.user_id is not None:
        sql += " AND user_id = :uid"
        params["uid"] = args.user_id
    if args.source_type is not None:
        sql += " AND source_type = :stype"
        params["stype"] = args.source_type
    if args.enabled_only:
        sql += " AND enabled"
    sql += " ORDER BY user_id, priority DESC, id ASC"
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    if not rows:
        print("(no rules)")
        return 0
    for r in rows:
        flag = "✓" if r["enabled"] else "✗"
        st = r["source_type"] or "*"
        sid = r["source_id"] if r["source_id"] is not None else "*"
        print(
            f"[{flag}] id={r['id']} user={r['user_id']} type={st} source={sid} "
            f"action={r['action']} prio={r['priority']}"
        )
        print(f"     scope={json.dumps(r['scope'])}")
    return 0


def cmd_set_enabled(args: argparse.Namespace, enabled: bool) -> int:
    with connection() as conn:
        n = conn.execute(
            text("UPDATE filter_rules SET enabled = :v WHERE id = :id"),
            {"v": enabled, "id": args.rule_id},
        ).rowcount
    if n == 0:
        print(f"no rule with id={args.rule_id}", file=sys.stderr)
        return 1
    print(f"rule id={args.rule_id} {'enabled' if enabled else 'disabled'}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    with connection() as conn:
        n = conn.execute(
            text("DELETE FROM filter_rules WHERE id = :id"),
            {"id": args.rule_id},
        ).rowcount
    if n == 0:
        print(f"no rule with id={args.rule_id}", file=sys.stderr)
        return 1
    print(f"rule id={args.rule_id} removed")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    payload = _parse_payload(args.payload)
    with connection() as conn:
        rules = filters.load_active_rules(
            conn,
            user_id=args.user_id,
            source_type=args.source_type,
            source_id=args.source_id,
        )
    if not rules:
        print("(no active rules for that scope)")
        return 0
    matched = filters.decide(rules, payload)
    if matched is None:
        print(f"no rule matched — record would be kept (default). evaluated {len(rules)} rule(s)")
        return 0
    print(f"matched rule id={matched.id} action={matched.action} priority={matched.priority}")
    print(f"  scope={json.dumps(matched.scope)}")
    if matched.action == "ignore":
        print("  -> record WOULD BE DROPPED (filter pre-ingest)")
    else:
        print("  -> record would be kept")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memex-filters")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="create a filter rule")
    p_add.add_argument("--user-id", type=int, required=True)
    p_add.add_argument(
        "--source-type",
        default=None,
        help="NULL means: applies to any source_type for this user",
    )
    p_add.add_argument(
        "--source-id",
        type=int,
        default=None,
        help="NULL means: applies to any source of the given source_type",
    )
    p_add.add_argument(
        "--scope",
        required=True,
        help='JSON object, e.g. \'{"from":{"equals":"x@y"}}\'',
    )
    p_add.add_argument("--action", required=True, choices=["keep", "ignore", "archive"])
    p_add.add_argument("--priority", type=int, default=100)
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="list filter rules")
    p_list.add_argument("--user-id", type=int, default=None)
    p_list.add_argument("--source-type", default=None)
    p_list.add_argument("--enabled-only", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_enable = sub.add_parser("enable", help="enable a rule by id")
    p_enable.add_argument("rule_id", type=int)
    p_enable.set_defaults(func=lambda a: cmd_set_enabled(a, True))

    p_disable = sub.add_parser("disable", help="disable a rule by id")
    p_disable.add_argument("rule_id", type=int)
    p_disable.set_defaults(func=lambda a: cmd_set_enabled(a, False))

    p_remove = sub.add_parser("remove", help="delete a rule by id")
    p_remove.add_argument("rule_id", type=int)
    p_remove.set_defaults(func=cmd_remove)

    p_test = sub.add_parser("test", help="dry-run: show which rule would match a payload")
    p_test.add_argument("--user-id", type=int, required=True)
    p_test.add_argument("--source-type", default=None)
    p_test.add_argument("--source-id", type=int, default=None)
    p_test.add_argument("--payload", required=True, help="JSON object representing record.payload")
    p_test.set_defaults(func=cmd_test)

    return p


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    func = args.func
    return int(func(args))


if __name__ == "__main__":
    raise SystemExit(main())
