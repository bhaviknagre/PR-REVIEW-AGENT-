"""Enclosing function/class context for diff chunks (FR-13, AC-10).

Python uses stdlib `ast`; JS/TS/Go/Rust use tree-sitter (per-language wheels: prebuilt for macOS
and Linux, no compilation, no network). Source files are untrusted: read only regular files that
resolve inside the workspace, capped in size, parsed under a time limit, and the resulting text is
redacted. Context is trimmed to fit the chunk's token budget; the diff text is never touched.
"""

import ast
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import tree_sitter as ts
import tree_sitter_go
import tree_sitter_javascript
import tree_sitter_rust
import tree_sitter_typescript

from pr_review.core.redaction import redact
from pr_review.parsers.chunker import DEFAULT_BUDGET, Chunk, TokenCounter, estimate_tokens
from pr_review.runner.workspace import Workspace

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 1 << 20
MAX_CONTEXT_LINES = 40  # context lines per chunk, header and signature included
SURROUND_LINES = 5  # source lines kept above and below the changed span
MAX_LINE_CHARS = 200
MAX_SIG_LINES = 4
MAX_SUMMARY_ITEMS = 8
PARSE_TIMEOUT_S = 2.0

_LANGS: dict[str, Callable[[], Any]] = {
    "javascript": tree_sitter_javascript.language,
    "typescript": tree_sitter_typescript.language_typescript,
    "tsx": tree_sitter_typescript.language_tsx,
    "go": tree_sitter_go.language,
    "rust": tree_sitter_rust.language,
}
_EXT = {
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".go": "go", ".rs": "rust", ".py": "python", ".pyi": "python",
}  # fmt: skip
_SCOPES = {
    "function_declaration", "generator_function_declaration", "method_definition",
    "class_declaration", "abstract_class_declaration", "method_declaration", "function_item",
    "impl_item", "trait_item", "struct_item", "enum_item", "mod_item", "interface_declaration",
}  # fmt: skip
_IMPORTS = {"import_statement", "import_declaration", "use_declaration"}


@dataclass(frozen=True)
class _Scope:
    name: str
    kind: str
    start: int  # 1-based inclusive
    end: int
    sig: tuple[str, ...]
    sig_line: int


@dataclass(frozen=True)
class _Parsed:
    lines: tuple[str, ...]
    scopes: Callable[[int, int], list[_Scope]]  # outermost first, containing [first, last]
    summary: Callable[[int], list[str]]  # module-level imports / nearby top-level names


class _ParseError(Exception):
    pass


def _read(root: Path, rel: str, max_bytes: int) -> str:
    """Read a regular file inside root; symlinks (even in parent dirs) are ignored."""
    base = root.resolve()
    p = base / rel
    if p.is_symlink() or not p.resolve().is_relative_to(base) or not p.is_file():
        raise _ParseError("not a regular file inside the workspace")
    if p.stat().st_size > max_bytes:
        raise _ParseError(f"file larger than {max_bytes} bytes")
    return p.read_bytes().decode("utf-8", errors="replace")


def _python(text: str, lines: tuple[str, ...], timeout: float) -> _Parsed:
    t0 = time.monotonic()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as e:
        raise _ParseError(f"python parse failed: {type(e).__name__}") from e
    if time.monotonic() - t0 > timeout:
        raise _ParseError("parse time limit exceeded")
    found: list[_Scope] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                kind = {ast.FunctionDef: "def", ast.AsyncFunctionDef: "async def"}.get(
                    type(ch), "class"
                )
                start = min([ch.lineno, *(d.lineno for d in ch.decorator_list)])
                sig_end = max(ch.lineno, ch.body[0].lineno - 1)
                sig = tuple(lines[ch.lineno - 1 : min(sig_end, ch.lineno - 1 + MAX_SIG_LINES)])
                found.append(
                    _Scope(
                        prefix + ch.name, kind, start, ch.end_lineno or ch.lineno, sig, ch.lineno
                    )
                )
                walk(ch, f"{prefix}{ch.name}.")
            else:
                walk(ch, prefix)

    walk(tree, "")

    def summary(first: int) -> list[str]:
        imports = [
            lines[n.lineno - 1].strip()
            for n in tree.body
            if isinstance(n, ast.Import | ast.ImportFrom)
        ][:MAX_SUMMARY_ITEMS]
        tops: list[tuple[int, str]] = []
        for n in tree.body:
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                tops.append((n.lineno, f"{n.name}@{n.lineno}"))
            elif isinstance(n, ast.Assign):
                tops += [
                    (n.lineno, f"{t.id}@{n.lineno}") for t in n.targets if isinstance(t, ast.Name)
                ]
        near = sorted(tops, key=lambda x: abs(x[0] - first))[:MAX_SUMMARY_ITEMS]
        return _summary_lines(imports, [s for _, s in sorted(near)])

    return _Parsed(lines, lambda a, b: [s for s in found if s.start <= a and s.end >= b], summary)


def _summary_lines(imports: Sequence[str], names: Sequence[str]) -> list[str]:
    out = [f"imports: {'; '.join(imports)}"] if imports else []
    return [*out, f"top-level: {', '.join(names)}"] if names else out


def _ts_name(n: ts.Node) -> str:
    nm = n.child_by_field_name("name") or n.child_by_field_name("type")
    if nm is None and n.parent is not None and n.parent.type == "variable_declarator":
        nm = n.parent.child_by_field_name("name")
    return nm.text.decode(errors="replace") if nm is not None and nm.text else "<anonymous>"


def _ts_scope(n: ts.Node, lines: tuple[str, ...]) -> _Scope:
    body = n.child_by_field_name("body")
    first, last = n.start_point.row, (body.start_point.row if body else n.start_point.row)
    sig = tuple(lines[first : min(last + 1, first + MAX_SIG_LINES)])
    return _Scope(_ts_name(n), n.type, n.start_point.row + 1, n.end_point.row + 1, sig, first + 1)


def _treesitter(lang: str, text: str, lines: tuple[str, ...], timeout: float) -> _Parsed:
    src = text.encode()
    deadline = time.monotonic() + timeout
    expired = False

    def reader(offset: int, _pt: Any) -> bytes:
        nonlocal expired
        if time.monotonic() > deadline:
            expired = True
            return b""
        return src[offset : offset + 4096]

    # ponytail: time limit is enforced by starving the reader; progress_callback segfaults in 0.26
    tree = ts.Parser(ts.Language(_LANGS[lang]())).parse(reader)
    if expired or tree is None:
        raise _ParseError("parse time limit exceeded")
    if tree.root_node.has_error:
        raise _ParseError(f"{lang} parse failed: syntax errors")
    root = tree.root_node

    def scopes(a: int, b: int) -> list[_Scope]:
        width = len(lines[b - 1].encode()) if b <= len(lines) else 0
        node: ts.Node | None = root.descendant_for_point_range((a - 1, 0), (b - 1, width))
        out: list[_Scope] = []
        while node is not None:
            is_arrow = node.type == "arrow_function" and (
                node.parent is not None and node.parent.type == "variable_declarator"
            )
            if node.type in _SCOPES or is_arrow:
                out.append(_ts_scope(node, lines))
            node = node.parent
        return out[::-1]

    def summary(first: int) -> list[str]:
        imports: list[str] = []
        tops: list[tuple[int, str]] = []
        for ch in root.children:
            if ch.type in _IMPORTS and len(imports) < MAX_SUMMARY_ITEMS:
                imports.append(lines[ch.start_point.row].strip())
            inner = ch.child_by_field_name("declaration") if ch.type == "export_statement" else ch
            if inner is not None and inner.type in _SCOPES:
                tops.append(
                    (inner.start_point.row + 1, f"{_ts_name(inner)}@{inner.start_point.row + 1}")
                )
        near = sorted(tops, key=lambda x: abs(x[0] - first))[:MAX_SUMMARY_ITEMS]
        return _summary_lines(imports, [s for _, s in sorted(near)])

    return _Parsed(lines, scopes, summary)


def _parse(root: Path, path: str, max_bytes: int, timeout: float) -> _Parsed | None:
    """None: unsupported language. _ParseError: supported but not usable (caller falls back)."""
    lang = _EXT.get(Path(path).suffix.lower())
    if lang is None:
        return None
    text = _read(root, path, max_bytes)
    lines = tuple(text.splitlines())
    if lang == "python":
        return _python(text, lines, timeout)
    return _treesitter(lang, text, lines, timeout)


def _clip(s: str) -> str:
    return s if len(s) <= MAX_LINE_CHARS else s[:MAX_LINE_CHARS] + " [truncated]"


def _numbered(lines: Sequence[str], lo: int, hi: int) -> list[str]:
    return [_clip(f"{n:>5} {lines[n - 1]}") for n in range(max(lo, 1), min(hi, len(lines)) + 1)]


def _candidates(
    p: _Parsed, first: int, last: int, warn: str | None, max_lines: int
) -> list[list[str]]:
    """Context variants from fullest to emptiest; the caller takes the first that fits."""
    head: list[str] = [f"warning: {warn}"] if warn else []
    scopes = p.scopes(first, last) if warn is None else []
    if scopes:
        s = scopes[-1]
        title = f"scope: {s.kind} {s.name} (lines {s.start}-{s.end})"
        sig = [_clip(f"signature (line {s.sig_line}): {s.sig[0].strip()}") if s.sig else ""]
        sig = [x for x in [*sig, *(_clip(x) for x in s.sig[1:])] if x]
        lo, hi = s.start, s.end
    else:
        title = "scope: file window" if warn else "scope: module-level"
        sig = [] if warn else p.summary(first)
        lo, hi = 1, len(p.lines)
    base = [*head, title, *sig][:max_lines]
    out: list[list[str]] = []
    for k in range(SURROUND_LINES, -1, -1):
        above = _numbered(p.lines, max(lo, first - k), first - 1) if k else []
        below = _numbered(p.lines, last + 1, min(hi, last + k)) if k else []
        out.append((base + above + below)[:max_lines])
    out += [base, base[:1], []]
    return out


def attach_context(
    chunks: Sequence[Chunk],
    workspace: Workspace | Path,
    *,
    budget: int = DEFAULT_BUDGET,
    count: TokenCounter = estimate_tokens,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_context_lines: int = MAX_CONTEXT_LINES,
    parse_timeout_s: float = PARSE_TIMEOUT_S,
) -> list[Chunk]:
    """Return chunks with `context` filled (redacted) and `tokens` including it, <= budget."""
    root = workspace.path if isinstance(workspace, Workspace) else workspace
    cache: dict[str, _Parsed | _ParseError | None] = {}
    out: list[Chunk] = []
    for c in chunks:
        if c.path not in cache:
            try:
                cache[c.path] = _parse(root, c.path, max_file_bytes, parse_timeout_s)
            except (_ParseError, OSError) as e:
                log.warning("context fallback for %s: %s", c.path, e)
                cache[c.path] = e if isinstance(e, _ParseError) else _ParseError("unreadable file")
        got = cache[c.path]
        if got is None:
            out.append(c)
            continue
        lines_known = c.changed_new or {r.new_start: "" for r in c.ranges if r.new_len}
        first = min(lines_known, default=c.ranges[0].new_start if c.ranges else 1)
        last = max(lines_known, default=first)
        if isinstance(got, _ParseError):
            # unreadable/unparseable: a plain line window when we can read the file at all
            try:
                text = _read(root, c.path, max_file_bytes)
            except (_ParseError, OSError):
                out.append(c)
                continue
            p = _Parsed(tuple(text.splitlines()), lambda a, b: [], lambda f: [])
            variants = _candidates(p, first, last, str(got), max_context_lines)
        else:
            variants = _candidates(got, first, last, None, max_context_lines)
        for v in variants:
            ctx = tuple(redact(x) for x in v)
            cost = count("\n".join(ctx)) if ctx else 0
            if c.tokens + cost <= budget:
                out.append(replace(c, context=ctx, tokens=c.tokens + cost))
                break
        else:  # chunk alone exceeds budget: no context
            out.append(c)
    return out
