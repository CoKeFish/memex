"""CLI `memex-documentos` — entregarle un archivo a memex desde la consola.

  memex-documentos add <archivo> [<archivo> ...] [--procesar] [--nota "..."] [--json]

`--procesar` corre las etapas de lectura (OCR → clasificar → extraer) en el acto, así el documento
queda convertido en lo que sea que contenga (las transacciones de un extracto, por ejemplo) sin un
segundo comando. Sin el flag solo queda ingerido, esperando al procesamiento normal.

Exit code 0 si OK; 1 si algún archivo falló; 2 si error de uso.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from memex.db import connection
from memex.documentos.ingest import DocumentoError, entregar_archivo
from memex.logging import get_logger, setup_logging

#: Etapas que convierten el documento en datos. `media` no hace falta: los bytes entran con el
#: registro, no hay que re-bajarlos de ningún lado.


def _safe(text_: str) -> str:
    enc = sys.stdout.encoding or "utf-8"
    return text_.encode(enc, errors="replace").decode(enc, errors="replace")


def _say(msg: str, *, err: bool = False) -> None:
    print(_safe(msg), file=sys.stderr if err else sys.stdout)


def _emit_json(obj: object) -> None:
    print(_safe(json.dumps(obj, default=str, ensure_ascii=False)))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-documentos")
    sub = parser.add_subparsers(dest="cmd", required=True)

    add_p = sub.add_parser("add", help="Entrega uno o más archivos a memex.")
    add_p.add_argument("paths", nargs="+", help="Archivos (pdf, zip, png, jpg, webp).")
    add_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    add_p.add_argument(
        "--canal",
        default="consola",
        help="Por dónde llegó (consola, telegram, …). Separa las fuentes en /carga.",
    )
    add_p.add_argument("--nota", default="", help="Nota que acompaña al documento.")
    add_p.add_argument(
        "--asunto", type=int, default=None, help="Colgarlo como evidencia de este asunto (id)."
    )
    add_p.add_argument(
        "--procesar",
        action="store_true",
        help="Lee el documento ya mismo (OCR + extracción) en vez de esperar al procesamiento.",
    )
    add_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")
    return parser


def _cmd_add(args: argparse.Namespace) -> int:
    entregas: list[dict[str, Any]] = []
    fallos: list[str] = []
    nuevos: list[int] = []

    for raw in args.paths:
        path = Path(raw).expanduser()
        try:
            with connection() as conn:
                entrega = entregar_archivo(conn, args.user, path, canal=args.canal, nota=args.nota)
                if args.asunto is not None and entrega.inbox_id is not None:
                    from memex.modules.asuntos import store as asuntos_store

                    asuntos_store.add_evidence(
                        conn,
                        args.user,
                        args.asunto,
                        entrega.inbox_id,
                        method="manual",
                        confidence=1.0,
                    )
        except DocumentoError as e:
            fallos.append(str(e))
            continue
        entregas.append(
            {
                "archivo": entrega.filename,
                "inbox_id": entrega.inbox_id,
                "duplicado": entrega.duplicado,
            }
        )
        if entrega.inbox_id is not None and not entrega.duplicado:
            nuevos.append(entrega.inbox_id)

    resultado: dict[str, Any] = {
        "entregados": len(entregas),
        "nuevos": len(nuevos),
        "duplicados": sum(1 for item in entregas if item["duplicado"]),
        "fallidos": len(fallos),
        "items": entregas,
        "errores": fallos,
    }

    if args.procesar and nuevos:
        from memex.documentos.procesar import procesar_documentos

        resultado["procesamiento"] = asyncio.run(procesar_documentos(args.user, nuevos))

    if args.as_json:
        _emit_json(resultado)
        return 1 if fallos else 0

    _say("")
    for item in entregas:
        estado = "ya estaba" if item["duplicado"] else f"inbox {item['inbox_id']}"
        _say(f"  [ok] {item['archivo']} — {estado}")
    for fallo in fallos:
        _say(f"  [x]  {fallo}", err=True)
    proc = resultado.get("procesamiento")
    if isinstance(proc, dict):
        ocr = proc.get("results", {}).get("ocr", {})
        ext = proc.get("results", {}).get("extract", {})
        _say(
            f"\nleídos: {ocr.get('ok', 0)} · con error: {ocr.get('errors', 0)} · "
            f"datos extraídos: {ext.get('items', 0)} (USD {ext.get('cost_usd', 0)})"
        )
    _say(
        f"\nentregados={len(entregas)} nuevos={len(nuevos)} "
        f"duplicados={resultado['duplicados']} fallidos={len(fallos)}\n"
    )
    return 1 if fallos else 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.documentos.cli")
    args = _build_parser().parse_args(argv)
    log.info("documentos.cli.start", cmd=args.cmd)
    try:
        if args.cmd == "add":
            return _cmd_add(args)
        return 2
    except Exception as e:
        log.exception("documentos.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        _say(f"\nERROR: {e}\n", err=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
