"""Architectural / typing discipline checks.

These tests fail loud when someone bypasses the abstractions intentionally
declared in `memex.core.*`. They act as lint rules that the rest of the
codebase doesn't already enforce (mypy can't tell us "use Protocol instead
of concrete class" — that's an architectural choice).

Things checked here:

  * Ingestors don't import memex internals other than `memex.core.*` and
    `memex.logging` (ADR-001 isolation).
  * The runner types against `MemexSink` Protocol, not `MemexServerClient`.
  * Source-specific config errors derive from `SourceConfigError` so generic
    catches work.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "memex"
SRC_LOCAL = REPO_ROOT / "src" / "memex_local_client"


def _file_imports(path: Path) -> set[str]:
    """Return the full dotted names imported in the file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _collect_annotations(path: Path) -> list[str]:
    """Unparse every type annotation (params and returns) in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    annotations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
                if arg.annotation:
                    annotations.append(ast.unparse(arg.annotation))
            if node.returns:
                annotations.append(ast.unparse(node.returns))
        elif isinstance(node, ast.AnnAssign) and node.annotation:
            annotations.append(ast.unparse(node.annotation))
    return annotations


def _ingestor_py_files() -> list[Path]:
    root = SRC / "ingestors"
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


# ----- Isolation: ingestors only talk to memex via HTTP --------------------- #

FORBIDDEN_FOR_INGESTORS = (
    "memex.db",
    "memex.api",
    "memex.core.inbox",
    "memex.core.checkpoint",
)


@pytest.mark.parametrize("py_file", _ingestor_py_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_ingestor_does_not_import_memex_internals(py_file: Path) -> None:
    """ADR-001: ingestors are isolated from memex internals."""
    imports = _file_imports(py_file)
    offenders = [
        imp
        for imp in imports
        if any(imp == f or imp.startswith(f + ".") for f in FORBIDDEN_FOR_INGESTORS)
    ]
    assert not offenders, (
        f"{py_file.relative_to(REPO_ROOT)} imports forbidden modules: {offenders}. "
        "Ingestors must only depend on memex.core.* and memex.logging."
    )


def _local_client_py_files() -> list[Path]:
    if not SRC_LOCAL.exists():
        return []
    return sorted(p for p in SRC_LOCAL.rglob("*.py") if "__pycache__" not in p.parts)


@pytest.mark.parametrize(
    "py_file",
    _local_client_py_files(),
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_local_client_does_not_import_memex_internals(py_file: Path) -> None:
    """ADR-001: el cliente local cumple el rol Ingestor — mismas restricciones."""
    imports = _file_imports(py_file)
    offenders = [
        imp
        for imp in imports
        if any(imp == f or imp.startswith(f + ".") for f in FORBIDDEN_FOR_INGESTORS)
    ]
    assert not offenders, (
        f"{py_file.relative_to(REPO_ROOT)} imports forbidden modules: {offenders}. "
        "memex_local_client must only depend on memex.core.*, memex.ingestors.* and memex.logging."
    )


# ----- Runner uses Protocol, not concrete client ---------------------------- #


def test_runner_types_against_memexsink_not_memexclient() -> None:
    """The runner depends on the Protocol, not the concrete HTTP class."""
    runner = SRC / "ingestors" / "runner.py"
    annotations = _collect_annotations(runner)
    joined = " ".join(annotations)
    assert "MemexSink" in joined, "runner.py must annotate against MemexSink Protocol"
    assert "MemexServerClient" not in joined, (
        "runner.py must not annotate against MemexServerClient — use MemexSink Protocol "
        "so the runner is transport-agnostic"
    )


def test_runner_does_not_import_memexclient() -> None:
    """The runner only knows about the Protocol; it never sees the concrete class."""
    runner = SRC / "ingestors" / "runner.py"
    imports = _file_imports(runner)
    assert "memex.ingestors.memex_server_client" not in imports, (
        "runner.py imports memex_server_client (concrete); "
        "should only depend on memex.core.sink (Protocol)"
    )
    assert "memex.core.sink" in imports, "runner.py must import MemexSink from memex.core.sink"


# ----- Source config errors derive from generic base ------------------------ #


def test_imap_config_error_is_source_config_error() -> None:
    """ImapConfigError must inherit from SourceConfigError so generic catches work."""
    from memex.core.source import SourceConfigError
    from memex.ingestors.imap.config import ImapConfigError

    assert issubclass(ImapConfigError, SourceConfigError), (
        "ImapConfigError must subclass SourceConfigError so callers can use "
        "the generic except clause across source types"
    )


# ----- MemexServerClient satisfies MemexSink (runtime structural check) ----------- #


def test_memex_client_satisfies_memex_sink() -> None:
    """The concrete HTTP client must structurally satisfy the MemexSink Protocol."""
    from memex.core.sink import MemexSink
    from memex.ingestors.memex_server_client import MemexServerClient

    client = MemexServerClient(base_url="http://localhost", api_token=None)
    assert isinstance(client, MemexSink), (
        "MemexServerClient does not satisfy MemexSink Protocol — either a method "
        "signature drifted or the Protocol changed"
    )
