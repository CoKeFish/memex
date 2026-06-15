"""CLI `memex-ocr` — etapa de OCR sobre las imágenes pendientes (usa un proveedor de visión).

Subcomandos:
  run           — una pasada de OCR sobre las `media_assets` pendientes de un user
                  (filtrable por --source, acotable por --limit, modelo override con --model).
  ensure-bucket — crea el bucket de MinIO si no existe (idempotente; útil para bootstrap).

Server-side + async. Necesita OCR_API_KEY + MEMEX_OCR_BASE_URL/MODEL (proveedor de visión) y
MEMEX_MINIO_* + MINIO_* (object storage), inyectadas por `doppler run`. Orden del pipeline:
`classify → ocr → summarize/extract` (correr esta etapa ANTES de memex-process).

Exit code 0 si OK; 1 si error fatal (config faltante, etc.).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from memex.logging import get_logger, setup_logging
from memex.ocr.client import OcrError, OcrQuotaError
from memex.ocr.worker import run_ocr
from memex.storage import MinioObjectStore, StorageConfig, StorageError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-ocr")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="OCR-ea las imágenes pendientes de un user.")
    run_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    run_p.add_argument("--source", type=int, default=None, help="Limitar a este source id.")
    run_p.add_argument("--limit", type=int, default=200, help="Máximo de imágenes (default 200).")
    run_p.add_argument(
        "--model", default=None, help="Modelo de visión (override del MEMEX_OCR_MODEL)."
    )

    sub.add_parser("ensure-bucket", help="Crea el bucket de MinIO si no existe (idempotente).")

    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    stats = asyncio.run(
        run_ocr(args.user, source_id=args.source, limit=args.limit, model=args.model)
    )
    print(
        f"\nocr: ok={stats.ok} (dedup={stats.deduped}, truncados={stats.truncated}) "
        f"errores={stats.errors}\n"
    )
    return 0


def _cmd_ensure_bucket() -> int:
    store = MinioObjectStore(StorageConfig.from_env())
    store.ensure_bucket()
    print(f"\nbucket '{store.bucket}' listo.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.ocr.cli")

    parser = _build_parser()
    args = parser.parse_args(argv)
    log.info("ocr.cli.start", cmd=args.cmd)

    try:
        if args.cmd == "run":
            return _cmd_run(args)
        if args.cmd == "ensure-bucket":
            return _cmd_ensure_bucket()
        log.error("ocr.cli.unknown_command", cmd=args.cmd)
        return 1
    except OcrQuotaError as e:
        log.error("ocr.cli.quota_abort", status_code=e.status_code, msg=str(e))
        print(
            "\nSALDO AGOTADO (HTTP 402): corrida abortada. Recargá saldo del proveedor.\n",
            file=sys.stderr,
        )
        return 1
    except OcrError as e:
        log.error("ocr.cli.ocr_error", status_code=e.status_code, msg=str(e))
        print(
            "\nERROR OCR. ¿Corriste con `doppler run -- ...` (OCR_API_KEY) y "
            "está seteado MEMEX_OCR_BASE_URL/MODEL?\n",
            file=sys.stderr,
        )
        return 1
    except StorageError as e:
        log.error("ocr.cli.storage_error", msg=str(e))
        print(
            "\nERROR storage (MinIO). ¿Está MEMEX_MINIO_ENDPOINT + MINIO_ACCESS_KEY/SECRET_KEY?\n",
            file=sys.stderr,
        )
        return 1
    except Exception as e:
        log.exception("ocr.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
