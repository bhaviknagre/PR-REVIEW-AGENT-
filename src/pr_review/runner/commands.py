"""Pure argv builders for test runs inside the sandbox (never shell strings).

Every builder writes machine-readable output under /out (the only writable, collected path).
Targets are project-root-relative; `root` is the project's directory inside the workspace.

Per framework:
- pytest: `python -m pytest --junitxml=/out/junit.xml -q -p no:cacheprovider`. Coverage uses
  pytest-cov when installed, else a one-argv coverage.py driver (coverage run + json in one
  process, because the sandbox runs a single argv and /tmp is per-container); without either,
  tests run uncovered.
- jest: `npx --no-install jest --ci --json --outputFile=/out/jest.json --coverage` (lcov in /out).
- go: `go test -json -coverprofile=/out/cover.out` (JSON events on stdout).
- cargo: plain `cargo test --no-fail-fast` (parsed by the cargo text parser; the JSON format
  needs nightly `-Z unstable-options`, which the parser does not read, so it is not used).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pr_review.runner.detect import Language, Project
from pr_review.runner.sandbox import OUT_DIR, WORKDIR

MAX_ARGV = 64
MAX_ARG_LEN = 512
_BAD_ARG = re.compile(r"[\x00-\x1f\x7f]")

_COV_DRIVER = (
    "import sys, coverage, pytest\n"
    f"c = coverage.Coverage(data_file='/tmp/.coverage', include=['{WORKDIR}/*'],"
    f" omit=['{WORKDIR}/.deps/*'])\n"
    "c.start()\n"
    "rc = int(pytest.main(sys.argv[1:]))\n"
    "c.stop()\n"
    "c.save()\n"
    "try:\n"
    f"    c.json_report(outfile='{OUT_DIR}/coverage.json')\n"
    "except coverage.CoverageException:\n"
    "    pass\n"
    "sys.exit(rc)\n"
)


class CommandError(ValueError):
    """A repo-config command that fails validation."""


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    framework: str  # pytest | jest | go | cargo
    results: str | None  # artifact name under /out; None when results are read from stdout
    coverage: str | None  # artifact name of the coverage report, if any
    coverage_format: str | None  # coverage-json | lcov | go-cover
    env: tuple[tuple[str, str], ...] = ()
    custom: bool = False  # argv came from the repo config


def validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Repo-config argv check (it is executed without a shell, inside the sandbox)."""
    if not argv or len(argv) > MAX_ARGV:
        raise CommandError("argv must have 1..64 items")
    for a in argv:
        if not a or len(a) > MAX_ARG_LEN or _BAD_ARG.search(a):
            raise CommandError("argv item empty, too long, or has control characters")
    if argv[0].startswith("-"):
        raise CommandError("argv[0] must be an executable")
    return tuple(argv)


def has_module(workspace: Path, name: str) -> bool:
    """Whether an installed dependency (`<workspace>/.deps`, see install.py) provides `name`."""
    d = workspace / ".deps"
    return (d / name).is_dir() or (d / f"{name}.py").is_file()


def _join(root: str, p: str) -> str:
    return p if root in ("", ".") else f"{root}/{p}"


def pytest_command(
    root: str,
    targets: Sequence[str] = (),
    *,
    coverage: bool = True,
    pytest_cov: bool = False,
    coverage_py: bool = False,
) -> Command:
    base = ["--junitxml=/out/junit.xml", "-q", "-p", "no:cacheprovider"]
    base += [f"--ignore={_join(root, '.deps')}"]
    if root not in ("", "."):
        base.append(f"--rootdir={root}")
    paths = [_join(root, t) for t in targets] or [root or "."]
    if coverage and pytest_cov:
        argv = ["python", "-m", "pytest", *base, "--cov", "--cov-report=json:/out/coverage.json"]
        return Command(
            (*argv, *paths),
            "pytest",
            "junit.xml",
            "coverage.json",
            "coverage-json",
            env=(("COVERAGE_FILE", "/tmp/.coverage"),),  # noqa: S108 - in-container tmpfs
        )
    if coverage and coverage_py:
        return Command(
            ("python", "-c", _COV_DRIVER, *base, *paths),
            "pytest",
            "junit.xml",
            "coverage.json",
            "coverage-json",
        )
    argv = ["python", "-m", "pytest", *base, *paths]
    return Command(tuple(argv), "pytest", "junit.xml", None, None)


def jest_command(root: str, related: Sequence[str] = (), *, coverage: bool = True) -> Command:
    argv = ["npx", "--no-install", "jest", "--ci", "--json", "--outputFile=/out/jest.json"]
    argv.append("--passWithNoTests")
    if root not in ("", "."):
        argv.append(f"--rootDir={root}")
    if coverage:
        argv += ["--coverage", "--coverageReporters=lcov", f"--coverageDirectory={OUT_DIR}"]
    if related:
        argv += ["--findRelatedTests", *[_join(root, p) for p in related]]
    return Command(
        tuple(argv),
        "jest",
        "jest.json",
        "lcov.info" if coverage else None,
        "lcov" if coverage else None,
    )


def go_command(root: str, packages: Sequence[str] = (), *, coverage: bool = True) -> Command:
    argv = ["go"]
    if root not in ("", "."):
        argv += ["-C", root]
    argv += ["test", "-json"]
    if coverage:
        argv.append("-coverprofile=/out/cover.out")
    argv += list(packages) or ["./..."]
    env = (("GOCACHE", "/tmp/gocache"), ("GOPATH", "/tmp/go"), ("GOTOOLCHAIN", "local"))  # noqa: S108
    return Command(
        tuple(argv),
        "go",
        None,
        "cover.out" if coverage else None,
        "go-cover" if coverage else None,
        env,
    )


def cargo_command(root: str, crates: Sequence[str] = ()) -> Command:
    argv = ["cargo", "test", "--no-fail-fast"]
    if root not in ("", "."):
        argv += ["--manifest-path", f"{root}/Cargo.toml"]
    for c in crates:
        argv += ["-p", c]
    env = (
        ("CARGO_HOME", "/tmp/cargo"),  # noqa: S108
        ("CARGO_TARGET_DIR", "/tmp/target"),  # noqa: S108
        ("CARGO_NET_OFFLINE", "true"),
    )
    return Command(tuple(argv), "cargo", None, None, None, env)


def build_command(
    project: Project,
    targets: Sequence[str],
    *,
    workspace: Path,
    coverage: bool = True,
    override: Sequence[str] | None = None,
) -> Command | None:
    """Command for `project` or None if no supported framework. An override keeps the parse
    metadata of the built-in command but replaces argv (validated, still no shell)."""
    fw = project.test_framework
    root = project.root
    cmd: Command | None
    if project.language is Language.PYTHON:
        cmd = pytest_command(
            root,
            targets,
            coverage=coverage,
            pytest_cov=has_module(workspace, "pytest_cov"),
            coverage_py=has_module(workspace, "coverage"),
        )
    elif project.language is Language.NODE and fw == "jest":
        cmd = jest_command(root, targets, coverage=coverage)
    elif project.language is Language.GO:
        cmd = go_command(root, targets, coverage=coverage)
    elif project.language is Language.RUST:
        cmd = cargo_command(root, targets)
    else:
        cmd = None
    if override is None:
        return cmd
    argv = validate_argv(override)
    if cmd is None:
        return Command(argv, fw or "custom", None, None, None, custom=True)
    return Command(
        argv, cmd.framework, cmd.results, cmd.coverage, cmd.coverage_format, cmd.env, custom=True
    )
