"""Real tests: real `git diff` in temp repos and real `ruff` runs; secrets built at runtime."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from pr_review.core.repo_config import RepoConfig
from pr_review.parsers.lint import LintResult, parse_ruff
from pr_review.review.static_checks import merge_lint, scan_diff, static_findings


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def make_diff(tmp_path: Path, name: str, before: str, after: str) -> tuple[Path, str]:
    _git(tmp_path, "init", "-q")
    f = tmp_path / name
    f.write_text(before)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    f.write_text(after)
    return tmp_path, _git(tmp_path, "diff")


def _tok() -> str:  # built at runtime: no token-shaped literal in the repo
    return "gh" + "p_" + "aB3dE5gH7jK9mN1pQ3sT5vX7zA9cD1fG3hJ5"


def test_secret_critical_and_never_leaked(tmp_path: Path) -> None:
    pw = "Zq" + "8x" + "Lm4" + "Pv9Rt2"
    _, d = make_diff(tmp_path, "a.py", "x = 1\n", f'x = 1\ntoken = "{_tok()}"\npassword = "{pw}"\n')
    fs = scan_diff(d)
    assert {f.line_start for f in fs} == {2, 3}
    for f in fs:
        assert f.severity == "CRITICAL" and f.category == "security" and f.confidence == 1.0
        assert f.summary.startswith("[secret-scan]")
        assert pw not in f.model_dump_json() and _tok() not in f.model_dump_json()


def test_placeholders_and_removed_lines_ignored(tmp_path: Path) -> None:
    old = 'password = "' + "Zq8xLm4" + 'Pv9Rt2"\n'
    new = (
        'password = "changeme123"\napi_key = "<your-api-key>"\ntoken = os.environ["TOKEN_VALUE1"]\n'
        'secret = "xxxxxxxxxxxx"\naws = "AKIAIOSFODNN7EXAMPLE"\n'
    )
    _, d = make_diff(tmp_path, "a.py", old, new)
    assert scan_diff(d) == []


SINKS = {
    "a.py": [
        "eval(x)",
        "exec(x)",
        "subprocess.run(cmd, shell=True)",
        "os.system(c)",
        'cur.execute(f"select * from t where a={a}")',
        'cur.execute("select " + a)',
        "pickle.loads(b)",
        "yaml.load(s)",
    ],
    "a.js": [
        "eval(x)",
        "new Function(x)",
        'child_process.exec("ls " + d)',
        "el.innerHTML = x",
        "<div dangerouslySetInnerHTML={h} />",
    ],
    "a.go": [
        'exec.Command("sh", "-c", cmd)',
        'db.Query("select * from t where a=" + a)',
        'db.Exec(fmt.Sprintf("delete from t where a=%s", a))',
    ],
    "a.rs": ['Command::new("sh").arg("-c").arg(format!("ls {}", d));'],
}


@pytest.mark.parametrize(("name", "lines"), SINKS.items())
def test_each_sink_flagged(tmp_path: Path, name: str, lines: list[str]) -> None:
    _, d = make_diff(tmp_path, name, "// base\n", "// base\n" + "\n".join(lines) + "\n")
    fs = scan_diff(d)
    assert sorted(f.line_start for f in fs) == list(range(2, 2 + len(lines)))
    assert all(f.severity == "MAJOR" and f.summary.startswith("[pattern]") for f in fs)


def test_safe_variants_not_flagged(tmp_path: Path) -> None:
    safe = 'yaml.load(s, Loader=yaml.SafeLoader)\ncur.execute("select 1 where a=%s", (a,))\n'
    safe += "subprocess.run(cmd)\nre.exec(x)\n"
    _, d = make_diff(tmp_path, "a.py", "x = 1\n", "x = 1\n" + safe)
    assert scan_diff(d) == []


def test_long_line_is_fast(tmp_path: Path) -> None:
    evil = 'cur.execute("' + "a" * 200_000 + "\n" + "exec(" * 20_000 + "\nx = '" + "a" * 100_000
    _, d = make_diff(tmp_path, "a.py", "x = 1\n", "x = 1\n" + evil + "\n")
    t = time.monotonic()
    scan_diff(d)
    assert time.monotonic() - t < 2


def _ruff(repo: Path) -> LintResult:
    exe = Path(sys.executable).parent / "ruff"
    out = subprocess.run(
        [str(exe), "check", "--no-cache", "--isolated", "--output-format", "json", "."],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout
    res = parse_ruff(out, str(repo.resolve()))
    assert isinstance(res, LintResult)
    return res


def test_real_ruff_merge_changed_only_and_dedupe(tmp_path: Path) -> None:
    before = "import os\n\n\ndef f() -> int:\n    return 1\n"
    after = before + "\nimport sys\n"
    repo, d = make_diff(tmp_path, "m.py", before, after)
    res = _ruff(repo)
    assert {i.line for i in res.issues if i.rule == "F401"} == {1, 7}  # real ruff output
    fs = merge_lint(d, [res, res])  # same result twice -> deduped
    assert [(f.file_path, f.line_start, f.category, f.severity) for f in fs] == [
        ("m.py", 7, "style", "MINOR")
    ]
    assert fs[0].summary.startswith("[lint:ruff] F401") and fs[0].confidence == 1.0


def test_config_filters_and_ignores(tmp_path: Path) -> None:
    repo, d = make_diff(tmp_path, "m.py", "import os\n", "import os\nimport sys\nx = 1\neval(x)\n")
    res = _ruff(repo)
    assert len(static_findings(d, [res])) == 2
    assert [f.category for f in static_findings(d, [res], RepoConfig(categories=["security"]))] == [
        "security"
    ]
    assert static_findings(d, [res], RepoConfig(min_severity="MAJOR"))[0].severity == "MAJOR"
    assert len(static_findings(d, [res], RepoConfig(min_severity="MAJOR"))) == 1
    assert static_findings(d, [res], RepoConfig(ignore=["m.py"])) == []
