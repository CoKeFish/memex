"""CLI `memex` — la interfaz ÚNICA del AGENTE (Hermes) sobre memex.

Despacha a las CLIs de dominio existentes (`memex-bienestar`, `memex-finance`) exponiendo SOLO los
comandos que el agente usa para registrar/consultar la vida del usuario; los de MANTENIMIENTO
(`dedup`/`consolidate`) quedan fuera (para eso, la CLI de dominio directa). No reimplementa nada:
forwardea el resto de argv al `main` del dominio, así los flags y el `--json` (ÚLTIMA línea de
stdout) son idénticos. Determinista, sin LLM: el agente estructura los campos; memex guarda,
deduplica, consolida y conecta.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

from memex.modules.bienestar.cli import main as _bienestar_main
from memex.modules.finance.cli import main as _finance_main

#: Grupos del agente → `main` de la CLI de dominio a la que se forwardea.
_GROUPS: dict[str, Callable[[list[str]], int]] = {
    "bienestar": _bienestar_main,
    "finance": _finance_main,
}

#: Subcomandos de MANTENIMIENTO (no del agente): se bloquean en la superficie `memex`.
_BLOCKED: dict[str, frozenset[str]] = {"finance": frozenset({"dedup", "consolidate"})}

_HELP = """memex — interfaz del agente para registrar y consultar la vida del usuario.

Uso: memex <grupo> <comando> [opciones]

bienestar (salud y bienestar):
  memex bienestar register     comida/higiene/ejercicio/grooming/salud
  memex bienestar list         lista registros
  memex bienestar summary      total + conteos
  memex bienestar adherence    adherencia + rachas de hábitos
  memex bienestar habit        define hábitos (add/list/rm)

finance (gastos/ingresos):
  memex finance register       registra una transacción

Reglas:
  --event <id>  hechos del MISMO mensaje comparten el id (factura = gasto + comida)
  --json        la respuesta JSON es la ÚLTIMA línea de stdout

Detalle de un comando: memex <grupo> <comando> -h   (o: memex <grupo> help)"""


def _safe(s: str) -> str:
    """Sanea para el encoding de la consola (cp1252 en Windows), como las CLIs de dominio."""
    enc = sys.stdout.encoding or "utf-8"
    return s.encode(enc, errors="replace").decode(enc, errors="replace")


def _err(msg: str) -> int:
    print(_safe(msg), file=sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("help", "-h", "--help"):
        print(_safe(_HELP))
        return 0
    group, rest = args[0], args[1:]
    dispatch = _GROUPS.get(group)
    if dispatch is None:
        return _err(f"grupo desconocido: '{group}'. Probá: memex help")
    if rest and rest[0] in _BLOCKED.get(group, frozenset()):
        return _err(f"'{rest[0]}' es de mantenimiento, no del agente; usá 'memex-{group}' directo.")
    return dispatch(rest)


if __name__ == "__main__":
    sys.exit(main())
