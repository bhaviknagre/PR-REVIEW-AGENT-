"""Linter-output parsers (Ruff JSON, Flake8 text, ESLint JSON); failures never raise."""

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from pr_review.parsers.tests import (
    MAX_CASES,
    SCHEMA_VERSION,
    ParseError,
    _as_dict,
    _list,
    clip,
    guarded,
    rel_path,
    to_int,
)

Severity = Literal["error", "warning", "info"]
MAX_ISSUES = MAX_CASES
MAX_LINT_MESSAGE = 500  # lint messages are one-liners; tighter than test failures


class LintIssue(BaseModel):
    model_config = ConfigDict(frozen=True)
    file: str
    line: int
    column: int
    end_line: int | None = None
    rule: str
    severity: Severity
    message: str
    fix_available: bool | None = None


class LintResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: int = SCHEMA_VERSION
    tool: str
    issues: tuple[LintIssue, ...] = ()
    truncated: bool = False
    warnings: tuple[str, ...] = ()


class _Acc:
    def __init__(self, tool: str, root: str) -> None:
        self.tool, self.root = tool, root
        self.issues: list[LintIssue] = []
        self.warnings: list[str] = []
        self.truncated = False

    def add(
        self,
        file: str,
        line: int | None,
        col: int | None,
        rule: str,
        severity: Severity,
        msg: str,
        end_line: int | None = None,
        fix: bool | None = None,
    ) -> None:
        if len(self.issues) >= MAX_ISSUES:
            self.truncated = True
            return
        path = rel_path(file, self.root, self.warnings)
        if path is None:
            return
        self.issues.append(
            LintIssue(
                file=path,
                line=line or 1,
                column=col or 1,
                end_line=end_line,
                rule=clip(rule, 100)[0],
                severity=severity,
                message=clip(msg, MAX_LINT_MESSAGE)[0],
                fix_available=fix,
            )
        )

    def result(self) -> LintResult:
        return LintResult(
            tool=self.tool,
            issues=tuple(self.issues),
            truncated=self.truncated,
            warnings=tuple(self.warnings),
        )


def _json_list(text: str) -> list[object]:
    doc = json.loads(text)
    if not isinstance(doc, list):
        raise ValueError("expected a JSON array")
    return doc


def _pos(obj: object, key: str) -> int | None:
    return to_int(obj.get(key)) if isinstance(obj, dict) else None


def parse_ruff(data: str | bytes, root: str = "") -> LintResult | ParseError:
    """`ruff check --output-format json`. Ruff has no severities: all findings are warnings."""

    def run(text: str) -> LintResult:
        acc = _Acc("ruff", root)
        for item in _json_list(text):
            d = _as_dict(item)
            loc = d.get("location")
            acc.add(
                str(d["filename"]),
                _pos(loc, "row"),
                _pos(loc, "column"),
                str(d.get("code") or "invalid-syntax"),
                "warning",
                str(d.get("message", "")),
                _pos(d.get("end_location"), "row"),
                d.get("fix") is not None,
            )
        return acc.result()

    return guarded("ruff", data, run)


def parse_eslint(data: str | bytes, root: str = "") -> LintResult | ParseError:
    """`eslint -f json`. Source snippets (`source`) are ignored."""

    def run(text: str) -> LintResult:
        acc = _Acc("eslint", root)
        for item in _json_list(text):
            f = _as_dict(item)
            path = str(f["filePath"])
            for m in _list(f.get("messages")):
                m = _as_dict(m)
                acc.add(
                    path,
                    to_int(m.get("line")),
                    to_int(m.get("column")),
                    str(m.get("ruleId") or "parse-error"),
                    "error" if m.get("severity") == 2 else "warning",
                    str(m.get("message", "")),
                    to_int(m.get("endLine")),
                    "fix" in m,
                )
        return acc.result()

    return guarded("eslint", data, run)


_FLAKE8 = re.compile(r"^(.+?):(\d+):(?:(\d+):)? \[?([A-Z]+\d+)\]? (.*)$")


def parse_flake8(data: str | bytes, root: str = "") -> LintResult | ParseError:
    """Default (`path:row:col: CODE msg`) and pylint-style (`path:row: [CODE] msg`) formats.

    E9xx and F-codes are errors, everything else warnings. Unmatched lines are ignored,
    but non-empty input with no match at all is a ParseError.
    """

    def run(text: str) -> LintResult:
        acc, lines = _Acc("flake8", root), [ln for ln in text.splitlines() if ln.strip()]
        matched = 0
        for ln in lines:
            if not (m := _FLAKE8.match(ln)):
                continue
            matched += 1
            code = m[4]
            sev: Severity = "error" if code.startswith(("E9", "F")) else "warning"
            acc.add(m[1], to_int(m[2]), to_int(m[3]), code, sev, m[5])
        if lines and not matched:
            raise ValueError("no flake8 diagnostics recognised")
        return acc.result()

    return guarded("flake8", data, run)
