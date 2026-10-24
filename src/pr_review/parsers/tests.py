"""Test-output parsers into versioned models. Input is untrusted; failures never raise."""

import json
import logging
import posixpath
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from pr_review.core.redaction import redact

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2  # v2: TestFailure.locations (v1 payloads lack it and still validate)
MAX_INPUT_BYTES = 5_000_000
MAX_CASES = 10_000
MAX_MESSAGE = 2_000
_ERR_MESSAGE = 200
MAX_LOCATIONS = 20
_SCAN_CAP = 200_000
_LOC = re.compile(r"((?:[\w@+.-]*[/\\])*[\w@+.-]+\.(?:py|go|rs|js|jsx|ts|tsx|mjs|cjs)):(\d+)")
_NOISE = (
    "site-packages",
    "dist-packages",
    "/lib/python",
    "node_modules",
    "/rustc/",
    ".cargo/registry",
    "/usr/local/go/",
    "/go/pkg/mod/",
    "<frozen ",
    "/_pytest/",
)

Status = Literal["passed", "failed", "skipped", "errored"]


class ParseError(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: int = SCHEMA_VERSION
    framework: str
    error_class: str
    message: str


class Location(BaseModel):
    model_config = ConfigDict(frozen=True)
    file: str
    line: int


class TestFailure(BaseModel):
    __test__ = False
    model_config = ConfigDict(frozen=True)
    test_id: str
    file: str | None = None
    line: int | None = None
    message: str = ""
    message_truncated: bool = False
    locations: tuple[Location, ...] = ()  # project frames from the traceback, innermost first


class TestRunResult(BaseModel):
    __test__ = False
    model_config = ConfigDict(frozen=True)
    schema_version: int = SCHEMA_VERSION
    framework: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errored: int = 0
    duration: float = 0.0
    failures: tuple[TestFailure, ...] = ()
    truncated: bool = False
    warnings: tuple[str, ...] = ()


def clip(text: str, cap: int = MAX_MESSAGE) -> tuple[str, bool]:
    """Redact then truncate; pre-cut so redaction never scans huge strings."""
    cut = len(text) > cap
    return redact(text[: cap * 4])[:cap], cut


def rel_path(path: str, root: str, warnings: list[str]) -> str | None:
    """Repo-relative POSIX path, or None (with a warning) if it escapes the workspace."""
    p = path.replace("\\", "/")
    if p.startswith("/") or re.match(r"[A-Za-z]:/", p):
        prefix = root.replace("\\", "/").rstrip("/") + "/"
        if not root or not p.startswith(prefix):
            _drop(path, warnings)
            return None
        p = p[len(prefix) :]
    norm = posixpath.normpath(p)
    if "\0" in norm or norm in ("", ".", "..") or norm.startswith("../"):
        _drop(path, warnings)
        return None
    return norm


def extract_locations(
    text: str, root: str, *, innermost_last: bool = False
) -> tuple[Location, ...]:
    """Project-only ``file:line`` frames from long failure text, innermost first, capped."""
    found: list[Location] = []
    for m in _LOC.finditer(text[:_SCAN_CAP]):
        raw = m[1].replace("\\", "/")
        if any(n in raw for n in _NOISE):
            continue
        path = rel_path(raw, root, [])  # escapes are dropped silently: tracebacks are noisy
        if path is not None and (loc := Location(file=path, line=int(m[2]))) not in found:
            found.append(loc)
    if innermost_last:
        found.reverse()
    return tuple(found[:MAX_LOCATIONS])


def _drop(path: str, warnings: list[str]) -> None:
    msg = f"dropped path outside workspace: {clip(path, 100)[0]}"
    log.warning(msg)
    warnings.append(msg)


def parse_xml(text: str) -> ET.Element:
    """Stdlib XML parse with DOCTYPE/ENTITY rejected first (no XXE, no entity expansion)."""
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I):
        raise ValueError("DOCTYPE/ENTITY declarations are not allowed")
    return ET.fromstring(text)


def guarded[M: BaseModel](
    framework: str, data: str | bytes, fn: Callable[[str], M]
) -> M | ParseError:
    """Run a parser with a size cap; any failure becomes a ParseError."""
    try:
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError(f"input exceeds {MAX_INPUT_BYTES} bytes")
        text = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
        return fn(text)
    except Exception as e:  # noqa: BLE001 - untrusted input must never crash the caller
        return ParseError(
            framework=framework,
            error_class=type(e).__name__,
            message=clip(str(e), _ERR_MESSAGE)[0],
        )


def to_int(v: object) -> int | None:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


class _Acc:
    def __init__(self, framework: str, root: str) -> None:
        self.framework, self.root = framework, root
        self.counts: dict[str, int] = {"passed": 0, "failed": 0, "skipped": 0, "errored": 0}
        self.failures: list[TestFailure] = []
        self.warnings: list[str] = []
        self.n = 0
        self.truncated = False

    def add(
        self,
        status: Status,
        test_id: str,
        file: str | None = None,
        line: int | None = None,
        msg: str = "",
        trace: str = "",
        innermost_last: bool = False,
    ) -> None:
        if self.n >= MAX_CASES:
            self.truncated = True
            return
        self.n += 1
        self.counts[status] += 1
        if status in ("failed", "errored"):
            text, cut = clip(msg)
            self.failures.append(
                TestFailure(
                    test_id=clip(test_id, 300)[0],
                    file=rel_path(file, self.root, self.warnings) if file else None,
                    line=line,
                    message=text,
                    message_truncated=cut,
                    locations=extract_locations(
                        trace or msg, self.root, innermost_last=innermost_last
                    ),
                )
            )

    def result(self, duration: float) -> TestRunResult:
        return TestRunResult(
            framework=self.framework,
            duration=duration,
            failures=tuple(self.failures),
            truncated=self.truncated,
            warnings=tuple(self.warnings),
            **self.counts,
        )


def _as_dict(obj: object) -> dict[str, object]:
    if not isinstance(obj, dict):
        raise ValueError("expected a JSON object")
    return obj


def _as_dict_or_empty(obj: object) -> dict[str, object]:
    return obj if isinstance(obj, dict) else {}


def _list(obj: object) -> list[object]:
    return obj if isinstance(obj, list) else []


_JEST = {"passed": "passed", "failed": "failed"}


def parse_jest(data: str | bytes, root: str = "") -> TestRunResult | ParseError:
    def run(text: str) -> TestRunResult:
        doc = _as_dict(json.loads(text))
        suites = doc["testResults"]
        if not isinstance(suites, list):
            raise ValueError("testResults must be a list")
        acc, dur = _Acc("jest", root), 0.0
        for s in suites:
            s = _as_dict(s)
            name = str(s.get("name", ""))
            span = (to_int(s.get("endTime")) or 0) - (to_int(s.get("startTime")) or 0)
            dur += max(span, 0) / 1000
            cases = _list(s.get("assertionResults"))
            if not cases and s.get("status") == "failed":
                acc.add("errored", name, name, None, str(s.get("message", "")))
            for c in cases:
                c = _as_dict(c)
                status: Status = _JEST.get(str(c.get("status")), "skipped")  # type: ignore[assignment]
                loc = c.get("location")
                line = to_int(loc.get("line")) if isinstance(loc, dict) else None
                acc.add(
                    status,
                    str(c.get("fullName", "")),
                    name,
                    line,
                    "\n".join(map(str, _list(c.get("failureMessages")))),
                )
        return acc.result(dur)

    return guarded("jest", data, run)


def parse_junit(data: str | bytes, root: str = "") -> TestRunResult | ParseError:
    def run(text: str) -> TestRunResult:
        tree = parse_xml(text)
        if tree.tag not in ("testsuite", "testsuites"):
            raise ValueError(f"unexpected root element <{tree.tag}>")
        acc, dur = _Acc("pytest-junit", root), 0.0
        for tc in tree.iter("testcase"):
            dur += float(tc.get("time") or 0)
            cls, name = tc.get("classname"), tc.get("name") or ""
            status: Status = "passed"
            msg = trace = ""
            for tag, st in (("failure", "failed"), ("error", "errored"), ("skipped", "skipped")):
                el = tc.find(tag)
                if el is not None:
                    status, msg = st, el.get("message") or el.text or ""  # type: ignore[assignment]
                    trace = el.text or ""
                    break
            acc.add(
                status,
                f"{cls}::{name}" if cls else name,
                tc.get("file"),
                to_int(tc.get("line")),
                msg,
                trace,
                innermost_last=True,
            )
        return acc.result(dur)

    return guarded("pytest-junit", data, run)


def parse_pytest_json(data: str | bytes, root: str = "") -> TestRunResult | ParseError:
    outcomes: dict[str, Status] = {
        "passed": "passed",
        "xpassed": "passed",
        "failed": "failed",
        "error": "errored",
        "skipped": "skipped",
        "xfailed": "skipped",
    }

    def run(text: str) -> TestRunResult:
        doc = _as_dict(json.loads(text))
        tests = doc["tests"]
        if not isinstance(tests, list):
            raise ValueError("tests must be a list")
        acc = _Acc("pytest-json", root)
        for t in tests:
            t = _as_dict(t)
            nodeid = str(t.get("nodeid", ""))
            msg, line, trace = "", None, ""
            for phase in ("setup", "call", "teardown"):
                p = t.get(phase)
                if isinstance(p, dict) and p.get("outcome") in ("failed", "error"):
                    crash = p.get("crash")
                    frames = [*_list(p.get("traceback")), crash]  # outer -> inner
                    trace = (
                        "\n".join(
                            f"{fr.get('path')}:{fr.get('lineno')}"
                            for fr in map(_as_dict_or_empty, frames)
                        )
                        + "\n"
                        + str(p.get("longrepr", ""))
                    )
                    if isinstance(crash, dict):
                        line = to_int(crash.get("lineno"))
                        msg = str(crash.get("message", ""))
                    else:
                        msg = str(p.get("longrepr", ""))
                    break
            acc.add(
                outcomes.get(str(t.get("outcome")), "skipped"),
                nodeid,
                nodeid.split("::")[0] or None,
                line,
                msg,
                trace,
                innermost_last=True,
            )
        return acc.result(float(str(doc.get("duration") or 0)))

    return guarded("pytest-json", data, run)


_GO_LOC = re.compile(r"(\S+\.go):(\d+)")


def parse_go_test(data: str | bytes, root: str = "") -> TestRunResult | ParseError:
    final: dict[object, Status] = {"pass": "passed", "fail": "failed", "skip": "skipped"}

    def run(text: str) -> TestRunResult:
        acc, dur = _Acc("go-test", root), 0.0
        out: dict[tuple[str, str], list[str]] = {}
        failed_pkgs: set[str] = set()
        seen = False
        for raw in text.splitlines():
            if not raw.strip():
                continue
            ev = _as_dict(json.loads(raw))
            seen = True
            action, pkg, test = ev.get("Action"), str(ev.get("Package", "")), ev.get("Test")
            key = (pkg, str(test or ""))
            if action == "output":
                buf = out.setdefault(key, [])
                if sum(map(len, buf)) < MAX_MESSAGE * 4:
                    buf.append(str(ev.get("Output", "")))
            elif test and action in final:
                msg = "".join(out.get(key, []))
                m = _GO_LOC.search(msg)
                if action == "fail":
                    failed_pkgs.add(pkg)
                acc.add(
                    final[action],
                    f"{pkg}.{test}",
                    m.group(1) if m else None,
                    int(m.group(2)) if m else None,
                    msg,
                )
            elif not test and action in ("pass", "fail"):
                dur += float(str(ev.get("Elapsed") or 0))
                if action == "fail" and pkg not in failed_pkgs:  # build failure etc.
                    acc.add("errored", pkg, None, None, "".join(out.get(key, [])))
        if not seen:
            raise ValueError("no go test events")
        return acc.result(dur)

    return guarded("go-test", data, run)


_CARGO_TEST = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)")
_CARGO_SUM = re.compile(r"^test result: .*finished in ([\d.]+)s")
_CARGO_LOC = re.compile(r"(\S+\.rs):(\d+)")


def parse_cargo_test(data: str | bytes, root: str = "") -> TestRunResult | ParseError:
    def run(text: str) -> TestRunResult:
        status_of: dict[str, Status] = {"ok": "passed", "FAILED": "failed", "ignored": "skipped"}
        results: list[tuple[str, Status]] = []
        sections: dict[str, list[str]] = {}
        cur: list[str] | None = None
        dur, summaries = 0.0, 0
        for line in text.splitlines():
            if m := _CARGO_TEST.match(line):
                results.append((m[1], status_of[m[2]]))
            elif m := _CARGO_SUM.match(line):
                dur += float(m[1])
                summaries += 1
                cur = None
            elif m := re.match(r"^---- (\S+) stdout ----", line):
                cur = sections.setdefault(m[1], [])
            elif line == "failures:":
                cur = None
            elif cur is not None and sum(map(len, cur)) < MAX_MESSAGE * 4:
                cur.append(line)
        if not summaries:
            raise ValueError("no 'test result:' line (truncated or not cargo test output)")
        acc = _Acc("cargo-test", root)
        for name, st in results:
            msg = "\n".join(sections.get(name, [])).strip()
            m2 = _CARGO_LOC.search(msg)
            acc.add(st, name, m2[1] if m2 else None, int(m2[2]) if m2 else None, msg)
        return acc.result(dur)

    return guarded("cargo-test", data, run)
