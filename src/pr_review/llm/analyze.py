"""Per-chunk LLM analysis (FR-14, FR-15): prompt, JSON extraction, validation, bounded re-ask.

Chunk text and context are untrusted and are only ever placed inside the delimited data blocks of
the prompt templates (the seam for the injection guard). Findings must anchor to the chunk's
changed lines so GitHub review comments can be placed.
"""

import json
import re
import uuid
from collections.abc import Iterator, Sequence
from importlib import resources
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from pr_review.core.redaction import redact
from pr_review.core.repo_config import SEVERITY_ORDER, Category, Severity
from pr_review.llm.guard import check_output, guard_messages, record_guard
from pr_review.llm.provider import Provider
from pr_review.parsers.chunker import Chunk
from pr_review.review.models import FailedCategory, FailedChunk, Finding, FindingSet
from pr_review.storage.events import EventRepository

PROMPT_VERSION = "v1"
MAX_ATTEMPTS = 3  # first ask + 2 re-asks, per category
MAX_FINDINGS = 20
MAX_REASON = 400
_SEV_ALIAS = {
    "HIGH": "MAJOR", "ERROR": "MAJOR", "MEDIUM": "MINOR", "LOW": "MINOR", "WARNING": "MINOR",
    "NOTE": "INFO", "INFORMATIONAL": "INFO",
}  # fmt: skip
_LIMITS = {"summary": 300, "rationale": 2000, "suggestion": 4000, "file_path": 512}


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    findings: list[Finding]


def load_prompt(name: str) -> str:
    return (resources.files("pr_review.llm") / "prompts" / f"{name}.{PROMPT_VERSION}.md").read_text(
        encoding="utf-8"
    )


def _lines(nums: Sequence[int]) -> str:
    """Compress 3,4,5,9 to '3-5, 9'."""
    runs: list[list[int]] = []
    for n in sorted(nums):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def build_messages(chunk: Chunk, category: Category) -> list[dict[str, str]]:
    user = (
        load_prompt(category)
        .replace("{{path}}", chunk.path)
        .replace("{{lines}}", _lines(list(chunk.changed_new)) or "none")
        .replace("{{context}}", "\n".join(chunk.context) or "(none)")
        .replace("{{diff}}", chunk.text)
    )
    return [
        {"role": "system", "content": load_prompt("system")},
        {"role": "user", "content": user},
    ]


def _candidates(text: str) -> Iterator[Any]:
    """Every JSON object/array embedded in text (handles code fences and leading prose)."""
    dec = json.JSONDecoder()
    i = 0
    while i < len(text):
        if text[i] in "{[":
            try:
                val, end = dec.raw_decode(text, i)
            except ValueError:
                i += 1
                continue
            yield val
            i = end
        else:
            i += 1


def _normalize(item: Any, category: Category) -> Any:
    """Clamp what a small model commonly gets slightly wrong; leave real defects to validation."""
    if not isinstance(item, dict):
        return item
    f = dict(item)
    if isinstance(f.get("severity"), str):
        sev = f["severity"].strip().upper()
        f["severity"] = _SEV_ALIAS.get(sev, sev)
    f["category"] = category  # one category is asked per call; the model's value is ignored
    c = f.get("confidence")
    if isinstance(c, int | float) and not isinstance(c, bool):
        f["confidence"] = min(1.0, max(0.0, float(c)))
    f["confidence_defaulted"] = "confidence" not in f  # overrides anything the model claimed
    f.setdefault("confidence", 0.5)  # small models often omit it; mid-scale beats a wasted re-ask
    if "line_end" not in f and "line_start" in f:
        f["line_end"] = f["line_start"]
    for k, n in _LIMITS.items():
        if isinstance(f.get(k), str):
            f[k] = f[k][:n]
    return f


_STR_FIELD = re.compile(r'^(\s*"(?:summary|rationale|suggestion)":\s*")(.*)("\s*,?\s*)$', re.M)


def _escape_inner_quotes(text: str) -> str:
    """Small models often leave bare double quotes inside one-line string fields."""

    def fix(m: re.Match[str]) -> str:
        body = re.sub(r'(?<!\\)"', lambda _: '\\"', m.group(2))
        return m.group(1) + body + m.group(3)

    return _STR_FIELD.sub(fix, text)


def _parse(text: str, category: Category) -> _Reply:
    err: Exception = ValueError("no JSON object found in the reply")
    for val in [*_candidates(text), *_candidates(_escape_inner_quotes(text))]:
        if isinstance(val, list):
            val = {"findings": val}
        if isinstance(val, dict) and isinstance(val.get("findings"), list):
            val = {
                **val,
                "findings": [_normalize(x, category) for x in val["findings"]][:MAX_FINDINGS],
            }
        try:
            return _Reply.model_validate(val)
        except ValidationError as exc:
            err = exc
    raise err


def _summary(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5]]
        return "; ".join(parts)[:MAX_REASON]
    return str(exc)[:MAX_REASON]


def _anchor(f: Finding, chunk: Chunk) -> Finding | None:
    """Keep only findings on this file whose start is a changed line; clamp the end to one too."""
    if f.file_path != chunk.path or f.line_start not in chunk.changed_new:
        return None
    end = max(n for n in chunk.changed_new if f.line_start <= n <= f.line_end)
    return f.model_copy(update={"line_end": end})


async def analyze_chunk(
    provider: Provider,
    chunk: Chunk,
    categories: Sequence[Category],
    job_id: uuid.UUID | None,
    *,
    min_severity: Severity = "INFO",
    events: EventRepository | None = None,
) -> FindingSet | FailedChunk:
    """One bounded ask per category. A category that never yields valid output is recorded in
    `failed_categories` and the others' findings are kept; only if all fail is it a FailedChunk.
    LLM/budget errors propagate for the pipeline's retry policy."""
    keep = SEVERITY_ORDER[: SEVERITY_ORDER.index(min_severity) + 1]
    found: list[Finding] = []
    failed: list[FailedCategory] = []
    rejected = 0
    for cat in categories:
        messages = build_messages(chunk, cat)
        reply: _Reply | None = None
        reason = ""
        for attempt in range(MAX_ATTEMPTS):
            guarded = guard_messages(messages)  # every outbound call, re-asks included
            if attempt == 0 and events is not None and job_id is not None:
                record_guard(events, job_id, guarded)
            res = await provider.complete("review", guarded.messages, job_id=job_id)
            try:
                reply = _parse(res.text, cat)
                break
            except (ValidationError, ValueError) as exc:
                reason = _summary(exc)
                messages = [
                    *messages,
                    {"role": "assistant", "content": res.text},
                    {
                        "role": "user",
                        "content": "Your reply was invalid: "
                        f"{reason}\nReply again with ONLY the corrected JSON object.",
                    },
                ]
        if reply is None:
            failed.append(
                FailedCategory(category=cat, reason=redact(reason)[:500], attempts=MAX_ATTEMPTS)
            )
            continue
        checked = check_output(reply.findings, chunk)
        rejected += checked.dropped
        for f in checked.findings:
            if f.severity not in keep:
                continue
            anchored = _anchor(f, chunk)
            if anchored is None:
                rejected += 1
                continue
            found.append(anchored)
    if failed and len(failed) == len(categories):
        why = "; ".join(f"{x.category}: {x.reason}" for x in failed)
        return FailedChunk(
            chunk_id=chunk.id,
            reason=f"all categories invalid after {MAX_ATTEMPTS} attempts each: {why}"[:500],
            attempts=MAX_ATTEMPTS * len(failed),
        )
    return FindingSet(
        prompt_version=PROMPT_VERSION,
        chunk_id=chunk.id,
        findings=tuple(found),
        failed_categories=tuple(failed),
        rejected=rejected,
    )
