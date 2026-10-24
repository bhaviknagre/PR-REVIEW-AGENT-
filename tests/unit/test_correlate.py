"""Correlation against real artifacts: git diff, pytest JUnit XML, coverage.py JSON."""

import itertools
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pr_review.core.repo_config import SEVERITY_ORDER, Category, Severity
from pr_review.parsers.chunker import Chunk, chunk_diff
from pr_review.parsers.coverage import CoverageResult, parse_coverage_json
from pr_review.parsers.tests import Location, TestRunResult, parse_junit
from pr_review.review.correlate import correlate
from pr_review.review.models import Finding

GOOD = {
    "calc.py": (
        "def divide(a, b):\n    return a / b if b else 0\n\n\ndef sub(a, b):\n    return a - b\n"
    ),
    "shapes.py": "def area(w, h):\n    return w * h\n\n\ndef perimeter(w, h):\n    return w + h\n",
    "util.py": "def twice(x):\n    return x * 2\n",
    "test_calc.py": (
        "from calc import divide\n\n\ndef test_divide():\n    assert divide(1, 0) == 0\n"
    ),
    "test_shapes.py": "from shapes import area\n\n\ndef test_area():\n    assert area(2, 3) == 6\n",
    "test_util.py": "from util import twice\n\n\ndef test_twice():\n    assert twice(2) == 4\n",
}
BAD = {
    # line 2: ZeroDivisionError at calc.py:2 (failing test); line 6: changed but untested
    "calc.py": "def divide(a, b):\n    return a / b\n\n\ndef sub(a, b):\n    return b - a\n",
    "shapes.py": GOOD["shapes.py"].replace("w + h", "2 * (w + h)"),  # line 6: never executed
    "util.py": GOOD["util.py"].replace("x * 2", "x * 3"),  # line 2: real bug, no finding
}


def _run(cwd: Path, *cmd: str) -> str:
    env = {**os.environ, "COVERAGE_FILE": str(cwd / ".cov"), "GIT_CONFIG_GLOBAL": os.devnull}
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    assert r.returncode in (0, 1), r.stderr
    return r.stdout


@pytest.fixture(scope="module")
def project(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[TestRunResult, CoverageResult, list[Chunk]]:
    root = tmp_path_factory.mktemp("fx")
    for name, text in GOOD.items():
        (root / name).write_text(text)
    for cmd in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
    ):
        _run(root, "git", *cmd)
    for name, text in BAD.items():
        (root / name).write_text(text)
    diff = _run(root, "git", "diff", "-U0")
    _run(
        root,
        sys.executable,
        "-m",
        "coverage",
        "run",
        "--include=calc.py,shapes.py,util.py",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        f"--junitxml={root / 'j.xml'}",
    )
    _run(root, sys.executable, "-m", "coverage", "json", "-o", str(root / "c.json"))
    xml = (root / "j.xml").read_text()
    tests = parse_junit(xml, str(root))
    assert isinstance(tests, TestRunResult)
    cov = parse_coverage_json((root / "c.json").read_text(), str(root))
    assert isinstance(tests, TestRunResult) and isinstance(cov, CoverageResult)
    return tests, cov, list(chunk_diff(diff).chunks)


def _f(
    path: str,
    a: int,
    b: int,
    sev: Severity = "MINOR",
    cat: Category = "correctness",
    conf: float = 0.8,
    defaulted: bool = False,
    summary: str = "s",
) -> Finding:
    return Finding(
        severity=sev,
        category=cat,
        file_path=path,
        line_start=a,
        line_end=b,
        summary=summary,
        rationale="r",
        confidence=conf,
        confidence_defaulted=defaulted,
    )


def test_real_fixture_reports(project: tuple[TestRunResult, CoverageResult, list[Chunk]]) -> None:
    tests, cov, chunks = project
    by_id = {f.test_id: f for f in tests.failures}
    assert by_id["test_calc::test_divide"].locations[0] == Location(file="calc.py", line=2)
    assert {f.test_id for f in tests.failures} == {
        "test_calc::test_divide",
        "test_util::test_twice",
    }
    assert {c.path for c in chunks} == {"calc.py", "shapes.py", "util.py"}
    assert any(fc.path == "shapes.py" and fc.uncovered for fc in cov.files)


def test_failing_test_raises_and_narrows(
    project: tuple[TestRunResult, CoverageResult, list[Chunk]],
) -> None:
    tests, cov, chunks = project
    broad = _f("calc.py", 1, 6)
    out = correlate([broad], tests, cov, chunks)
    cf = out.findings[0]
    assert cf.tags == ("test-confirmed", "uncovered")  # sub() at line 6 is in range, untested
    assert (cf.finding.severity, cf.raised_from) == ("MAJOR", "MINOR")
    assert (cf.finding.line_start, cf.finding.line_end) == (2, 2)  # first changed line
    assert cf.original == broad


def test_uncovered_security_raises_one_level(
    project: tuple[TestRunResult, CoverageResult, list[Chunk]],
) -> None:
    tests, cov, chunks = project
    sec = correlate([_f("shapes.py", 5, 6, "MINOR", "security")], tests, cov, chunks).findings[0]
    assert sec.tags == ("uncovered",) and sec.finding.severity == "MAJOR"
    plain = correlate([_f("shapes.py", 5, 6, "MINOR", "style")], tests, cov, chunks).findings[0]
    assert plain.tags == ("uncovered",) and plain.finding.severity == "MINOR"
    capped = correlate(
        [_f("shapes.py", 6, 6, "CRITICAL", "security")], tests, cov, chunks
    ).findings[0]
    assert capped.finding.severity == "CRITICAL" and capped.raised_from is None
    covered = correlate([_f("shapes.py", 1, 2, "MINOR", "security")], tests, cov, chunks).findings[
        0
    ]
    assert covered.tags == ()


def test_unrelated_failing_test_listed(
    project: tuple[TestRunResult, CoverageResult, list[Chunk]],
) -> None:
    tests, cov, chunks = project
    out = correlate([_f("calc.py", 2, 2)], tests, cov, chunks)
    assert [u.test_id for u in out.unmatched_tests] == ["test_util::test_twice"]
    assert out.unmatched_tests[0].message.startswith("assert 6 == 4")


def test_no_reports_changes_nothing() -> None:
    f = _f("a.py", 1, 9)
    out = correlate([f])
    assert out.findings[0].finding == f and out.unmatched_tests == ()


def test_dedupe_and_ranking_deterministic(
    project: tuple[TestRunResult, CoverageResult, list[Chunk]],
) -> None:
    tests, cov, chunks = project
    fs = [
        _f("shapes.py", 5, 6, "MINOR", "style", 0.9, summary="low"),
        _f("shapes.py", 6, 6, "MAJOR", "style", 0.5, summary="high"),  # overlaps, same category
        _f("util.py", 2, 2, "MAJOR", "style", 0.9, True, summary="defaulted"),
        _f("util.py", 1, 1, "MAJOR", "correctness", 0.6, summary="solid"),
        _f("calc.py", 2, 2, "MINOR", "correctness", 0.1, summary="confirmed"),
    ]
    first = correlate(fs, tests, cov, chunks)
    for perm in itertools.islice(itertools.permutations(fs), 24):
        assert correlate(perm, tests, cov, chunks) == first
    assert [c.finding.summary for c in first.findings] == [
        "confirmed",
        "solid",
        "defaulted",
        "high",
    ]
    assert "low" not in {c.finding.summary for c in first.findings}


def test_never_lowers_severity(project: tuple[TestRunResult, CoverageResult, list[Chunk]]) -> None:
    tests, cov, chunks = project
    for sev, cat, path in itertools.product(
        SEVERITY_ORDER, ("security", "style"), ("calc.py", "shapes.py", "util.py")
    ):
        out = correlate([_f(path, 1, 6, sev, cat)], tests, cov, chunks).findings[0]
        assert SEVERITY_ORDER.index(out.finding.severity) <= SEVERITY_ORDER.index(sev)


def test_module_with_two_findings_only_exact_location_raises(
    project: tuple[TestRunResult, CoverageResult, list[Chunk]],
) -> None:
    tests, cov, chunks = project
    at_failure, elsewhere = _f("calc.py", 2, 2, summary="at"), _f("calc.py", 6, 6, summary="else")
    by = {
        c.finding.summary: c
        for c in correlate([elsewhere, at_failure], tests, cov, chunks).findings
    }
    assert by["at"].tags[0] == "test-confirmed" and by["at"].finding.severity == "MAJOR"
    assert by["else"].tags[0] == "test-related" and by["else"].finding.severity == "MINOR"
    assert by["else"].raised_from is None


@pytest.mark.parametrize("llm_severity", ["CRITICAL", "MAJOR", "MINOR"])
def test_static_finding_survives_overlapping_llm_finding(llm_severity: Severity) -> None:
    def f(sev: Severity, summary: str, start: int, end: int, conf: float) -> Finding:
        return Finding(
            severity=sev, category="security", file_path="user_store.py", line_start=start,
            line_end=end, summary=summary, rationale="r", confidence=conf,
        )  # fmt: skip

    static = f("MAJOR", "[pattern] Injection sink (string-built SQL)", 10, 10, 1.0)
    llm = f(llm_severity, "SQL injection in find_user", 8, 12, 0.9)
    kept = correlate([llm, static]).findings
    assert [(k.finding.summary, k.finding.line_start) for k in kept] == [(static.summary, 10)]
