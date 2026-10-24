"""Summary rendering on real artifacts: git diff -> chunk_diff -> static_checks, real pytest +
coverage through the real parsers, real correlate/aggregate; plus a real Ollama FindingSet."""

import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

from pr_review.core.repo_config import Category, Severity
from pr_review.core.settings import Settings
from pr_review.llm.analyze import analyze_chunk
from pr_review.llm.budget import degraded_notice
from pr_review.llm.provider import BudgetExceeded, Provider
from pr_review.parsers.chunker import ChunkPlan, chunk_diff
from pr_review.parsers.coverage import CoverageResult, parse_coverage_json
from pr_review.parsers.tests import TestRunResult, parse_junit
from pr_review.review.aggregate import ReviewPlan, aggregate
from pr_review.review.correlate import correlate
from pr_review.review.models import FailedCategory, FailedChunk, Finding, FindingSet
from pr_review.review.static_checks import static_findings
from pr_review.review.summary import MAX_CHARS, esc, render_summary

OLLAMA = os.environ.get("PR_REVIEW_TEST_OLLAMA", "http://localhost:11434")
MODEL = os.environ.get("PR_REVIEW_TEST_LLM_MODEL", "ollama/qwen2.5-coder:1.5b")

BASE = {
    "calc.py": "def divide(a, b):\n    return a / b if b else 0\n",
    "util.py": "def twice(x):\n    return x * 2\n",
    "app.py": 'import os\n\nNAME = "app"\n',
    "uv.lock": "version = 1\n",
    "test_calc.py": (
        "from calc import divide\n\n\ndef test_divide():\n    assert divide(1, 0) == 0\n"
    ),
    "test_util.py": "from util import twice\n\n\ndef test_twice():\n    assert twice(2) == 4\n",
}
HEAD = {
    "calc.py": "def divide(a, b):\n    return a / b\n",
    "util.py": "def twice(x):\n    return x * 3\n",
    "app.py": 'import os\n\nNAME = "app"\nKEY = "AKIAZ7QW3XK9PLM2RT5V"\nos.system("ls " + NAME)\n',
    "uv.lock": "version = 2\n",
}


@dataclass
class Pipeline:
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
    root = tmp_path_factory.mktemp("summary")
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
    py = sys.executable
    _run(
        root,
        py,
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
    _run(root, py, "-m", "coverage", "json", "-o", str(root / "c.json"))
    tests = parse_junit((root / "j.xml").read_text(), str(root))
    cov = parse_coverage_json((root / "c.json").read_text(), str(root))
    assert isinstance(tests, TestRunResult) and isinstance(cov, CoverageResult)
    return Pipeline(chunk_diff(diff), tests, cov, static_findings(diff))


def _f(
    path: str, line: int, summary: str, sev: Severity = "MAJOR", cat: Category = "correctness"
) -> Finding:
    return Finding(
        severity=sev,
        category=cat,
        file_path=path,
        line_start=line,
        line_end=line,
        summary=summary,
        rationale="because",
        confidence=0.9,
    )


def _plan(p: Pipeline, llm: list[Finding] = (), **kw: object) -> ReviewPlan:  # type: ignore[assignment]
    cors = [
        correlate(p.static, p.tests, p.cov, p.plan.chunks),
        correlate(llm, p.tests, p.cov, p.plan.chunks),
    ]
    return aggregate(cors, p.plan, received=len(p.static) + len(llm), **kw)  # type: ignore[arg-type]


def _render(p: Pipeline, r: ReviewPlan, **kw: object) -> str:
    return render_summary(r, p.tests, p.cov, p.plan.chunks, **kw)  # type: ignore[arg-type]


def test_realistic_plan_renders_all_sections(pipe: Pipeline) -> None:
    llm = [_f("app.py", 3, "unchanged def"), _f("util.py", 2, "wrong factor", "MINOR")]
    r = _plan(
        pipe,
        llm,
        max_inline=2,
        failed_categories=[FailedCategory(category="security", reason="bad json", attempts=2)],
        failed_chunks=[FailedChunk(chunk_id="calc.py#0", reason="timeout", attempts=3)],
        notices=[
            ".pr-review.yml ignored (bad); using defaults.",
            degraded_notice(BudgetExceeded("daily cap")),
        ],
    )
    out = _render(pipe, r)
    assert out.startswith("## PR Review Agent\n")
    for part in (
        "| CRITICAL |",
        "**Tests:** 0 passed, 2 failed",
        "**Coverage of changed lines:**",
        "Notices",
        "daily LLM cost cap",
        esc("pr-review.yml ignored"),
        "Failed analysis categories",
        "Chunks that could not be analysed",
        "Failing tests not linked",
        "Skipped files",
        "uv.lock",
        "lock file",
        "Findings outside the changed lines: MAJOR",
        "app.py:3",
        "Findings over the inline comment cap",
    ):
        assert part in out, part
    assert out.startswith("## PR Review Agent")
    assert "%)" in out and "uncovered" in out
    assert out == _render(pipe, r)  # deterministic


def test_empty_plan_and_test_status_variants(pipe: Pipeline) -> None:
    empty = aggregate(
        [],
        ChunkPlan((), (), False, 0, ()),
    )  # type: ignore[call-arg]
    out = render_summary(empty, pipe.tests)
    assert (
        "No issues found." in out and "**Tests:** 0 passed, 2 failed, 0 skipped, 0 errored" in out
    )
    assert "| Severity" not in out
    for status in (
        "tests disabled by repo config",
        "no tests detected",
        "test run failed: timeout",
    ):
        assert f"**Tests:** {esc(status)}" in render_summary(empty, None, tests_status=status)
    assert "not available" in render_summary(empty)


def test_hostile_strings_are_neutralised(pipe: Pipeline) -> None:
    evil = "](http://x) <img src=x> @someone fixes #123 </details> ![i](http://y) www.evil.com"
    llm = [_f("calc.py", 1, evil)]
    r = _plan(pipe, llm, notices=[evil])
    out = _render(pipe, r)
    assert "<img" not in out and "</details>" not in out.replace("\n</details>", "")
    assert (
        "@someone" not in out
        and "#123" not in out
        and "http://" not in out
        and "www.evil" not in out
    )
    assert not re.search(r"(?<!\\)\]\(", out) and not re.search(r"(?<!\\)!\[", out)
    assert "&lt;img" in out
    assert esc("a\nb\r\n|c|") == "a b \\|c\\|"
    assert esc("x" * 1000, 50).endswith("...")
    assert not re.search(r"(?<!\\)\[", esc("[a](b)"))


def test_size_cap_with_thousands_of_items(pipe: Pipeline) -> None:
    llm = [_f("calc.py", 1, f"issue {i} " + "w" * 250, cat="style") for i in range(1)]
    r = _plan(pipe, llm)
    big = r.model_copy(
        update={
            "summary": tuple(
                g.__class__(
                    severity=s,
                    items=tuple(
                        g.items[0].model_copy(
                            update={"file": f"src/f{i}.py", "text": f"summary {i} " + "<b>" * 60}
                        )
                        for i in range(3000)
                    ),
                )
                for s, g in [("MAJOR", r.summary[0])]
            )
        }
    )
    out = _render(pipe, big)
    assert len(out) <= MAX_CHARS < 65_536
    assert "Output truncated:" in out and "omitted to fit GitHub's size limit" in out
    assert out.count("<details>") == out.count("</details>")
    assert "**Tests:**" in out  # high-priority sections survive
    small = _render(pipe, big, max_chars=5_000)
    assert len(small) <= 5_000 and small.count("<details>") == small.count("</details>")


def test_degraded_budget_notice_shown(pipe: Pipeline) -> None:
    n = degraded_notice(BudgetExceeded("job budget"))
    out = _render(pipe, _plan(pipe, notices=[n]))
    assert esc(n, 600) in out and "LLM budget" in out


def test_truncated_review_notice(pipe: Pipeline) -> None:
    trunc = chunk_diff(
        "".join(
            f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n@@ -0,0 +1 @@\n+x\n"
            for i in range(5)
        ),
        max_chunks=2,
    )
    out = render_summary(aggregate([], trunc))
    assert trunc.truncated and "Review truncated" in out and "not reviewed" in out
    assert "No issues found in the analysed portion" in out


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.llm
@pytest.mark.anyio
async def test_real_ollama_plan_renders(pipe: Pipeline) -> None:
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
    chunk = next(c for c in pipe.plan.chunks if c.path == "app.py")
    res = await analyze_chunk(
        Provider(s, rng=lambda: 0.0), chunk, ["correctness", "security"], None
    )
    assert isinstance(res, FindingSet), res
    r = _plan(pipe, list(res.findings), failed_categories=res.failed_categories)
    out = _render(pipe, r)
    assert len(out) <= MAX_CHARS and "critical" in out
    assert out.count("<details>") == out.count("</details>") and out == _render(pipe, r)
