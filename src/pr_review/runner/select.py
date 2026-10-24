"""Diff-aware test selection, time-capped execution, and sanitized run notices (FR-9, AC-8).

Selection (project-root-relative targets):
- pytest: a changed test file runs directly; a changed source file runs tests named for it
  (`test_x.py`, `x_test.py`) or importing it (bounded grep). Config files (conftest.py,
  pyproject/setup.cfg/pytest.ini/requirements, ...) force the full suite.
- jest: `--findRelatedTests <changed js/ts files>`; package.json / jest / tsconfig -> full.
- go: packages containing changed .go files; go.mod/go.sum/go.work -> full.
- cargo: workspace member crates containing changed .rs files; root crate / Cargo.* -> full.
An empty selection (nothing mapped) means the full suite. A selection is *partial* when some
changed source file mapped to no test.

Policy: run the selection first. If it was partial and time remains under the overall cap, also
run the full suite with the remaining time; its results replace the selection's. If that
follow-up times out or cannot start, the selection's results stand (with a warning). A selected
run that timed out, was killed, or could not start never triggers a follow-up (failing tests do
not block it). The cap is the repo config's `test_timeout_seconds` (already clamped to the
operator ceiling) or the caller's default.
"""

import re
import time
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

import docker
from docker.errors import DockerException

from pr_review.core.repo_config import RepoConfig
from pr_review.parsers.coverage import (
    CoverageResult,
    parse_coverage_json,
    parse_go_cover,
    parse_lcov,
)
from pr_review.parsers.diff import parse_diff
from pr_review.parsers.tests import (
    ParseError,
    TestRunResult,
    parse_cargo_test,
    parse_go_test,
    parse_jest,
    parse_junit,
)
from pr_review.runner.commands import Command, CommandError, build_command
from pr_review.runner.detect import IGNORED_DIRS, Language, Project
from pr_review.runner.sandbox import (
    OUT_DIR,
    WORKDIR,
    SandboxError,
    SandboxResult,
    SandboxSpec,
    run_in_sandbox,
)

MAX_FILES = 5000
MAX_READ = 64 * 1024
MIN_FOLLOWUP_S = 5.0
NOTICE_MAX = 160

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_NETWORK = (
    "network is unreachable",
    "temporary failure in name resolution",
    "name or service not known",
    "could not resolve host",
    "no route to host",
)
_DOCS = frozenset({".md", ".rst", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf"})
_PY_CONFIG = frozenset(
    {
        "conftest.py",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "pytest.ini",
        "tox.ini",
        "uv.lock",
        "poetry.lock",
        "Pipfile",
        "Pipfile.lock",
    }
)
_JS_CONFIG = frozenset(
    {
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "tsconfig.json",
        ".babelrc",
        "babel.config.js",
    }
)
_JS_EXT = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
_GO_CONFIG = frozenset({"go.mod", "go.sum", "go.work"})
_RUST_CONFIG = frozenset({"Cargo.toml", "Cargo.lock", "rust-toolchain", "rust-toolchain.toml"})


def strip_control(text: str, limit: int = NOTICE_MAX) -> str:
    """ANSI escapes, then C0/C1 control characters (newlines too) -> single spaces, capped."""
    return " ".join(_CTRL.sub(" ", _ANSI.sub("", text)).split())[:limit]


@dataclass(frozen=True)
class Selection:
    targets: tuple[str, ...]  # project-root-relative; empty with full=True
    full: bool
    reason: str
    partial: bool = False  # some changed source file mapped to no test


@dataclass(frozen=True)
class TestPlan:
    __test__ = False  # not a pytest class

    project: Project
    selection: Selection
    command: Command | None  # selected (or full) run; None when skipped
    full_command: Command | None  # follow-up full run, only when selection is partial
    timeout_s: float
    notice: str | None = None  # set when the run is skipped


@dataclass(frozen=True)
class RunRecord:
    argv: tuple[str, ...]
    full: bool
    exit_code: int | None
    duration_s: float


@dataclass(frozen=True)
class TestOutcome:
    __test__ = False

    plan: TestPlan
    runs: tuple[RunRecord, ...] = ()
    ran_full: bool = False  # the reported results come from a full-suite run
    duration_s: float = 0.0
    artifact_paths: tuple[str, ...] = ()
    notice: str | None = None
    tests: TestRunResult | ParseError | None = None
    coverage: CoverageResult | ParseError | None = None
    warnings: tuple[str, ...] = ()


# --------------------------------------------------------------------------- changed files


def changed_paths(diff_text: str) -> tuple[str, ...]:
    """Repo-relative paths touched by a unified diff (new path; old path for deletions)."""
    seen: dict[str, None] = {}
    for f in parse_diff(diff_text).files:
        p = f.new_path or f.old_path
        if p:
            seen[p] = None
    return tuple(seen)


def _within(root: str, changed: Sequence[str]) -> list[str]:
    if root in ("", "."):
        return list(changed)
    pre = root.rstrip("/") + "/"
    return [c[len(pre) :] for c in changed if c.startswith(pre)]


def _walk(base: Path) -> Iterator[str]:
    """Bounded walk of regular .py files below base, POSIX paths relative to base."""
    n = 0
    stack = [base]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_symlink():
                continue
            if e.is_dir():
                if e.name not in IGNORED_DIRS and e.name != ".deps":
                    stack.append(e)
            elif e.suffix == ".py":
                n += 1
                if n > MAX_FILES:
                    return
                yield e.relative_to(base).as_posix()


def _is_test_name(name: str) -> bool:
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


# --------------------------------------------------------------------------- python


def _py_import_regex(rel: str) -> re.Pattern[str]:
    parts = list(PurePosixPath(rel).with_suffix("").parts)
    stem = parts[-1]
    if stem == "__init__":
        parts.pop()
        stem = parts[-1] if parts else ""
    if not stem:
        return re.compile(r"(?!)")
    mods = {".".join(parts[i:]) for i in range(max(len(parts) - 2, 0), len(parts))}
    mods.add(".".join(parts))
    alt = "|".join(re.escape(m) for m in sorted(mods, key=len, reverse=True))
    parent = re.escape(".".join(parts[:-1]))
    tail = rf"|from\s+(?:{parent})\s+import\s+[^\n#]*\b{re.escape(stem)}\b" if parts[:-1] else ""
    return re.compile(
        rf"^\s*(?:from\s+(?:{alt})(?:\.\w+)*\s+import\b|import\s+(?:{alt})\b{tail})", re.M
    )


def _select_python(ws: Path, root: str, rel: list[str]) -> Selection:
    base = ws if root in ("", ".") else ws / root
    tests = [p for p in _walk(base) if _is_test_name(PurePosixPath(p).name)]
    picked: dict[str, None] = {}
    partial = False
    for c in rel:
        name = PurePosixPath(c).name
        if name in _PY_CONFIG or (name.startswith("requirements") and name.endswith(".txt")):
            return Selection((), True, f"config file changed: {c}")
        if PurePosixPath(c).suffix in _DOCS:
            continue
        if not c.endswith(".py"):
            partial = True
            continue
        if _is_test_name(name):
            if (base / c).is_file():
                picked[c] = None
            continue
        rx = _py_import_regex(c)
        stem = PurePosixPath(c).stem
        hit = False
        for t in tests:
            tn = PurePosixPath(t).name
            if tn in (f"test_{stem}.py", f"{stem}_test.py") or _grep(base / t, rx):
                picked[t] = None
                hit = True
        partial = partial or not hit
    if not picked:
        return Selection((), True, "no tests mapped to the changed files")
    return Selection(tuple(picked), False, "tests mapped from changed files", partial)


def _grep(path: Path, rx: re.Pattern[str]) -> bool:
    try:
        with path.open("rb") as f:
            return rx.search(f.read(MAX_READ).decode("utf-8", errors="replace")) is not None
    except OSError:
        return False


# --------------------------------------------------------------------------- other languages


def _select_node(rel: list[str]) -> Selection:
    code: list[str] = []
    for c in rel:
        p = PurePosixPath(c)
        if p.name in _JS_CONFIG or p.name.startswith(("jest.config", "babel.config")):
            return Selection((), True, f"config file changed: {c}")
        if p.suffix in _JS_EXT:
            code.append(c)
    if not code:
        return Selection((), True, "no tests mapped to the changed files")
    return Selection(tuple(code), False, "jest --findRelatedTests")


def _select_go(rel: list[str]) -> Selection:
    pkgs: dict[str, None] = {}
    for c in rel:
        p = PurePosixPath(c)
        if p.name in _GO_CONFIG:
            return Selection((), True, f"config file changed: {c}")
        if p.suffix == ".go":
            d = p.parent.as_posix()
            pkgs["./" if d == "." else f"./{d}"] = None
    if not pkgs:
        return Selection((), True, "no tests mapped to the changed files")
    return Selection(tuple(pkgs), False, "packages containing changed files")


def _crate_name(manifest: Path) -> str | None:
    try:
        with manifest.open("rb") as f:
            data = tomllib.loads(f.read(MAX_READ).decode("utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    name = data.get("package", {}).get("name")
    return name if isinstance(name, str) else None


def _select_rust(ws: Path, root: str, rel: list[str]) -> Selection:
    base = ws if root in ("", ".") else ws / root
    crates: dict[str, None] = {}
    for c in rel:
        p = PurePosixPath(c)
        if p.name in _RUST_CONFIG:
            if p.name == "Cargo.toml" and p.parent.as_posix() != ".":
                name = _crate_name(base / c)
                if name:
                    crates[name] = None
                    continue
            return Selection((), True, f"config file changed: {c}")
        if p.suffix != ".rs":
            continue
        for parent in p.parents:
            if parent.as_posix() == ".":
                return Selection((), True, "root crate changed (crate-level = whole crate)")
            if (base / parent / "Cargo.toml").is_file():
                name = _crate_name(base / parent / "Cargo.toml")
                if name is None:
                    return Selection((), True, "crate name unreadable")
                crates[name] = None
                break
    if not crates:
        return Selection((), True, "no tests mapped to the changed files")
    return Selection(tuple(crates), False, "member crates containing changed files")


def select_tests(ws: Path, project: Project, changed: Sequence[str]) -> Selection:
    rel = _within(project.root, changed)
    if not rel:
        return Selection((), True, "no changed files in this project")
    match project.language:
        case Language.PYTHON:
            return _select_python(ws, project.root, rel)
        case Language.NODE:
            return _select_node(rel)
        case Language.GO:
            return _select_go(rel)
        case Language.RUST:
            return _select_rust(ws, project.root, rel)


# --------------------------------------------------------------------------- notices


def notice_for(
    result: SandboxResult | None,
    *,
    timeout_s: float = 0.0,
    disabled: bool = False,
    detected: bool = True,
    start_error: str | None = None,
) -> str | None:
    """Short sanitized notice for a PR summary; None when the run needs none. Raw tool output
    is never echoed except a control-stripped, length-capped start-failure reason."""
    if disabled:
        return "tests disabled by repo config"
    if not detected:
        return "no tests detected"
    if start_error is not None:
        return f"test command failed to start: {strip_control(start_error, 80)}"
    if result is None:
        return None
    if result.oom_killed:
        return "test run killed: memory limit"
    if result.timed_out:
        return f"test run timed out after {timeout_s:.0f}s"
    if result.artifacts_rejected:
        return "artifacts rejected: too large"
    if result.exit_code in (125, 126, 127):
        return f"test command failed to start: {_reason(result)}"
    if result.exit_code != 0:
        low = f"{result.stderr}\n{result.stdout}".lower()
        if any(m in low for m in _NETWORK):
            return "network access blocked"
    return None


def _reason(result: SandboxResult) -> str:
    for line in _lines(result.stderr) or _lines(result.stdout):
        return strip_control(line, 80)
    return f"exit {result.exit_code}"


def _lines(text: str) -> list[str]:
    return [ln for ln in _ANSI.sub("", text).splitlines() if ln.strip()]


# --------------------------------------------------------------------------- plan and run


def plan_tests(
    workspace: Path,
    project: Project | None,
    changed: Sequence[str],
    cfg: RepoConfig,
    *,
    timeout_s: float = 300.0,
    coverage: bool = True,
    full_after_selected: bool = True,
) -> TestPlan:
    """Pure planning: no container is started. `workspace` should hold `.deps` if installed."""
    empty = Selection((), True, "")
    if cfg.tests_disabled:
        return TestPlan(
            project or _NONE, empty, None, None, timeout_s, notice_for(None, disabled=True)
        )
    cap = cfg.test_timeout_seconds or timeout_s
    if project is None:
        return TestPlan(_NONE, empty, None, None, cap, notice_for(None, detected=False))
    sel = select_tests(workspace, project, changed)
    override = cfg.test_commands.get(project.root) or cfg.test_commands.get(
        "." if project.root == "" else project.root
    )
    try:
        if override is not None:
            # An override is the whole command: no selection is applied to it.
            sel = Selection((), True, "repo-config command")
            cmd = build_command(
                project, (), workspace=workspace, coverage=coverage, override=override
            )
            full = None
        else:
            cmd = build_command(project, sel.targets, workspace=workspace, coverage=coverage)
            full = (
                build_command(project, (), workspace=workspace, coverage=coverage)
                if full_after_selected and sel.partial and not sel.full
                else None
            )
    except CommandError as e:
        return TestPlan(project, sel, None, None, cap, notice_for(None, start_error=str(e)))
    if cmd is None:
        return TestPlan(project, sel, None, None, cap, notice_for(None, detected=False))
    return TestPlan(project, sel, cmd, full, cap)


_NONE = Project("", Language.PYTHON, None, None)


def _execute(
    cmd: Command,
    ws: Path,
    spec: SandboxSpec,
    budget: float,
    client: docker.DockerClient | None,
) -> tuple[SandboxResult | None, str | None]:
    env = {**spec.env, **dict(cmd.env)}
    if cmd.framework == "pytest" and (ws / ".deps").is_dir():
        env["PYTHONPATH"] = f"{WORKDIR}/.deps"
    if cmd.framework == "pytest":
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        res = run_in_sandbox(replace(spec, env=env), ws, cmd.argv, timeout_s=budget, client=client)
    except (SandboxError, DockerException, OSError) as e:
        return None, str(e) or type(e).__name__
    return res, None


def _parse(
    cmd: Command, res: SandboxResult
) -> tuple[TestRunResult | ParseError | None, CoverageResult | ParseError | None]:
    parsers = {
        "pytest": parse_junit,
        "jest": parse_jest,
        "go": parse_go_test,
        "cargo": parse_cargo_test,
    }
    cov = {"coverage-json": parse_coverage_json, "lcov": parse_lcov, "go-cover": parse_go_cover}
    parse = parsers.get(cmd.framework)
    data: bytes | str | None = res.artifacts.get(cmd.results) if cmd.results else res.stdout
    tests = parse(data, WORKDIR) if parse and data else None
    cdata = res.artifacts.get(cmd.coverage) if cmd.coverage else None
    cparse = cov.get(cmd.coverage_format or "")
    return tests, (cparse(cdata, WORKDIR) if cparse and cdata else None)


def run_tests(
    workspace: Path,
    plan: TestPlan,
    *,
    spec: SandboxSpec | None = None,
    client: docker.DockerClient | None = None,
) -> TestOutcome:
    """Execute a plan in the sandbox under its overall time cap."""
    if plan.command is None:
        return TestOutcome(plan, notice=plan.notice)
    spec = spec or SandboxSpec()
    t0 = time.monotonic()
    runs: list[RunRecord] = []
    warnings: list[str] = []

    def go(cmd: Command, full: bool, budget: float) -> tuple[SandboxResult | None, str | None]:
        s = time.monotonic()
        res, err = _execute(cmd, workspace, spec, budget, client)
        runs.append(RunRecord(cmd.argv, full, res.exit_code if res else None, time.monotonic() - s))
        return res, err

    cmd, ran_full = plan.command, plan.selection.full
    res, err = go(cmd, ran_full, plan.timeout_s)
    ok = res is not None and notice_for(res, timeout_s=plan.timeout_s) is None
    left = plan.timeout_s - (time.monotonic() - t0)
    if plan.full_command and ok and left >= MIN_FOLLOWUP_S:
        res2, err2 = go(plan.full_command, True, left)
        if res2 is not None and not res2.timed_out and not res2.oom_killed and err2 is None:
            cmd, res, ran_full = plan.full_command, res2, True
        else:
            warnings.append("full-suite follow-up did not complete; selected results shown")
    elif plan.full_command and ok:
        warnings.append("no time left for the full-suite follow-up")
    if res is None:
        return TestOutcome(
            plan,
            tuple(runs),
            ran_full,
            time.monotonic() - t0,
            notice=notice_for(None, start_error=err),
            warnings=tuple(warnings),
        )
    tests, cov = _parse(cmd, res)
    if cmd.framework == "pytest" and cmd.coverage is None and not cmd.custom:
        warnings.append("coverage unavailable: neither pytest-cov nor coverage is installed")
    note = notice_for(res, timeout_s=plan.timeout_s)
    if note is None and res.exit_code != 0 and tests is None and err is None:
        low = strip_control(res.stderr, 400).lower()
        if "no module named" in low:
            note = f"test command failed to start: {_reason(res)}"
    paths = tuple(f"{OUT_DIR}/{n}" for n in sorted(res.artifacts))
    return TestOutcome(
        plan, tuple(runs), ran_full, time.monotonic() - t0, paths, note, tests, cov, tuple(warnings)
    )


def run_project_tests(
    workspace: Path,
    project: Project | None,
    changed: Sequence[str],
    cfg: RepoConfig,
    *,
    spec: SandboxSpec | None = None,
    timeout_s: float = 300.0,
    coverage: bool = True,
    full_after_selected: bool = True,
    client: docker.DockerClient | None = None,
) -> TestOutcome:
    plan = plan_tests(
        workspace,
        project,
        changed,
        cfg,
        timeout_s=timeout_s,
        coverage=coverage,
        full_after_selected=full_after_selected,
    )
    return run_tests(workspace, plan, spec=spec, client=client)
