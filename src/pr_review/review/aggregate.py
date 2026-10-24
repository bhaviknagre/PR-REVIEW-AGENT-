"""Aggregate correlated findings into one typed ReviewPlan (FR-17, AC-13). Pure and deterministic.

Pipeline: category/severity/confidence filters (counted) -> output sanitising via the guard ->
dedupe by (file, overlapping range, category) -> inline eligibility -> comment cap.
- precedence: deterministic sources (`[secret-scan]`, `[pattern]`, `[lint:*]`), then
  test-confirmed, then severity, then effective confidence. The winner keeps its text, gains the
  union of tags and the highest severity of the group.
- inline: anchored on a changed RIGHT-side line of the diff; off-diff, deleted-line and skipped-file
  findings go to the summary. Above the cap the lowest-ranked inline comments overflow to the
  summary; anchorable secret-scan CRITICALs are never dropped by the cap.
"""

import logging
from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pr_review.core.repo_config import SEVERITY_ORDER, Category, RepoConfig, Severity
from pr_review.llm.guard import check_output
from pr_review.parsers.chunker import Chunk, ChunkPlan, Skip
from pr_review.review.correlate import (
    DETERMINISTIC,
    CorrelatedFinding,
    Correlation,
    UnmatchedTest,
    _confidence,
)
from pr_review.review.models import FailedCategory, FailedChunk, Finding

log = logging.getLogger(__name__)
MIN_CONFIDENCE = 0.4  # effective confidence (defaulted ones are discounted by correlate's factor)
MAX_INLINE = 25
_SECRET = "[secret-scan]"


class InlineComment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str
    line: int = Field(ge=1)
    side: Literal["RIGHT"] = "RIGHT"
    severity: Severity
    category: Category
    body: str


class SummaryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    severity: Severity
    category: Category
    text: str
    reason: Literal["off-diff", "inline-cap"]
    tags: tuple[str, ...] = ()


class SummaryGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: Severity
    items: tuple[SummaryItem, ...]


class Counts(BaseModel):
    """total == inline + summary_only + filtered + deduped + dropped_unsafe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int
    inline: int
    summary_only: int
    filtered: int
    filtered_category: int
    filtered_severity: int
    filtered_confidence: int
    deduped: int
    dropped_unsafe: int

    def balanced(self) -> bool:
        out = self.inline + self.summary_only + self.filtered + self.deduped + self.dropped_unsafe
        return self.total == out


class ReviewPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    inline: tuple[InlineComment, ...]
    summary: tuple[SummaryGroup, ...]
    failed_categories: tuple[FailedCategory, ...]
    failed_chunks: tuple[FailedChunk, ...]
    unmatched_tests: tuple[UnmatchedTest, ...]
    skipped_files: tuple[Skip, ...]
    notices: tuple[str, ...]
    truncated: bool
    omitted_chunks: int
    counts: Counts


def _deterministic(f: Finding) -> bool:
    return DETERMINISTIC.match(f.summary) is not None


def _key(cf: CorrelatedFinding) -> tuple[object, ...]:
    f = cf.finding
    return (
        not _deterministic(f),
        "test-confirmed" not in cf.tags,
        SEVERITY_ORDER.index(f.severity),
        -_confidence(f),
        f.file_path,
        f.line_start,
        f.line_end,
        f.summary,
    )


def _rank(cf: CorrelatedFinding) -> tuple[object, ...]:
    f = cf.finding
    return (SEVERITY_ORDER.index(f.severity), "test-confirmed" not in cf.tags, -_confidence(f)) + (
        f.file_path,
        f.line_start,
        f.summary,
    )


def _sanitise(cf: CorrelatedFinding, chunks: dict[str, Chunk]) -> CorrelatedFinding | None:
    f = cf.finding
    chunk = chunks.get(f.file_path) or Chunk(f.file_path, f.file_path, (), {}, {}, "", 0)
    checked = check_output([f], chunk).findings
    return CorrelatedFinding(checked[0], cf.tags, cf.original, cf.raised_from) if checked else None


def _dedupe(items: Sequence[CorrelatedFinding]) -> tuple[list[CorrelatedFinding], int]:
    kept: list[CorrelatedFinding] = []
    dups = 0
    for cf in sorted(items, key=_key):
        f = cf.finding
        for i, k in enumerate(kept):
            g = k.finding
            if (
                g.file_path == f.file_path
                and g.category == f.category
                and g.line_start <= f.line_end
                and f.line_start <= g.line_end
            ):
                sev = min(g.severity, f.severity, key=SEVERITY_ORDER.index)
                kept[i] = CorrelatedFinding(
                    g.model_copy(update={"severity": sev}),
                    tuple(dict.fromkeys(k.tags + cf.tags)),
                    k.original,
                    k.raised_from,
                )
                dups += 1
                break
        else:
            kept.append(cf)
    return kept, dups


def _anchor(f: Finding, changed: Iterable[int]) -> int | None:
    return min((n for n in changed if f.line_start <= n <= f.line_end), default=None)


def _body(cf: CorrelatedFinding) -> str:
    f = cf.finding
    parts = [f"**{f.severity}** ({f.category}): {f.summary}", f.rationale]
    if f.suggestion:
        parts.append(f"Suggested change: {f.suggestion}")
    if cf.tags:
        parts.append("Signals: " + ", ".join(cf.tags))
    return "\n\n".join(parts)


def _item(cf: CorrelatedFinding, reason: Literal["off-diff", "inline-cap"]) -> SummaryItem:
    f = cf.finding
    return SummaryItem(
        file=f.file_path,
        line_start=f.line_start,
        line_end=f.line_end,
        severity=f.severity,
        category=f.category,
        text=f.summary,
        reason=reason,
        tags=cf.tags,
    )


def aggregate(
    correlations: Sequence[Correlation],
    plan: ChunkPlan,
    *,
    config: RepoConfig | None = None,
    failed_categories: Sequence[FailedCategory] = (),
    failed_chunks: Sequence[FailedChunk] = (),
    notices: Sequence[str] = (),
    received: int | None = None,
    min_confidence: float = MIN_CONFIDENCE,
    max_inline: int = MAX_INLINE,
) -> ReviewPlan:
    cfg = config or RepoConfig()
    floor = SEVERITY_ORDER.index(cfg.min_severity)
    chunks: dict[str, Chunk] = {}
    changed: dict[str, set[int]] = {}
    for c in plan.chunks:
        chunks.setdefault(c.path, c)
        changed.setdefault(c.path, set()).update(c.changed_new)

    total = by_cat = by_sev = by_conf = unsafe = 0
    live: list[CorrelatedFinding] = []
    for cf in (x for c in correlations for x in c.findings):
        total += 1
        f = cf.finding
        if f.category not in cfg.categories:
            by_cat += 1
        elif SEVERITY_ORDER.index(f.severity) > floor:
            by_sev += 1
        elif _confidence(f) < min_confidence:
            by_conf += 1
        elif (clean := _sanitise(cf, chunks)) is None:
            unsafe += 1
        else:
            live.append(clean)

    # correlate collapses overlapping same-category findings itself; `received` (findings fed to
    # correlate) lets those drops be counted as deduped instead of vanishing.
    if received is not None and received < total:
        raise ValueError("received is smaller than the number of correlated findings")
    collapsed = (received or total) - total
    total += collapsed
    merged, deduped = _dedupe(live)
    deduped += collapsed
    eligible: list[tuple[CorrelatedFinding, int]] = []
    off: list[CorrelatedFinding] = []
    for cf in merged:
        line = _anchor(cf.finding, changed.get(cf.finding.file_path, ()))
        if line is None:
            off.append(cf)
        else:
            eligible.append((cf, line))

    eligible.sort(key=lambda p: _rank(p[0]))
    forced = [
        p
        for p in eligible
        if p[0].finding.summary.startswith(_SECRET) and p[0].finding.severity == "CRITICAL"
    ]
    room = max(max_inline - len(forced), 0)
    rest = [p for p in eligible if p not in forced]
    inline = forced + rest[:room]
    overflow = [p[0] for p in rest[room:]]

    comments = tuple(
        sorted(
            (
                InlineComment(
                    file=cf.finding.file_path,
                    line=line,
                    severity=cf.finding.severity,
                    category=cf.finding.category,
                    body=_body(cf),
                )
                for cf, line in inline
            ),
            key=lambda c: (c.file, c.line, SEVERITY_ORDER.index(c.severity), c.body),
        )
    )
    items = sorted(
        [_item(cf, "off-diff") for cf in off] + [_item(cf, "inline-cap") for cf in overflow],
        key=lambda i: (i.file, i.line_start, i.line_end, i.category, i.text),
    )
    groups = tuple(
        SummaryGroup(severity=s, items=tuple(i for i in items if i.severity == s))
        for s in SEVERITY_ORDER
        if any(i.severity == s for i in items)
    )

    unmatched_ids = (
        set.intersection(*({u.test_id for u in c.unmatched_tests} for c in correlations))
        if correlations
        else set()
    )
    unmatched = {
        u.test_id: u for c in correlations for u in c.unmatched_tests if u.test_id in unmatched_ids
    }
    extra = list(plan.warnings)
    if plan.truncated:
        extra.append(
            f"Review truncated: {plan.omitted_chunks} chunk(s) beyond the size limit were skipped."
        )
    counts = Counts(
        total=total,
        inline=len(comments),
        summary_only=len(items),
        filtered=by_cat + by_sev + by_conf,
        filtered_category=by_cat,
        filtered_severity=by_sev,
        filtered_confidence=by_conf,
        deduped=deduped,
        dropped_unsafe=unsafe,
    )
    if not counts.balanced():
        log.error("aggregate lost findings: %s", counts)
    return ReviewPlan(
        inline=comments,
        summary=groups,
        failed_categories=tuple(
            sorted(set(failed_categories), key=lambda c: (c.category, c.reason, c.attempts))
        ),
        failed_chunks=tuple(sorted(failed_chunks, key=lambda c: c.chunk_id)),
        unmatched_tests=tuple(unmatched[k] for k in sorted(unmatched)),
        skipped_files=tuple(sorted(plan.skipped, key=lambda s: (s.path, s.reason))),
        notices=tuple(dict.fromkeys([*notices, *extra])),
        truncated=plan.truncated,
        omitted_chunks=plan.omitted_chunks,
        counts=counts,
    )
