"""Clean Architecture import rules (complements import-linter, which CI also runs)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, Tuple

import pytest

PKG = Path(__file__).resolve().parent.parent / "src" / "faultline"

#: layer -> layers it must never import
FORBIDDEN = {
    "domain": {"application", "infrastructure", "integrations"},
    "application": {"infrastructure", "integrations"},
    "infrastructure": {"integrations"},
    "integrations": {"infrastructure"},
}
DOMAIN_FORBIDDEN_STDLIB = {
    "os",
    "sys",
    "io",
    "socket",
    "ssl",
    "http",
    "urllib.request",
    "threading",
    "asyncio",
    "linecache",
    "logging",
    "gzip",
    "subprocess",
    "platform",
    "contextvars",
    "atexit",
}
FRAMEWORKS = {"flask", "django", "fastapi", "starlette", "asgiref"}


def imports(path: Path) -> Iterator[Tuple[str, int]]:
    """Yield absolute module names imported by ``path`` (relative imports resolved)."""
    rel = path.relative_to(PKG.parent).with_suffix("")
    package = list(rel.parts[:-1])
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            yield mod, node.lineno
            for alias in node.names:
                yield f"{mod}.{alias.name}", node.lineno


def layer_files(layer: str) -> list[Path]:
    return sorted((PKG / layer).rglob("*.py"))


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layers_only_depend_inward(layer: str) -> None:
    violations = []
    for f in layer_files(layer):
        for mod, line in imports(f):
            parts = mod.split(".")
            if parts[0] == "faultline" and len(parts) > 1 and parts[1] in FORBIDDEN[layer]:
                violations.append(f"{f.relative_to(PKG)}:{line} imports {mod}")
            if parts == ["faultline"]:
                violations.append(f"{f.relative_to(PKG)}:{line} imports the composition root")
    assert not violations, "\n".join(violations)


def test_domain_is_pure() -> None:
    violations = []
    for f in layer_files("domain"):
        for mod, line in imports(f):
            if any(mod == m or mod.startswith(m + ".") for m in DOMAIN_FORBIDDEN_STDLIB):
                violations.append(f"{f.relative_to(PKG)}:{line} imports {mod}")
    assert not violations, "\n".join(violations)


def test_frameworks_are_imported_only_by_their_integration_and_lazily() -> None:
    for f in PKG.rglob("*.py"):
        tree = ast.parse(f.read_text())
        top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        for node in top_level:
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                assert name.split(".")[0] not in FRAMEWORKS, f"{f}: top-level import of {name}"


def test_package_has_no_runtime_dependencies() -> None:
    tomllib = pytest.importorskip("tomllib")
    data = tomllib.loads((PKG.parent.parent / "pyproject.toml").read_text())
    assert data["project"]["dependencies"] == []
