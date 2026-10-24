"""Detect project types from manifest files in an untrusted checkout (read-only, no execution)."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

MAX_DEPTH = 4
MAX_ENTRIES = 5000
MAX_PROJECTS = 100
MAX_MANIFEST_BYTES = 256 * 1024

IGNORED_DIRS = frozenset(
    {"node_modules", ".venv", "venv", "vendor", "target", "dist", "build", ".git"}
)
_NODE_LOCKS = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("package-lock.json", "npm"),
)
_PY_LOCKS = (("uv.lock", "uv"), ("poetry.lock", "poetry"), ("Pipfile.lock", "pipenv"))
_NODE_TESTERS = ("vitest", "jest", "mocha")


class Language(StrEnum):
    NODE = "node"
    PYTHON = "python"
    GO = "go"
    RUST = "rust"


class Kind(StrEnum):
    SINGLE = "single"
    POLYGLOT = "polyglot"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Project:
    root: str  # relative to workspace, "." for the workspace itself
    language: Language
    package_manager: str | None
    test_framework: str | None
    typescript: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Detection:
    kind: Kind
    projects: tuple[Project, ...]
    truncated: bool = False


def _read(path: Path, warnings: list[str]) -> str | None:
    """Read a regular (non-symlink) file, capped; record a warning on failure."""
    try:
        if path.is_symlink():
            return None
        with path.open("rb") as f:
            data = f.read(MAX_MANIFEST_BYTES + 1)
    except OSError as e:
        warnings.append(f"{path.name}: unreadable ({e.__class__.__name__})")
        return None
    if len(data) > MAX_MANIFEST_BYTES:
        warnings.append(f"{path.name}: exceeds {MAX_MANIFEST_BYTES} bytes, skipped")
        return None
    return data.decode("utf-8", errors="replace")


def _toml(path: Path, warnings: list[str]) -> dict[str, object]:
    text = _read(path, warnings)
    if text is None:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        warnings.append(f"{path.name}: malformed TOML")
        return {}


def _lock(names: set[str], locks: tuple[tuple[str, str], ...], default: str) -> str:
    return next((mgr for f, mgr in locks if f in names), default)


def _node(d: Path, names: set[str]) -> Project:
    w: list[str] = []
    ts = "tsconfig.json" in names
    tester: str | None = None
    text = _read(d / "package.json", w)
    if text is not None:
        try:
            pkg = json.loads(text)
            if not isinstance(pkg, dict):
                raise ValueError
        except ValueError:
            w.append("package.json: malformed JSON")
        else:
            deps: set[str] = set()
            for k in ("dependencies", "devDependencies"):
                v = pkg.get(k)
                if isinstance(v, dict):
                    deps |= set(v)
            scripts = pkg.get("scripts")
            test = scripts.get("test") if isinstance(scripts, dict) else None
            blob = test if isinstance(test, str) else ""
            ts = ts or "typescript" in deps
            tester = next((t for t in _NODE_TESTERS if t in deps or t in blob), None)
    return Project("", Language.NODE, _lock(names, _NODE_LOCKS, "npm"), tester, ts, tuple(w))


def _python(d: Path, names: set[str]) -> Project:
    w: list[str] = []
    blobs: list[str] = []
    if "pyproject.toml" in names:
        data = _toml(d / "pyproject.toml", w)
        blobs.append(json.dumps(data, default=str))
    for n in sorted(names):
        if n in ("setup.cfg", "setup.py", "pytest.ini", "tox.ini") or (
            n.startswith("requirements") and n.endswith(".txt")
        ):
            blobs.append(_read(d / n, w) or "")
    text = "\n".join(blobs).lower()
    tester = "pytest" if "pytest" in text or "pytest.ini" in names else "unittest"
    return Project("", Language.PYTHON, _lock(names, _PY_LOCKS, "pip"), tester, warnings=tuple(w))


def _go(d: Path, names: set[str]) -> Project:
    w: list[str] = []
    _read(d / "go.mod", w)
    return Project("", Language.GO, "go", "go test", warnings=tuple(w))


def _rust(d: Path, names: set[str]) -> Project:
    w: list[str] = []
    _toml(d / "Cargo.toml", w)  # workspace members are found by the walk itself
    return Project("", Language.RUST, "cargo", "cargo test", warnings=tuple(w))


def _dir_projects(d: Path, names: set[str]) -> list[Project]:
    out: list[Project] = []
    if "package.json" in names:
        out.append(_node(d, names))
    if names & {"pyproject.toml", "setup.cfg", "setup.py"} or any(
        n.startswith("requirements") and n.endswith(".txt") for n in names
    ):
        out.append(_python(d, names))
    if "go.mod" in names:
        out.append(_go(d, names))
    if "Cargo.toml" in names:
        out.append(_rust(d, names))
    return out


def detect(workspace: Path) -> Detection:
    """Scan `workspace` for manifests. Never executes repo content or follows symlinks."""
    root = workspace.resolve()
    projects: list[Project] = []
    seen = 0
    truncated = False
    for cur, dirs, files in os.walk(root, followlinks=False):
        rel = Path(cur).relative_to(root)
        depth = len(rel.parts)
        dirs[:] = sorted(
            n for n in dirs if n not in IGNORED_DIRS and not (Path(cur) / n).is_symlink()
        )
        if depth >= MAX_DEPTH:
            dirs[:] = []
        files = [f for f in files if not (Path(cur) / f).is_symlink()]
        seen += len(files) + len(dirs)
        if seen > MAX_ENTRIES:
            truncated = True
            break
        for p in _dir_projects(Path(cur), set(files)):
            if len(projects) >= MAX_PROJECTS:
                truncated = True
                break
            projects.append(Project(**{**p.__dict__, "root": rel.as_posix()}))
    langs = {p.language for p in projects}
    kind = Kind.UNKNOWN if not langs else Kind.SINGLE if len(langs) == 1 else Kind.POLYGLOT
    return Detection(kind, tuple(projects), truncated)
