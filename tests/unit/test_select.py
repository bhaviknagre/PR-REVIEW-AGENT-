"""Test planning: pure builder/selection tests plus real pytest+coverage runs in the real
sandbox on real git diffs. Only Python is executed for real (the node/go/rust images are not
allowlisted yet, T-42); jest/go/cargo are covered by pure argv and selection tests."""

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import docker
import pytest

from pr_review.core.repo_config import RepoConfig
from pr_review.runner.commands import (
    CommandError,
    build_command,
    cargo_command,
    go_command,
    jest_command,
    pytest_command,
    validate_argv,
)
from pr_review.runner.detect import Language, Project
from pr_review.runner.install import install_deps
from pr_review.runner.sandbox import LABEL, SandboxResult
from pr_review.runner.select import (
    changed_paths,
    notice_for,
    plan_tests,
    run_project_tests,
    select_tests,
    strip_control,
)

try:
    CLIENT: docker.DockerClient | None = docker.from_env()
    assert CLIENT is not None
    CLIENT.ping()
    REASON = ""
except Exception as e:  # noqa: BLE001
    CLIENT, REASON = None, f"Docker daemon unreachable: {e}"


def real[F](fn: F) -> F:
    skip = pytest.mark.skipif(bool(REASON), reason=REASON or "x")
    return pytest.mark.docker(skip(fn))  # type: ignore[no-any-return]


PY = Project(".", Language.PYTHON, "pip", "pytest")

FILES = {
    "requirements.txt": "pytest==8.3.4\ncoverage==7.6.10\n",
    "conftest.py": "# shared fixtures\n",
    "pkg/__init__.py": "",
    "pkg/calc.py": "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
    "pkg/util.py": "def ident(x):\n    return x\n",
    "tests/test_calc.py": (
        "from pkg.calc import add, sub\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
        "def test_sub():\n    assert sub(3, 1) == 2\n"
    ),
    "tests/test_other.py": "def test_other():\n    assert 1 + 1 == 2\n",
}


def _git(cwd: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "HOME": str(cwd)}  # fmt: skip
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


@pytest.fixture(scope="module")
def base_ws(tmp_path_factory: pytest.TempPathFactory, pkg_index: str | None) -> Iterator[Path]:
    """Fixture repo with pytest+coverage installed through the real install step."""
    src = tmp_path_factory.mktemp("fx")
    _write(src, FILES)
    r = install_deps(src, client=CLIENT, index_url=pkg_index)
    assert r.ok and r.workspace is not None, r.stderr
    yield r.workspace
    shutil.rmtree(r.workspace, ignore_errors=True)


@pytest.fixture
def leftovers() -> Iterator[None]:
    yield
    assert CLIENT is not None
    f: dict[str, str | list[str] | bool] = {"label": f"{LABEL}=1"}
    assert not CLIENT.containers.list(all=True, filters=f)
    assert not CLIENT.volumes.list(filters=f)


def _scenario(
    tmp_path: Path, base: Path, edits: dict[str, str]
) -> tuple[Path, str, tuple[str, ...]]:
    """Workspace = base + edits; changed paths come from a real `git diff` of the same edits."""
    g = tmp_path / "git"
    _write(g, FILES)
    _git(g, "init", "-q")
    _git(g, "add", "-A")
    _git(g, "commit", "-qm", "base")
    _write(g, edits)
    diff = _git(g, "diff", "--no-color")
    ws = tmp_path / "ws"
    shutil.copytree(base, ws)
    _write(ws, edits)
    return ws, diff, changed_paths(diff)


# ----------------------------------------------------------------------------- real runs


@real
def test_changed_module_runs_only_its_tests_with_coverage(
    tmp_path: Path, base_ws: Path, leftovers: None
) -> None:
    edits = {"pkg/calc.py": FILES["pkg/calc.py"].replace("a - b", "a - b + 0")}
    ws, diff, changed = _scenario(tmp_path, base_ws, edits)
    out = run_project_tests(ws, PY, changed, RepoConfig(), client=CLIENT)
    assert out.plan.selection.targets == ("tests/test_calc.py",)
    assert not out.plan.selection.full and not out.ran_full
    assert "tests/test_other.py" not in " ".join(out.runs[0].argv)
    assert out.notice is None and out.warnings == ()
    assert out.tests is not None and not hasattr(out.tests, "error_class")
    assert (out.tests.passed, out.tests.failed) == (2, 0)  # test_other (1 test) did not run
    assert "/out/junit.xml" in out.artifact_paths and "/out/coverage.json" in out.artifact_paths
    assert out.coverage is not None and not hasattr(out.coverage, "error_class")
    fc = next(f for f in out.coverage.files if f.path == "pkg/calc.py")
    changed_line = 6  # the edited `return a - b + 0`
    assert any(a <= changed_line <= b for a, b in fc.covered)
    assert "+    return a - b + 0" in diff


@real
def test_config_change_runs_full_suite(tmp_path: Path, base_ws: Path, leftovers: None) -> None:
    ws, _, changed = _scenario(tmp_path, base_ws, {"conftest.py": "# shared fixtures v2\n"})
    out = run_project_tests(ws, PY, changed, RepoConfig(), client=CLIENT)
    assert out.plan.selection.full and "conftest.py" in out.plan.selection.reason
    assert out.tests is not None and not hasattr(out.tests, "error_class")
    assert out.tests.passed == 3


@real
def test_failing_test_is_parsed_with_locations_and_then_full_followup(
    tmp_path: Path, base_ws: Path, leftovers: None
) -> None:
    edits = {
        "pkg/calc.py": FILES["pkg/calc.py"].replace("a + b", "a + b + 1"),
        "pkg/util.py": "def ident(x):\n    return x  # edited, no test imports this\n",
    }
    ws, _, changed = _scenario(tmp_path, base_ws, edits)
    out = run_project_tests(ws, PY, changed, RepoConfig(), client=CLIENT)
    assert out.plan.selection.partial and out.plan.full_command is not None
    assert [r.exit_code for r in out.runs] == [1, 1] and out.ran_full  # failures do not block
    t = out.tests
    assert t is not None and not hasattr(t, "error_class")
    assert t.failed == 1 and t.failures[0].test_id.endswith("test_add")
    locs = {(loc.file, loc.line) for loc in t.failures[0].locations}
    assert ("tests/test_calc.py", 5) in locs
    assert out.notice is None  # a failing test is a result, not a runner problem


@real
def test_partial_selection_follows_up_with_full_suite(
    tmp_path: Path, base_ws: Path, leftovers: None
) -> None:
    edits = {
        "pkg/calc.py": FILES["pkg/calc.py"].replace("a - b", "a - b + 0"),
        "pkg/util.py": "def ident(x):\n    return x  # edited\n",
    }
    ws, _, changed = _scenario(tmp_path, base_ws, edits)
    out = run_project_tests(ws, PY, changed, RepoConfig(), client=CLIENT)
    assert [r.full for r in out.runs] == [False, True]
    assert out.ran_full and out.tests is not None
    assert getattr(out.tests, "passed", 0) == 3
    off = run_project_tests(ws, PY, changed, RepoConfig(), full_after_selected=False, client=CLIENT)
    assert len(off.runs) == 1 and not off.ran_full


@real
def test_repo_config_override_argv_is_used(tmp_path: Path, base_ws: Path, leftovers: None) -> None:
    ws, _, changed = _scenario(tmp_path, base_ws, {"pkg/calc.py": FILES["pkg/calc.py"] + "\n"})
    argv = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml=/out/junit.xml",
            "tests/test_other.py"]  # fmt: skip
    cfg = RepoConfig(test_commands={".": argv})
    out = run_project_tests(ws, PY, changed, cfg, client=CLIENT)
    assert out.runs[0].argv == tuple(argv)
    assert getattr(out.tests, "passed", 0) == 1


@real
def test_tests_disabled_skips_with_notice_and_no_container(
    tmp_path: Path, base_ws: Path, leftovers: None
) -> None:
    assert CLIENT is not None
    before = len(CLIENT.containers.list(all=True))
    out = run_project_tests(base_ws, PY, ("pkg/calc.py",), RepoConfig(tests_disabled=True))
    assert out.notice == "tests disabled by repo config" and out.runs == ()
    assert out.plan.command is None
    assert len(CLIENT.containers.list(all=True)) == before


@real
def test_timeout_notice(tmp_path: Path, base_ws: Path, leftovers: None) -> None:
    cfg = RepoConfig(test_commands={".": ["python", "-c", "import time; time.sleep(60)"]},
                     test_timeout_seconds=3)  # fmt: skip
    out = run_project_tests(base_ws, PY, ("pkg/calc.py",), cfg, client=CLIENT)
    assert out.notice == "test run timed out after 3s"
    assert out.tests is None


@real
def test_missing_executable_is_a_start_failure(base_ws: Path, leftovers: None) -> None:
    cfg = RepoConfig(test_commands={".": ["no-such-binary-xyz"]})
    out = run_project_tests(base_ws, PY, ("pkg/calc.py",), cfg, client=CLIENT)
    assert out.notice is not None
    assert out.notice.startswith("test command failed to start: ")


def test_unknown_project_means_no_tests_detected(tmp_path: Path) -> None:
    out = run_project_tests(tmp_path, None, ("a.txt",), RepoConfig())
    assert out.notice == "no tests detected" and out.runs == ()
    vitest = Project(".", Language.NODE, "npm", "vitest")
    assert (
        run_project_tests(tmp_path, vitest, ("a.js",), RepoConfig()).notice == "no tests detected"
    )


# ----------------------------------------------------------------------------- pure: notices


def _res(**kw: object) -> SandboxResult:
    base: dict[str, object] = {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False,
                               "oom_killed": False, "duration_s": 1.0, "artifacts": {},
                               "rootless": False}  # fmt: skip
    return SandboxResult(**{**base, **kw})  # type: ignore[arg-type]


def test_notice_mapping() -> None:
    assert notice_for(None, disabled=True) == "tests disabled by repo config"
    assert notice_for(None, detected=False) == "no tests detected"
    assert notice_for(_res(oom_killed=True, exit_code=137)) == "test run killed: memory limit"
    assert notice_for(_res(timed_out=True, exit_code=137), timeout_s=30) == (
        "test run timed out after 30s"
    )
    assert notice_for(_res(artifacts_rejected=True)) == "artifacts rejected: too large"
    net = _res(exit_code=1, stderr="OSError: [Errno 101] Network is unreachable")
    assert notice_for(net) == "network access blocked"
    assert notice_for(_res(exit_code=1, stderr="2 failed")) is None
    assert notice_for(_res()) is None
    gone = _res(exit_code=127, stderr="\x1b[31mexec: \x00 not found\x1b[0m\nsecond line")
    assert notice_for(gone) == "test command failed to start: exec: not found"
    assert notice_for(None, start_error="boom\x1b[2J\x85\x9bok") == (
        "test command failed to start: boom ok"
    )


def test_strip_control_removes_ansi_c0_c1() -> None:
    dirty = "\x1b[1;31mred\x1b[0m\x07\x1b]0;title\x07 a\x00b\x85c\x9bd\ne\x7f"
    assert strip_control(dirty) == "red a b c d e"
    assert len(strip_control("x" * 1000)) == 160


# ----------------------------------------------------------------------------- pure: argv


def test_pytest_argv_variants() -> None:
    plain = pytest_command(".", ["tests/test_a.py"], coverage=False)
    assert plain.argv[:4] == ("python", "-m", "pytest", "--junitxml=/out/junit.xml")
    assert plain.argv[-1] == "tests/test_a.py" and plain.coverage is None
    cov = pytest_command(".", coverage=True, pytest_cov=True)
    assert "--cov-report=json:/out/coverage.json" in cov.argv and cov.coverage == "coverage.json"
    drv = pytest_command("svc", ["t.py"], coverage=True, coverage_py=True)
    assert drv.argv[:2] == ("python", "-c") and "coverage.json" in drv.argv[2]
    assert "svc/t.py" in drv.argv and "--rootdir=svc" in drv.argv
    assert pytest_command(".", coverage=True).coverage is None  # neither installed: fall back


def test_jest_go_cargo_argv() -> None:
    full = jest_command(".")
    assert "--findRelatedTests" not in full.argv and "--coverage" in full.argv
    assert {"--ci", "--json", "--outputFile=/out/jest.json", "--coverageReporters=lcov",
            "--coverageDirectory=/out"} <= set(full.argv)  # fmt: skip
    rel = jest_command("web", ["src/a.ts", "src/b.ts"])
    i = rel.argv.index("--findRelatedTests")
    assert rel.argv[i + 1 :] == ("web/src/a.ts", "web/src/b.ts") and "--rootDir=web" in rel.argv
    g = go_command(".", ["./pkg/a", "./"])
    assert g.argv == ("go", "test", "-json", "-coverprofile=/out/cover.out", "./pkg/a", "./")
    assert (
        go_command("svc").argv[:3] == ("go", "-C", "svc") and go_command("svc").argv[-1] == "./..."
    )
    c = cargo_command("rs", ["core"])
    assert c.argv == ("cargo", "test", "--no-fail-fast", "--manifest-path", "rs/Cargo.toml",
                      "-p", "core")  # fmt: skip
    assert "-Z" not in c.argv


def test_override_is_validated_argv() -> None:
    ok = build_command(PY, (), workspace=Path("."), override=["pytest", "-x"])
    assert ok is not None and ok.argv == ("pytest", "-x") and ok.custom and ok.framework == "pytest"
    for bad in ([], ["-rm"], ["a\x00b"], ["x", "y\nz"], ["x"] * 65, ["a" * 513]):
        with pytest.raises(CommandError):
            validate_argv(bad)
    cfg = RepoConfig(test_commands={".": ["bad\x07"]})  # pydantic only rejects NUL
    p = plan_tests(Path("."), PY, ("a.py",), cfg)
    assert p.command is None and (p.notice or "").startswith("test command failed to start")


# ----------------------------------------------------------------------------- pure: selection


def test_python_selection_rules(tmp_path: Path) -> None:
    _write(tmp_path, FILES)
    _write(tmp_path, {
        "tests/test_integration.py": "from pkg import util\n\n\ndef test_u():\n    pass\n",
        "tests/test_misc.py": "import pkg.util\n",
        "tests/test_nope.py": "# mentions pkg.util only in a comment\n",
        "pkg/widget.py": "x = 1\n",
        "widget_test.py": "def test_w():\n    pass\n",
    })  # fmt: skip
    sel = lambda *c: select_tests(tmp_path, PY, c)  # noqa: E731
    assert sel("tests/test_other.py").targets == ("tests/test_other.py",)
    assert sel("pkg/calc.py").targets == ("tests/test_calc.py",)
    assert set(sel("pkg/util.py").targets) == {"tests/test_integration.py", "tests/test_misc.py"}
    assert sel("pkg/widget.py").targets == ("widget_test.py",)
    assert sel("pkg/calc.py", "README.md").targets == ("tests/test_calc.py",)
    assert not sel("pkg/calc.py", "README.md").partial
    assert sel("pkg/calc.py", "pkg/new.py").partial
    for cfgfile in ("conftest.py", "tests/conftest.py", "pyproject.toml", "setup.cfg",
                    "requirements-dev.txt", "pytest.ini"):  # fmt: skip
        assert sel("pkg/calc.py", cfgfile).full
    assert sel("README.md").full and sel("README.md").targets == ()  # nothing mapped
    assert sel("pkg/orphan.py").full  # uncertain -> full suite
    assert sel("tests/gone.py").full  # deleted test: nothing to run
    assert select_tests(tmp_path, Project("svc", Language.PYTHON, None, "pytest"), ("a.py",)).full


def test_node_go_rust_selection_rules(tmp_path: Path) -> None:
    node = Project(".", Language.NODE, "npm", "jest")
    s = select_tests(tmp_path, node, ("src/a.ts", "README.md", "src/b.jsx"))
    assert s.targets == ("src/a.ts", "src/b.jsx") and not s.full
    for c in ("package.json", "jest.config.js", "tsconfig.json"):
        assert select_tests(tmp_path, node, ("src/a.ts", c)).full
    assert select_tests(tmp_path, node, ("README.md",)).full

    go = Project(".", Language.GO, "go", "go test")
    s = select_tests(tmp_path, go, ("a.go", "pkg/x/y.go", "pkg/x/z.go", "doc.md"))
    assert s.targets == ("./", "./pkg/x")
    assert select_tests(tmp_path, go, ("pkg/x/y.go", "go.mod")).full
    sub = Project("svc", Language.GO, "go", "go test")
    assert select_tests(tmp_path, sub, ("svc/p/a.go", "other/b.go")).targets == ("./p",)

    _write(tmp_path, {
        "Cargo.toml": "[workspace]\nmembers = ['core', 'cli']\n",
        "core/Cargo.toml": "[package]\nname = 'mycore'\nversion = '0.1.0'\n",
        "core/src/lib.rs": "",
        "cli/Cargo.toml": "[package]\nname = 'mycli'\nversion = '0.1.0'\n",
        "cli/src/main.rs": "",
        "build.rs": "",
    })  # fmt: skip
    rust = Project(".", Language.RUST, "cargo", "cargo test")
    s = select_tests(tmp_path, rust, ("core/src/lib.rs", "cli/src/main.rs", "cli/README.md"))
    assert s.targets == ("mycore", "mycli") and not s.full
    assert select_tests(tmp_path, rust, ("core/src/lib.rs", "Cargo.lock")).full
    assert select_tests(tmp_path, rust, ("build.rs",)).full  # root crate = whole crate
    assert select_tests(tmp_path, rust, ("core/Cargo.toml",)).targets == ("mycore",)


def test_changed_paths_from_real_git_diff(tmp_path: Path) -> None:
    _write(tmp_path, FILES)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "pkg/util.py").unlink()
    _write(tmp_path, {"pkg/new.py": "x = 1\n", "pkg/calc.py": "y = 2\n"})
    _git(tmp_path, "add", "-A")
    got = changed_paths(_git(tmp_path, "diff", "--cached", "--no-color"))
    assert set(got) == {"pkg/util.py", "pkg/new.py", "pkg/calc.py"}
