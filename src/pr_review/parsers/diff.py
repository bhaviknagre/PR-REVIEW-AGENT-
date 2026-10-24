"""Unified-diff parser and skip rules. Input is untrusted; malformed parts warn, never raise."""

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from pr_review.parsers.tests import rel_path

MAX_FILES = 3000
Status = Literal["added", "modified", "deleted", "renamed"]
LineType = Literal["add", "del", "ctx"]

LOCK_FILES = frozenset(
    {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock",
     "Cargo.lock", "go.sum"}
)  # fmt: skip
VENDORED_DIRS = frozenset({"vendor", "node_modules", "third_party"})
_GENERATED_PATH = re.compile(r"(\.min\.(js|css)$|_pb2(_grpc)?\.pyi?$|\.pb\.go$|(^|/)dist/)")
_GENERATED_MARKERS = ("@generated", "DO NOT EDIT", "Code generated")
_MARKER_LINES = 10
_BINARY_EXT = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".jar", ".woff", ".woff2",
     ".ttf", ".so", ".dll", ".exe", ".pyc", ".class"}
)  # fmt: skip
_HUNK = re.compile(r"^@@ -(\d{1,9})(?:,(\d{1,9}))? \+(\d{1,9})(?:,(\d{1,9}))? @@ ?(.*)$")


@dataclass(frozen=True)
class Line:
    type: LineType
    text: str
    old_no: int | None  # None for added lines
    new_no: int | None  # None for deleted lines; RIGHT-side anchor for inline comments


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    section: str
    lines: tuple[Line, ...]


@dataclass(frozen=True)
class FileDiff:
    old_path: str | None
    new_path: str | None
    status: Status
    binary: bool
    hunks: tuple[Hunk, ...]
    warnings: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""


@dataclass(frozen=True)
class ParsedDiff:
    files: tuple[FileDiff, ...]
    warnings: tuple[str, ...] = ()  # diff-level: dropped paths, file cap


@dataclass
class _Builder:
    old: str | None = None
    new: str | None = None
    added: bool = False
    deleted: bool = False
    binary: bool = False
    saw_minus: bool = False
    hunks: list[Hunk] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_ESC = {"n": 10, "t": 9, "r": 13, '"': 34, "\\": 92, "a": 7, "b": 8, "f": 12, "v": 11}


def _unquote(s: str) -> str:
    """Decode a git C-style quoted path ("a/caf\\303\\251"); unquoted input passes through."""
    if len(s) < 2 or s[0] != '"' or s[-1] != '"':
        return s
    out = bytearray()
    body = s[1:-1].encode("utf-8", "surrogateescape")
    i = 0
    while i < len(body):
        c = body[i]
        if c == 92 and i + 1 < len(body):
            nxt = body[i + 1]
            if 48 <= nxt <= 55:  # octal, up to 3 digits
                j = i + 1
                while j < len(body) and j < i + 4 and 48 <= body[j] <= 55:
                    j += 1
                out.append(int(body[i + 1 : j], 8) & 0xFF)
                i = j
                continue
            out.append(_ESC.get(chr(nxt), nxt))
            i += 2
            continue
        out.append(c)
        i += 1
    return out.decode("utf-8", "replace")


def _file_token(s: str) -> str:
    """Path from a ---/+++ line: quoted token, or text up to the timestamp tab."""
    if s.startswith('"'):
        i = 1
        while i < len(s) and s[i] != '"':
            i += 2 if s[i] == "\\" else 1
        return _unquote(s[: i + 1])
    return s.split("\t")[0]


def _git_header(rest: str) -> tuple[str | None, str | None]:
    if rest.startswith('"'):
        i = 1
        while i < len(rest) and rest[i] != '"':
            i += 2 if rest[i] == "\\" else 1
        return _unquote(rest[: i + 1]), _unquote(rest[i + 1 :].strip()) or None
    for i, ch in enumerate(rest):  # a/X b/X: pick the split whose halves agree
        if ch == " " and rest[:i][2:] == rest[i + 1 :][2:] and rest[:i]:
            return rest[:i], rest[i + 1 :]
    a, _, b = rest.partition(" b/")
    return (a, "b/" + b) if b else (rest, None)


def _strip(p: str | None, prefix: str) -> str | None:
    if p is None or p == "/dev/null":
        return None
    return p[2:] if p.startswith(prefix) else p


def _finish(b: _Builder, out: list[FileDiff], warnings: list[str]) -> None:
    if b.old is None and b.new is None:
        return
    if len(out) >= MAX_FILES:
        msg = f"file cap {MAX_FILES} reached; remaining files ignored"
        if msg not in warnings:
            warnings.append(msg)
        return
    old = new = None
    for raw, side in ((b.old, "old"), (b.new, "new")):
        if raw is None:
            continue
        clean = rel_path(raw, "", warnings)
        if clean is None:
            return
        if side == "old":
            old = clean
        else:
            new = clean
    if b.added:
        old = None
    if b.deleted:
        new = None
    if old is None and new is None:
        return
    status: Status = (
        "added" if old is None else "deleted" if new is None
        else "modified" if old == new else "renamed"
    )  # fmt: skip
    out.append(FileDiff(old, new, status, b.binary, tuple(b.hunks), tuple(b.warnings)))


def parse_diff(text: str) -> ParsedDiff:
    rows = text.split("\n")
    n = len(rows)
    files: list[FileDiff] = []
    warnings: list[str] = []
    cur = _Builder()
    i = 0
    while i < n:
        ln = rows[i].removesuffix("\r")
        i += 1
        if ln.startswith("diff --git "):
            _finish(cur, files, warnings)
            cur = _Builder()
            o, nw = _git_header(ln[11:])
            cur.old, cur.new = _strip(o, "a/"), _strip(nw, "b/")
        elif ln.startswith("new file mode"):
            cur.added = True
        elif ln.startswith("deleted file mode"):
            cur.deleted = True
        elif ln.startswith("rename from "):
            cur.old = _unquote(ln[12:])
        elif ln.startswith("rename to "):
            cur.new = _unquote(ln[10:])
        elif ln.startswith(("Binary files ", "GIT binary patch")):
            cur.binary = True
        elif ln.startswith("--- "):
            if cur.saw_minus or cur.hunks:  # plain (non-git) diff: new file section
                _finish(cur, files, warnings)
                cur = _Builder()
            cur.saw_minus = True
            p = _strip(_file_token(ln[4:]), "a/")
            cur.old = p
            cur.added = cur.added or p is None
        elif ln.startswith("+++ "):
            p = _strip(_file_token(ln[4:]), "b/")
            cur.new = p
            cur.deleted = cur.deleted or p is None
        elif ln.startswith("@@"):
            m = _HUNK.match(ln)
            if not m:
                cur.warnings.append(f"malformed hunk header: {ln[:80]!r}")
                continue
            os_, ol, ns, nl, sec = m.groups()
            oc, nc = int(ol) if ol is not None else 1, int(nl) if nl is not None else 1
            lines, i = _body(rows, i, int(os_), oc, int(ns), nc, cur.warnings)
            cur.hunks.append(Hunk(int(os_), oc, int(ns), nc, sec, lines))
    _finish(cur, files, warnings)
    return ParsedDiff(tuple(files), tuple(warnings))


def _body(
    rows: list[str], i: int, o: int, ol: int, nw: int, nl: int, warns: list[str]
) -> tuple[tuple[Line, ...], int]:
    out: list[Line] = []
    n = len(rows)
    while (ol > 0 or nl > 0) and i < n:
        raw = rows[i].removesuffix("\r")
        c = raw[:1]
        if c == "\\":
            i += 1
            continue
        if (c == " " or (raw == "" and i < n - 1)) and ol > 0 and nl > 0:
            out.append(Line("ctx", raw[1:], o, nw))
            o, nw, ol, nl = o + 1, nw + 1, ol - 1, nl - 1
        elif c == "-" and ol > 0:
            out.append(Line("del", raw[1:], o, None))
            o, ol = o + 1, ol - 1
        elif c == "+" and nl > 0:
            out.append(Line("add", raw[1:], None, nw))
            nw, nl = nw + 1, nl - 1
        else:
            break
        i += 1
    if ol > 0 or nl > 0:
        warns.append("hunk shorter than its header declares")
    while i < n and rows[i].startswith("\\"):  # "\ No newline at end of file"
        i += 1
    return tuple(out), i


def skip_reason(f: FileDiff, ignore: Sequence[str] = ()) -> str | None:
    """Why this file is not reviewed, or None. Reasons are shown in the PR summary."""
    path = f.path
    base = path.rsplit("/", 1)[-1]
    if f.binary or ("." in base and "." + base.rsplit(".", 1)[-1].lower() in _BINARY_EXT):
        return "binary file"
    if base in LOCK_FILES:
        return "lock file"
    if VENDORED_DIRS.intersection(path.split("/")[:-1]):
        return "vendored dependency"
    if _GENERATED_PATH.search(path):
        return "generated file (path)"
    if f.hunks:
        head = f.hunks[0]
        if head.old_start <= 1 or head.new_start <= 1:
            for ln in head.lines[:_MARKER_LINES]:
                if any(m in ln.text for m in _GENERATED_MARKERS):
                    return "generated file (marker)"
    for g in ignore:
        if fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(base, g):
            return f"ignored by config: {g}"
    return None
