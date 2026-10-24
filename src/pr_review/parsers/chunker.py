"""Token-budgeted chunking of a parsed diff (FR-13, NFR-15).

A chunk is single-file. Hunks are packed whole; a hunk that alone exceeds the budget is split
into overlapping line windows that keep their original line numbers. `Chunk.context` is left
empty for the enclosing function/class enrichment to fill via `dataclasses.replace`.
"""

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pr_review.parsers.diff import FileDiff, Hunk, Line, ParsedDiff, parse_diff, skip_reason

DEFAULT_BUDGET = 6000
DEFAULT_OVERLAP = 3
MAX_CHUNKS = 200
MAX_TOTAL_TOKENS = 200_000
MAX_LINE_CHARS = 2000

TokenCounter = Callable[[str], int]


def estimate_tokens(text: str) -> int:
    """Deterministic chars/4 estimate with a 20% safety margin. Swap in a real tokenizer."""
    return (len(text) * 6 + 19) // 20


@dataclass(frozen=True)
class HunkRange:
    old_start: int
    old_len: int
    new_start: int
    new_len: int


@dataclass(frozen=True)
class Chunk:
    id: str
    path: str
    ranges: tuple[HunkRange, ...]
    changed_new: dict[int, str]  # new line no -> added text (RIGHT-side comment anchors)
    changed_old: dict[int, str]  # old line no -> deleted text
    text: str
    tokens: int
    windowed: bool = False
    context: tuple[str, ...] = ()


@dataclass(frozen=True)
class Skip:
    path: str
    reason: str


@dataclass(frozen=True)
class ChunkPlan:
    chunks: tuple[Chunk, ...]
    skipped: tuple[Skip, ...]
    truncated: bool
    omitted_chunks: int
    warnings: tuple[str, ...]


def _line(ln: Line) -> str:
    text = ln.text if len(ln.text) <= MAX_LINE_CHARS else ln.text[:MAX_LINE_CHARS] + " [truncated]"
    if ln.type == "del":
        return f"     -{text}"
    return f"{ln.new_no:>5}{'+' if ln.type == 'add' else ' '}{text}"


def _range(lines: Sequence[Line]) -> HunkRange:
    olds = [x.old_no for x in lines if x.old_no is not None]
    news = [x.new_no for x in lines if x.new_no is not None]
    return HunkRange(olds[0] if olds else 0, len(olds), news[0] if news else 0, len(news))


def _header(h: HunkRange, section: str) -> str:
    return f"@@ -{h.old_start},{h.old_len} +{h.new_start},{h.new_len} @@ {section}".rstrip()


def _make(path: str, parts: Sequence[tuple[HunkRange, str, Sequence[Line]]], count: TokenCounter,
          windowed: bool) -> Chunk:  # fmt: skip
    ranges = tuple(p[0] for p in parts)
    body = [f"File: {path}"]
    new: dict[int, str] = {}
    old: dict[int, str] = {}
    for r, section, lines in parts:
        body.append(_header(r, section))
        body.extend(_line(x) for x in lines)
        for x in lines:
            if x.type == "add" and x.new_no is not None:
                new[x.new_no] = x.text
            elif x.type == "del" and x.old_no is not None:
                old[x.old_no] = x.text
    text = "\n".join(body)
    key = (
        path
        + "\0"
        + ";".join(f"{r.old_start},{r.old_len},{r.new_start},{r.new_len}" for r in ranges)
    )
    cid = hashlib.sha256(key.encode()).hexdigest()[:16]
    return Chunk(cid, path, ranges, new, old, text, count(text), windowed)


def _windows(h: Hunk, budget: int, overlap: int, count: TokenCounter) -> list[Sequence[Line]]:
    lines = h.lines
    last = max((k for k, x in enumerate(lines) if x.type != "ctx"), default=len(lines) - 1)
    out: list[Sequence[Line]] = []
    start = 0
    while start < len(lines):
        end, used = start, 0
        while end < len(lines):
            cost = count(_line(lines[end])) + 1
            if end > start and used + cost > budget:
                break
            used += cost
            end += 1
        out.append(lines[start:end])
        if end > last:
            break
        start = max(end - overlap, start + 1)
    return out


def chunk_file(f: FileDiff, budget: int, overlap: int, count: TokenCounter) -> list[Chunk]:
    chunks: list[Chunk] = []
    cur: list[tuple[HunkRange, str, Sequence[Line]]] = []

    def flush() -> None:
        if cur:
            chunks.append(_make(f.path, cur, count, False))
            cur.clear()

    for h in f.hunks:
        if not h.lines:
            continue
        part = (_range(h.lines), h.section, h.lines)
        if _make(f.path, [part], count, False).tokens > budget:
            flush()
            for w in _windows(h, budget, overlap, count):
                chunks.append(_make(f.path, [(_range(w), h.section, w)], count, True))
            continue
        if cur and _make(f.path, [*cur, part], count, False).tokens > budget:
            flush()
        cur.append(part)
    flush()
    return chunks


def chunk_diff(
    diff_text: str,
    *,
    ignore: Sequence[str] = (),
    budget: int = DEFAULT_BUDGET,
    overlap: int = DEFAULT_OVERLAP,
    max_chunks: int = MAX_CHUNKS,
    max_tokens: int = MAX_TOTAL_TOKENS,
    count: TokenCounter = estimate_tokens,
) -> ChunkPlan:
    parsed: ParsedDiff = parse_diff(diff_text)
    skipped: list[Skip] = []
    chunks: list[Chunk] = []
    omitted = total = 0
    for f in sorted(parsed.files, key=lambda x: x.path):
        reason = skip_reason(f, ignore)
        if reason:
            skipped.append(Skip(f.path, reason))
            continue
        for c in chunk_file(f, budget, overlap, count):
            if omitted or len(chunks) >= max_chunks or total + c.tokens > max_tokens:
                omitted += 1
            else:
                chunks.append(c)
                total += c.tokens
    warnings = list(parsed.warnings)
    for f in parsed.files:
        warnings.extend(f"{f.path}: {w}" for w in f.warnings)
    return ChunkPlan(tuple(chunks), tuple(skipped), omitted > 0, omitted, tuple(warnings))
