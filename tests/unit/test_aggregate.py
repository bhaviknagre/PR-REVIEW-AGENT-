"""Aggregation on real artifacts: git diff -> chunk_diff -> static_checks, pytest JUnit + coverage
JSON through the real parsers, real correlate; plus a path with a real Ollama FindingSet."""

import itertools
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

from pr_review.core.repo_config import Category, RepoConfig, Severity
from pr_review.core.settings import Settings
from pr_review.llm.analyze import analyze_chunk
from pr_review.llm.budget import degraded_notice
from pr_review.llm.provider import BudgetExceeded, Provider
from pr_review.parsers.chunker import ChunkPlan, chunk_diff
from pr_review.parsers.coverage import CoverageResult, parse_coverage_json
from pr_review.parsers.tests import TestRunResult, parse_junit
from pr_review.review.aggregate import MAX_INLINE, ReviewPlan, aggregate
from pr_review.review.correlate import Correlation, correlate
from pr_review.review.models import FailedCategory, FailedChunk, Finding, FindingSet
from pr_review.review.static_checks import static_findings

OLLAMA = os.environ.get("PR_REVIEW_TEST_OLLAMA", "http://localhost:11434")
MODEL = os.environ.get("PR_REVIEW_TEST_LLM_MODEL", "ollama/qwen2.5-coder:1.5b")

BASE = {
    "calc.py": (
        "def divide(a, b):\n    return a / b if b else 0\n\n\ndef sub(a, b):\n    return a - b\n"
    ),
    "util.py": "def twice(x):\n    return x * 2\n",
    "app.py": 'import os\n\nNAME = "app"\n',
    "uv.lock": "version = 1\n",
    "test_calc.py": (
        "from calc import divide\n\n\ndef test_divide():\n    assert divide(1, 0) == 0\n"
    ),
    "test_util.py": "from util import twice\n\n\ndef test_twice():\n    assert twice(2) == 4\n",
}
HEAD = {
    "calc.py": "def divide(a, b):\n    return a / b\n\n\ndef sub(a, b):\n    return b - a\n",
    "util.py": "def twice(x):\n    return x * 3\n",
    # line 4: hard-coded AWS key (secret-scan CRITICAL); line 5: os.system sink (pattern MAJOR)
    "app.py": 'import os\n\nNAME = "app"\nKEY = "AKIAZ7QW3XK9PLM2RT5V"\nos.system("ls " + NAME)\n',
    "uv.lock": "version = 2\n",
}


@dataclass
class Pipeline:
    diff: str
    plan: ChunkPlan
    tests: TestRunResult
    cov: CoverageResult
    static: list[Finding]


def _run(cwd: Path, *cmd: str) -> str:
    env = {**os.environ, "COVERAGE_FILE": str(cwd / ".cov"), "GIT_CONFIG_GLOBAL": os.devnull}
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    assert r.returncode in (0, 1), r.stderr
    return r.stdout


@pytest.fixture(scope="module")
def pipe(tmp_path_factory: pytest.TempPathFactory) -> Pipeline:
    root = tmp_path_factory.mktemp("agg")
    for name, text in BASE.items():
        (root / name).write_text(text)
    for cmd in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
    ):
        _run(root, "git", *cmd)
    for name, text in HEAD.items():
        (root / name).write_text(text)
    diff = _run(root, "git", "diff", "-U0")
    _run(
        root,
        sys.executable,
        "-m",
        "coverage",
        "run",
        "--include=calc.py,util.py",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        f"--junitxml={root / 'j.xml'}",
    )
    _run(root, sys.executable, "-m", "coverage", "json", "-o", str(root / "c.json"))
    tests = parse_junit((root / "j.xml").read_text(), str(root))
    cov = parse_coverage_json((root / "c.json").read_text(), str(root))
    assert isinstance(tests, TestRunResult) and isinstance(cov, CoverageResult)
    return Pipeline(diff, chunk_diff(diff), tests, cov, static_findings(diff))


def _f(
    path: str,
    a: int,
    b: int | None = None,
    sev: Severity = "MINOR",
    cat: Category = "correctness",
    conf: float = 0.8,
    summary: str = "llm finding",
    defaulted: bool = False,
    rationale: str = "because",
    suggestion: str | None = None,
) -> Finding:
    return Finding(
        severity=sev,
        category=cat,
        file_path=path,
        line_start=a,
        line_end=b or a,
        summary=summary,
        rationale=rationale,
        suggestion=suggestion,
        confidence=conf,
        confidence_defaulted=defaulted,
    )


def _plan(p: Pipeline, llm: list[Finding] = (), **kw: object) -> ReviewPlan:  # type: ignore[assignment]
    cors: list[Correlation] = [
        correlate(p.static, p.tests, p.cov, p.plan.chunks),
        correlate(llm, p.tests, p.cov, p.plan.chunks),
    ]
    return aggregate(cors, p.plan, received=len(p.static) + len(llm), **kw)  # type: ignore[arg-type]


def _check_counts(r: ReviewPlan) -> None:
    c = r.counts
    assert c.balanced()
    assert c.inline == len(r.inline)
    assert c.summary_only == sum(len(g.items) for g in r.summary)


def test_real_inputs(pipe: Pipeline) -> None:
    kinds = {(f.file_path, f.line_start, f.severity) for f in pipe.static}
    assert ("app.py", 4, "CRITICAL") in kinds and ("app.py", 5, "MAJOR") in kinds
    assert [(s.path, s.reason) for s in pipe.plan.skipped] == [("uv.lock", "lock file")]
    assert {f.test_id for f in pipe.tests.failures} == {
        "test_calc::test_divide",
        "test_util::test_twice",
    }


def test_secret_and_llm_same_line_dedupe_prefers_secret_scan(pipe: Pipeline) -> None:
    llm = [_f("app.py", 4, sev="MAJOR", cat="security", conf=0.9, summary="hard-coded key")]
    r = _plan(pipe, llm)
    on4 = [c for c in r.inline if c.line == 4]
    assert len(on4) == 1
    assert "[secret-scan]" in on4[0].body and "hard-coded key" not in on4[0].body
    assert (on4[0].severity, on4[0].side) == ("CRITICAL", "RIGHT")
    assert r.counts.deduped == 1 and r.counts.total == 3
    _check_counts(r)


def test_dedupe_keeps_highest_severity_and_merges_tags(pipe: Pipeline) -> None:
    llm = [_f("app.py", 5, sev="CRITICAL", cat="security", conf=0.9, summary="shell injection")]
    r = _plan(pipe, llm)
    (c,) = [c for c in r.inline if c.line == 5]
    assert c.severity == "CRITICAL" and "[pattern]" in c.body  # deterministic text, higher severity
    other_category = _plan(pipe, [_f("app.py", 5, cat="style", summary="naming")])
    assert [c.line for c in other_category.inline].count(5) == 2  # different category: not a dup
    confirmed = _plan(pipe, [_f("calc.py", 1, 2, "MINOR")])  # test-confirmed at calc.py:2
    (cc,) = [c for c in confirmed.inline if c.file == "calc.py" and c.line == 2]
    assert cc.severity == "MAJOR" and "test-confirmed" in cc.body


def test_off_diff_and_file_level_go_to_summary(pipe: Pipeline) -> None:
    llm = [
        _f("calc.py", 1, sev="MAJOR", summary="unchanged def"),  # exists, not a changed line
        _f("uv.lock", 1, sev="MINOR", summary="lock bump"),  # skipped file
        _f("calc.py", 40, sev="MINOR", summary="past the end"),
    ]
    r = _plan(pipe, llm)
    assert not [c for c in r.inline if c.file == "uv.lock" or c.line in (1, 40)]
    items = {i.text: i for g in r.summary for i in g.items}
    assert set(items) == {"unchanged def", "lock bump", "past the end"}
    assert all(i.reason == "off-diff" for i in items.values())
    assert [g.severity for g in r.summary] == ["MAJOR", "MINOR"]
    _check_counts(r)


def test_cap_overflow_goes_to_summary_and_secret_stays(pipe: Pipeline) -> None:
    llm = [
        _f("calc.py", 6, sev="MAJOR", conf=0.9, summary="swapped operands"),
        _f("util.py", 2, sev="MINOR", conf=0.9, summary="wrong factor"),
    ]
    full = _plan(pipe, llm)
    assert len(full.inline) == 4 and not full.summary
    capped = _plan(pipe, llm, max_inline=2)
    assert len(capped.inline) == 2
    assert {c.line for c in capped.inline if c.file == "app.py"} == {4, 5}  # CRITICAL, top MAJOR
    over = [i for g in capped.summary for i in g.items]
    assert {i.reason for i in over} == {"inline-cap"} and len(over) == 2
    zero = _plan(pipe, llm, max_inline=0)  # secret-scan CRITICAL is exempt from the cap
    assert [(c.file, c.line) for c in zero.inline] == [("app.py", 4)]
    assert zero.counts.summary_only == 3
    assert MAX_INLINE == 25
    _check_counts(capped)


def test_min_severity_and_confidence_filters_counted(pipe: Pipeline) -> None:
    llm = [
        _f("calc.py", 6, sev="MAJOR", conf=0.9, summary="keep"),
        _f("util.py", 2, sev="INFO", conf=0.9, summary="too minor"),
        _f("util.py", 2, sev="MAJOR", conf=0.2, cat="style", summary="unsure"),
        _f("util.py", 2, sev="MAJOR", conf=0.7, cat="performance", defaulted=True, summary="guess"),
    ]
    r = _plan(pipe, llm, config=RepoConfig(min_severity="MINOR"))
    c = r.counts
    assert (c.filtered_severity, c.filtered_confidence, c.filtered) == (1, 2, 3)
    assert {x.line for x in r.inline if x.file != "app.py"} == {6}
    assert _plan(pipe, llm, min_confidence=0.1).counts.filtered_confidence == 0  # configurable
    cats = _plan(pipe, llm, config=RepoConfig(categories=["correctness", "security"]))
    assert cats.counts.filtered_category == 2
    _check_counts(r)


def test_failures_skips_notices_and_unmatched_tests(pipe: Pipeline) -> None:
    trunc = chunk_diff(pipe.diff, max_chunks=1)
    assert trunc.truncated
    budget = degraded_notice(BudgetExceeded("daily cap"))
    cors = [
        correlate(pipe.static, pipe.tests, pipe.cov, trunc.chunks),
        correlate([_f("calc.py", 2)], pipe.tests, pipe.cov, trunc.chunks),  # claims test_calc
    ]
    r = aggregate(
        cors,
        trunc,
        failed_categories=[FailedCategory(category="security", reason="bad json", attempts=2)],
        failed_chunks=[FailedChunk(chunk_id="calc.py#0", reason="timeout", attempts=3)],
        notices=[".pr-review.yml ignored (bad); using defaults.", budget],
    )
    assert [(c.category, c.attempts) for c in r.failed_categories] == [("security", 2)]
    assert [c.chunk_id for c in r.failed_chunks] == ["calc.py#0"]
    assert ("uv.lock", "lock file") in {(s.path, s.reason) for s in r.skipped_files}
    assert r.truncated and r.omitted_chunks == trunc.omitted_chunks > 0
    assert budget in r.notices and any("truncated" in n for n in r.notices)
    assert [u.test_id for u in r.unmatched_tests] == ["test_util::test_twice"]


def test_findings_collapsed_by_correlate_are_counted(pipe: Pipeline) -> None:
    llm = [_f("util.py", 2, summary=f"dup {i}", conf=0.9 - i / 10) for i in range(5)]
    r = _plan(pipe, llm)
    assert r.counts.total == len(pipe.static) + 5
    assert r.counts.deduped == 4 and r.counts.balanced()
    with pytest.raises(ValueError):
        aggregate([correlate(llm)], pipe.plan, received=0)


def test_output_is_sanitised(pipe: Pipeline) -> None:
    llm = [
        _f(
            "util.py",
            2,
            sev="MAJOR",
            summary="bad <b>factor</b>",
            rationale="see <img src=x onerror=1> [docs](https://evil.example/x) https://evil.example",
            suggestion="x * 2 <script>alert(1)</script>",
        )
    ]
    (c,) = [c for c in _plan(pipe, llm).inline if c.file == "util.py"]
    assert "<" not in c.body and "evil.example" not in c.body and "Suggested change" in c.body


def test_deterministic_and_order_independent(pipe: Pipeline) -> None:
    llm = [
        _f("app.py", 4, sev="MAJOR", cat="security", summary="key"),
        _f("calc.py", 6, sev="MAJOR", conf=0.9, summary="a"),
        _f("util.py", 2, sev="MINOR", conf=0.9, summary="b"),
        _f("calc.py", 1, sev="MINOR", summary="c"),
    ]
    first = _plan(pipe, llm, max_inline=3)
    assert _plan(pipe, llm, max_inline=3) == first
    assert _plan(pipe, llm, max_inline=3).model_dump_json() == first.model_dump_json()
    for perm in itertools.islice(itertools.permutations(llm), 12):
        assert _plan(pipe, list(perm), max_inline=3) == first
    assert ReviewPlan.model_validate_json(first.model_dump_json()) == first


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ollama() -> Provider:
    try:
        urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3).read()
    except OSError as e:
        pytest.skip(f"Ollama unreachable at {OLLAMA}: {e}")
    s = Settings.model_construct(
        llm_review_model=MODEL,
        llm_triage_model=MODEL,
        llm_fallback_models=[],
        ollama_api_base=OLLAMA,
    )
    return Provider(s, rng=lambda: 0.0)


@pytest.mark.llm
@pytest.mark.anyio
async def test_real_ollama_findingset_aggregates(pipe: Pipeline) -> None:
    provider = _ollama()
    chunk = next(c for c in pipe.plan.chunks if c.path == "app.py")
    res = await analyze_chunk(provider, chunk, ["correctness", "security"], None)
    assert isinstance(res, FindingSet), res
    cors = [
        correlate(pipe.static, pipe.tests, pipe.cov, pipe.plan.chunks),
        correlate(list(res.findings), pipe.tests, pipe.cov, pipe.plan.chunks),
    ]
    r = aggregate(
        cors,
        pipe.plan,
        received=len(pipe.static) + len(res.findings),
        failed_categories=res.failed_categories,
    )
    assert ReviewPlan.model_validate_json(r.model_dump_json()) == r
    assert any(c.file == "app.py" and c.line == 4 and c.severity == "CRITICAL" for c in r.inline)
    assert r.counts.total == len(pipe.static) + len(res.findings)
    _check_counts(r)
    print(
        f"\nreal model findings: {[(f.severity, f.category, f.line_start) for f in res.findings]}"
    )
