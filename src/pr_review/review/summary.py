"""Render a ReviewPlan plus test/coverage results as one GitHub Markdown summary (FR-20, NFR-10).

Pure and deterministic. Every dynamic string is clipped, flattened and escaped so it cannot inject
links, images, HTML, mentions or issue auto-links, or close a `<details>` block. Output is capped
below GitHub's 65,536 character comment limit; what does not fit is dropped and counted.
"""

import html
import re
from collections.abc import Sequence

from pr_review.core.repo_config import SEVERITY_ORDER, Severity
from pr_review.parsers.chunker import Chunk
from pr_review.parsers.coverage import CoverageResult
from pr_review.parsers.tests import TestRunResult
from pr_review.review.aggregate import ReviewPlan, SummaryItem

MAX_CHARS = 60_000
_RESERVE = 600  # room for closing a `<details>` and the truncation note
COLLAPSE_AFTER = 10
ZWSP = "​"
_MD = re.compile(r"([\\`*_{}\[\]()#+!|~<>=-])")


def esc(text: object, limit: int = 300) -> str:
    """Make arbitrary text inert in GitHub Markdown (clip, flatten, HTML-escape, neutralise)."""
    s = " ".join(str(text).split())
    s = s[:limit] + ("..." if len(s) > limit else "")
    s = html.escape(s, quote=False)
    s = _MD.sub(r"\\\1", s)
    s = (
        s.replace("@", "@" + ZWSP)
        .replace("://", ":" + ZWSP + "//")
        .replace("www.", "www" + ZWSP + ".")
    )
    return re.sub(r"(\\#|#)(\d)", lambda m: m.group(1) + ZWSP + m.group(2), s)


class _Out:
    def __init__(self, cap: int) -> None:
        self.lines: list[str] = []
        self.used = 0
        self.cap = cap
        self.dropped = 0

    def add(self, line: str) -> bool:
        if self.used + len(line) + 1 > self.cap - _RESERVE:
            self.dropped += bool(line)
            return False
        self.lines.append(line)
        self.used += len(line) + 1
        return True

    def block(self, title: str, items: Sequence[str]) -> None:
        """A heading and a bullet list; collapsed into `<details>` when long."""
        if not items:
            return
        fold = len(items) > COLLAPSE_AFTER
        if fold:
            if not self.add(f"<details><summary>{title} ({len(items)})</summary>"):
                return
            self.lines.append("")
        else:
            self.add(f"**{title}**")
            self.add("")
        for i, it in enumerate(items):
            if not self.add(f"- {it}"):
                self.dropped += len(items) - i - 1
                break
        if fold:
            self.lines += ["", "</details>"]  # forced: the reserve guarantees room
        self.add("")

    def text(self) -> str:
        if self.dropped:
            self.lines.append(
                f"_Output truncated: {self.dropped} item(s) omitted to fit GitHub's size limit._"
            )
        return "\n".join(self.lines).rstrip() + "\n"


def _count(plan: ReviewPlan, sev: Severity) -> tuple[int, int]:
    inline = sum(c.severity == sev for c in plan.inline)
    over = sum(len(g.items) for g in plan.summary if g.severity == sev)
    return inline, over


def _item(i: SummaryItem) -> str:
    span = str(i.line_start) if i.line_end == i.line_start else f"{i.line_start}-{i.line_end}"
    return f"{esc(i.file, 512)}:{span} ({esc(i.category)}) {esc(i.text)}"


def _tests(tests: TestRunResult | None, status: str) -> str:
    if tests is None:
        return f"**Tests:** {esc(status)}"
    return (
        f"**Tests:** {tests.passed} passed, {tests.failed} failed, {tests.skipped} skipped, "
        f"{tests.errored} errored in {tests.duration:.1f}s ({esc(tests.framework, 40)})"
    )


def _coverage(cov: CoverageResult | None, chunks: Sequence[Chunk]) -> str:
    if cov is None:
        return "**Coverage of changed lines:** not available"
    changed: dict[str, set[int]] = {}
    for c in chunks:
        changed.setdefault(c.path, set()).update(c.changed_new)
    hit = miss = 0
    for f in cov.files:
        want = changed.get(f.path, set())
        hit += sum(1 for a, b in f.covered for n in range(a, b + 1) if n in want)
        miss += sum(1 for a, b in f.uncovered for n in range(a, b + 1) if n in want)
    if hit + miss == 0:
        return "**Coverage of changed lines:** no measurable changed lines"
    return (
        f"**Coverage of changed lines:** {hit} covered, {miss} uncovered "
        f"({100 * hit / (hit + miss):.0f}%)"
    )


def render_summary(
    plan: ReviewPlan,
    tests: TestRunResult | None = None,
    coverage: CoverageResult | None = None,
    chunks: Sequence[Chunk] = (),
    *,
    tests_status: str = "no tests detected",
    max_chars: int = MAX_CHARS,
) -> str:
    """`tests_status` explains a missing result, e.g. "tests disabled by repo config" or
    "test run failed: timeout"."""
    out = _Out(min(max_chars, MAX_CHARS))
    per = {s: _count(plan, s) for s in SEVERITY_ORDER}
    total = sum(a + b for a, b in per.values())
    incomplete = bool(plan.failed_categories or plan.failed_chunks or plan.truncated)
    if total:
        parts = [f"{a + b} {s.lower()}" for s, (a, b) in per.items() if a + b]
        verdict = f"{total} finding(s): " + ", ".join(parts)
    elif incomplete:
        verdict = "No issues found in the analysed portion; analysis was incomplete."
    else:
        verdict = "No issues found."
    out.add("## PR Review Agent")
    out.add("")
    out.add(f"**{verdict}**")
    out.add("")
    if total:
        out.add("| Severity | Inline | Summary only | Total |")
        out.add("| --- | ---: | ---: | ---: |")
        for s, (a, b) in per.items():
            out.add(f"| {s} | {a} | {b} | {a + b} |")
        out.add("")
    out.block("Notices", [esc(n, 600) for n in plan.notices])
    out.add(_tests(tests, tests_status))
    out.add("")
    out.add(_coverage(coverage, chunks))
    out.add("")
    out.block(
        "Failed analysis categories",
        [
            f"{esc(c.category)}: {esc(c.reason)} (after {c.attempts} attempt(s))"
            for c in plan.failed_categories
        ],
    )
    out.block(
        "Chunks that could not be analysed",
        [
            f"{esc(c.chunk_id, 512)}: {esc(c.reason)} ({c.attempts} attempt(s))"
            for c in plan.failed_chunks
        ],
    )
    out.block(
        "Failing tests not linked to a finding",
        [f"{esc(u.test_id, 512)}: {esc(u.message)}" for u in plan.unmatched_tests],
    )
    out.block(
        "Skipped files",
        [f"{esc(s.path, 512)}: {esc(s.reason)}" for s in plan.skipped_files],
    )
    if plan.omitted_chunks:
        out.add(f"{plan.omitted_chunks} chunk(s) were not reviewed because the diff was too large.")
        out.add("")
    for reason, title in (
        ("off-diff", "Findings outside the changed lines"),
        ("inline-cap", "Findings over the inline comment cap"),
    ):
        for g in plan.summary:
            out.block(
                f"{title}: {g.severity}",
                [_item(i) for i in g.items if i.reason == reason],
            )
    return out.text()
