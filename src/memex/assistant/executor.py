"""Ejecuta comandos `memex ...` CONFIRMADOS por el usuario. Es la única vía por la que el asistente
escribe: el modelo propone, el usuario confirma, este módulo corre. Acepta solo comandos que
empiecen por `memex` (tolera los prefijos `uv run` / `python -m memex.agent_cli` que el modelo
a veces copia del prompt) y los lanza con el intérprete configurado, nunca por un shell.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from memex.logging import get_logger

_log = get_logger("memex.assistant.executor")

_MODULE = "memex.agent_cli"


class CommandRejected(ValueError):
    """El comando no es `memex ...`: no se ejecuta."""


@dataclass(frozen=True)
class ExecResult:
    command: str
    exit_code: int
    output: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def normalize(command: str) -> list[str]:
    """`memex finance register --amount 25000` → ['finance', 'register', '--amount', '25000'].
    Quita prefijos de intérprete y exige que el primer token sea `memex`."""
    try:
        tokens = shlex.split(command.strip(), posix=True)
    except ValueError as e:
        raise CommandRejected(f"comillas mal cerradas: {e}") from e
    if not tokens:
        raise CommandRejected("comando vacío")
    if tokens[:2] == ["uv", "run"]:
        tokens = tokens[2:]
    if len(tokens) >= 3 and tokens[1:3] == ["-m", _MODULE] and _is_python(tokens[0]):
        tokens = ["memex", *tokens[3:]]  # `python -m memex.agent_cli X` ≡ `memex X`
    if (
        tokens
        and tokens[0] != "memex"
        and tokens[0].replace("\\", "/").split("/")[-1]
        in (
            "memex",
            "memex.exe",
        )
    ):
        tokens[0] = "memex"  # ruta al script del venv
    if not tokens or tokens[0] != "memex":
        raise CommandRejected("solo ejecuto comandos que empiecen por `memex`")
    args = tokens[1:]
    if any(t in ("&&", "||", ";", "|", ">", "<") for t in args):
        raise CommandRejected("no acepto operadores de shell en el comando")
    return args


def for_humans(command: str) -> str:
    """El mismo comando sin `--json`: la salida humana del CLI es lo que se muestra en el chat
    (el JSON es para el modelo). Si el comando no se puede tokenizar, vuelve intacto."""
    try:
        tokens = shlex.split(command.strip(), posix=True)
    except ValueError:
        return command
    kept = [t for t in tokens if t != "--json"]
    return shlex.join(kept) if len(kept) != len(tokens) else command


def matches_prefix(command: str, prefixes: Sequence[str]) -> bool:
    """¿`command` es una de las escrituras de `prefixes`? Cada prefijo es `<dominio> <verbo>`
    (p. ej. «finance register») y se compara contra los tokens normalizados, así que
    `uv run memex finance register --amount 1` también matchea. Un comando inválido no matchea."""
    try:
        args = normalize(command)
    except CommandRejected:
        return False
    for prefix in prefixes:
        want = prefix.split()
        if want and args[: len(want)] == want:
            return True
    return False


def _is_python(token: str) -> bool:
    name = token.replace("\\", "/").split("/")[-1].lower()
    return name in ("python", "python3", "python.exe", "python3.exe", "py", "py.exe")


def human_output(raw: str, *, limit: int = 3000) -> str:
    """Quita las líneas de log JSON (structlog a stdout) y deja lo que el CLI le dijo al usuario."""
    kept: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict) and "event" in obj and "level" in obj:
                continue
        if stripped:
            kept.append(line.rstrip())
    text = "\n".join(kept).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class CommandExecutor:
    def __init__(
        self,
        memex_argv: Sequence[str],
        *,
        workdir: Path,
        timeout_s: float = 180.0,
        output_limit: int = 3000,
    ) -> None:
        self._argv = list(memex_argv)
        self._workdir = workdir
        self._timeout_s = timeout_s
        #: Tope de la salida devuelta. Para el chat (lo que ve la persona) 3000 alcanza; el
        #: cerebro por API lo sube (12k): si la salida se corta, el modelo re-consulta por rangos
        #: y el turno pasa de 4 s a 20 s (medido 2026-09-09).
        self._output_limit = output_limit

    async def run(self, command: str) -> ExecResult:
        args = normalize(command)
        proc = await asyncio.create_subprocess_exec(
            *self._argv,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._workdir),
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_s)
        except TimeoutError:
            proc.kill()
            _log.error("assistant.executor.timeout", command=command)
            return ExecResult(command, -1, f"el comando superó el timeout ({self._timeout_s:.0f}s)")
        out = out_b.decode("utf-8", errors="replace")
        err = err_b.decode("utf-8", errors="replace")
        code = proc.returncode if proc.returncode is not None else -1
        output = human_output(out, limit=self._output_limit) or human_output(
            err, limit=self._output_limit
        )
        if code != 0 and err.strip():
            tail = human_output(err, limit=800)
            output = f"{output}\n{tail}".strip() if tail and tail not in output else output
        _log.info("assistant.executor.ran", command=command, exit_code=code, chars=len(output))
        return ExecResult(command, code, output)
