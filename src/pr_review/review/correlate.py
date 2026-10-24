"""Correlate findings with failing tests and uncovered lines (FR-16, AC-12). Pure and deterministic.

Rules (no LLM involvement):
- test-confirmed: a failing test's file:line (or a traceback ``path:line`` in its message, e.g.
  ``calc.py:5: in divide``) falls inside the finding's file and line range. Only this raises.
- test-related: no exact location, but the failing test's module maps to the finding's source file
  (``test_calc`` / ``calc_test`` -> ``calc.py``). Tag and ranking only; never changes severity.
- uncovered: at least one changed line in the finding's range is reported uncovered.
- raise: test-confirmed -> at least MAJOR; uncovered + security -> one level up, capped at
  CRITICAL. Severity is never lowered.
- narrowing: a range wider than NARROW_OVER lines shrinks to the failing-test line if inside the
  range, else the first changed line in range; the original finding is kept in the wrapper.
- ranking: severity, test-confirmed first, confidence (halved when defaulted), file, line.
"""

import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass

from pr_review.core.repo_config import SEVERITY_ORDER, Severity
from pr_review.parsers.chunker import Chunk
from pr_review.parsers.coverage import CoverageResult
from pr_review.parsers.tests import TestFailure, TestRunResult, clip
from pr_review.review.models import Finding

# Static checks prefix their summaries; such findings are certain and outrank any LLM overlap.
DETERMINISTIC = re.compile(r"^\[(?:secret-scan|pattern|lint:[^\]]*)\]")

NARROW_OVER = 3
DEFAULT_DISCOUNT = 0.5
SUMMARY_MESSAGE = 120
_TIER = {"test-confirmed": 0, "test-related": 1}
_LOC = re.compile(r"([\w./-]+\.\w+):(\d+)")


@dataclass(frozen=True)
class CorrelatedFinding:
    finding: Finding  # after raise/narrowing
    tags: tuple[str, ...]  # "test-confirmed", "uncovered"
    original: Finding
    raised_from: Severity | None = None


@dataclass(frozen=True)
class UnmatchedTest:
    test_id: str
    file: str | None
    line: int | None
    message: str


@dataclass(frozen=True)
class Correlation:
    findings: tuple[CorrelatedFinding, ...]
    unmatched_tests: tuple[UnmatchedTest, ...]


def _stems(failure: TestFailure) -> set[str]:
    head = failure.test_id.split("::")[0]
    parts = re.split(r"[./]", head)
    if failure.file:
        parts.append(posixpath.splitext(posixpath.basename(failure.file))[0])
    return {p.removeprefix("test_").removesuffix("_test") for p in parts if p}


def _locations(failure: TestFailure) -> list[tuple[str, int]]:
    """Parsed traceback frames (innermost first); message text-scan only as a fallback."""
    locs = [(x.file, x.line) for x in failure.locations]
    if not locs:
        locs = [(m[1], int(m[2])) for m in _LOC.finditer(failure.message)]
    if failure.file and failure.line:
        locs.append((failure.file, failure.line))
    return locs


def _test_line(f: Finding, failure: TestFailure) -> int | None:
    for path, line in _locations(failure):
        if f.file_path in (path, path.lstrip("./")) and f.line_start <= line <= f.line_end:
            return line
    return None


def _related(f: Finding, failure: TestFailure) -> bool:
    return posixpath.splitext(posixpath.basename(f.file_path))[0] in _stems(failure)


def _short(message: str) -> str:
    lines = message.split("\n")
    return next((x[1:].strip() for x in lines if x.startswith("E ")), lines[0])


def _raise(sev: Severity, steps: int) -> Severity:
    return SEVERITY_ORDER[max(SEVERITY_ORDER.index(sev) - steps, 0)]


def _narrow(f: Finding, test_line: int | None, changed: Sequence[int]) -> Finding:
    if f.line_end - f.line_start + 1 <= NARROW_OVER:
        return f
    target = test_line or next(
        (n for n in sorted(changed) if f.line_start <= n <= f.line_end), None
    )
    if target is None:
        return f
    return f.model_copy(update={"line_start": target, "line_end": target})


def _confidence(f: Finding) -> float:
    return f.confidence * DEFAULT_DISCOUNT if f.confidence_defaulted else f.confidence


def correlate(
    findings: Sequence[Finding],
    tests: TestRunResult | None = None,
    coverage: CoverageResult | None = None,
    chunks: Sequence[Chunk] = (),
) -> Correlation:
    changed: dict[str, list[int]] = {}
    for c in chunks:
        changed.setdefault(c.path, []).extend(c.changed_new)
    uncovered = {fc.path: fc.uncovered for fc in coverage.files} if coverage else {}
    failures = tests.failures if tests else ()
    matched: set[int] = set()
    out: list[tuple[CorrelatedFinding, int]] = []
    for f in findings:
        tags: list[str] = []
        hits = [(i, x) for i, x in enumerate(failures) if _test_line(f, x) is not None]
        related = [i for i, x in enumerate(failures) if _related(f, x)]
        matched.update(i for i, _ in hits)
        matched.update(related)
        lines = changed.get(f.file_path, [])
        in_range = [n for n in lines if f.line_start <= n <= f.line_end]
        if f.file_path in uncovered and any(
            lo <= n <= hi for n in in_range for lo, hi in uncovered[f.file_path]
        ):
            tags.append("uncovered")
        sev = f.severity
        if hits:
            tags.insert(0, "test-confirmed")
            sev = min(sev, "MAJOR", key=SEVERITY_ORDER.index)
        elif related:
            tags.insert(0, "test-related")
        if "uncovered" in tags and f.category == "security":
            sev = _raise(sev, 1)
        test_line = next((t for _, x in hits if (t := _test_line(f, x)) is not None), None)
        new = _narrow(f.model_copy(update={"severity": sev}), test_line, lines)
        raised = f.severity if sev != f.severity else None
        out.append(
            (CorrelatedFinding(new, tuple(tags), f, raised), _TIER.get(tags[0], 2) if tags else 2)
        )

    out.sort(
        key=lambda p: (
            DETERMINISTIC.match(p[0].finding.summary) is None,
            SEVERITY_ORDER.index(p[0].finding.severity),
            p[1],
            -_confidence(p[0].finding),
            p[0].finding.file_path,
            p[0].finding.line_start,
            p[0].finding.summary,
        )
    )
    kept: list[CorrelatedFinding] = []
    for cf in out:
        a = cf[0].finding
        if not any(
            k.finding.file_path == a.file_path
            and k.finding.category == a.category
            and k.finding.line_start <= a.line_end
            and a.line_start <= k.finding.line_end
            for k in kept
        ):
            kept.append(cf[0])
    unmatched = tuple(
        UnmatchedTest(x.test_id, x.file, x.line, clip(_short(x.message), SUMMARY_MESSAGE)[0])
        for i, x in enumerate(failures)
        if i not in matched
    )
    return Correlation(tuple(kept), unmatched)
