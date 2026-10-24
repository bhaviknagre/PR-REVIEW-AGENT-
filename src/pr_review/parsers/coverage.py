"""Parsers for coverage reports into versioned models. Input is untrusted; failures never raise."""

import json
import re
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from pr_review.parsers.tests import (
    SCHEMA_VERSION,
    ParseError,
    _as_dict,
    _list,
    guarded,
    parse_xml,
    rel_path,
    to_int,
)

MAX_FILES = 5_000

Ranges = tuple[tuple[int, int], ...]
Entry = tuple[str, int, bool]  # (path, line, covered)


class FileCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)
    path: str
    covered: Ranges = ()
    uncovered: Ranges = ()
    covered_lines: int = 0
    uncovered_lines: int = 0


class CoverageResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: int = SCHEMA_VERSION
    framework: str
    files: tuple[FileCoverage, ...] = ()
    covered_lines: int = 0
    uncovered_lines: int = 0
    truncated: bool = False
    warnings: tuple[str, ...] = ()


def _ranges(lines: Iterable[int]) -> Ranges:
    out: list[list[int]] = []
    for n in sorted(lines):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return tuple((a, b) for a, b in out)


def _build(framework: str, entries: Iterable[Entry], root: str) -> CoverageResult:
    warnings: list[str] = []
    per_file: dict[str, dict[int, bool]] = {}
    skipped: set[str] = set()
    truncated = False
    for raw, line, hit in entries:
        if raw in skipped:
            continue
        path = rel_path(raw, root, warnings)
        if path is None:
            skipped.add(raw)
            continue
        if path not in per_file and len(per_file) >= MAX_FILES:
            truncated = True
            skipped.add(raw)
            continue
        lines = per_file.setdefault(path, {})
        lines[line] = lines.get(line, False) or hit
    files = []
    for path, lines in per_file.items():
        cov = [n for n, h in lines.items() if h]
        unc = [n for n, h in lines.items() if not h]
        files.append(
            FileCoverage(
                path=path,
                covered=_ranges(cov),
                uncovered=_ranges(unc),
                covered_lines=len(cov),
                uncovered_lines=len(unc),
            )
        )
    return CoverageResult(
        framework=framework,
        files=tuple(files),
        truncated=truncated,
        warnings=tuple(warnings),
        covered_lines=sum(f.covered_lines for f in files),
        uncovered_lines=sum(f.uncovered_lines for f in files),
    )


def parse_lcov(data: str | bytes, root: str = "") -> CoverageResult | ParseError:
    def entries(text: str) -> Iterable[Entry]:
        path, closed, seen = None, True, False
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("SF:"):
                path, closed, seen = line[3:], False, True
            elif line.startswith("DA:") and path is not None:
                n, _, rest = line[3:].partition(",")
                hits = rest.split(",")[0]
                yield path, int(n), int(hits) > 0
            elif line == "end_of_record":
                path, closed = None, True
        if not seen or not closed:
            raise ValueError("no SF record or truncated (missing end_of_record)")

    return guarded("lcov", data, lambda t: _build("lcov", list(entries(t)), root))


def parse_cobertura(data: str | bytes, root: str = "") -> CoverageResult | ParseError:
    def entries(text: str) -> Iterable[Entry]:
        tree = parse_xml(text)
        if tree.tag != "coverage":
            raise ValueError(f"unexpected root element <{tree.tag}>")
        for cls in tree.iter("class"):
            for ln in cls.iter("line"):
                hit = int(ln.attrib["hits"]) > 0
                yield cls.get("filename") or "", int(ln.attrib["number"]), hit

    return guarded("cobertura", data, lambda t: _build("cobertura", list(entries(t)), root))


def parse_coverage_json(data: str | bytes, root: str = "") -> CoverageResult | ParseError:
    def entries(text: str) -> Iterable[Entry]:
        files = _as_dict(_as_dict(json.loads(text))["files"])
        for path, info in files.items():
            info = _as_dict(info)
            for key, hit in (("executed_lines", True), ("missing_lines", False)):
                for n in _list(info.get(key)):
                    ln = to_int(n)
                    if ln is None:
                        raise ValueError("non-integer line number")
                    yield path, ln, hit

    return guarded("coverage-json", data, lambda t: _build("coverage-json", list(entries(t)), root))


_GO_BLOCK = re.compile(r"^(.+):(\d+)\.\d+,(\d+)\.\d+ \d+ (\d+)$")


def parse_go_cover(data: str | bytes, root: str = "") -> CoverageResult | ParseError:
    def entries(text: str) -> Iterable[Entry]:
        lines = text.splitlines()
        if not lines or not lines[0].startswith("mode:"):
            raise ValueError("missing 'mode:' header")
        for line in lines[1:]:
            if not line.strip():
                continue
            m = _GO_BLOCK.match(line)
            if not m:
                raise ValueError("malformed cover block")
            start, end = int(m[2]), int(m[3])
            if end < start or end - start > 100_000:
                raise ValueError("invalid line range")
            for n in range(start, end + 1):
                yield m[1], n, int(m[4]) > 0

    return guarded("go-cover", data, lambda t: _build("go-cover", list(entries(t)), root))
