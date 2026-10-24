import json

import pytest

from pr_review.parsers import lint as L
from pr_review.parsers.tests import MAX_INPUT_BYTES, ParseError

SECRET = "ghp_" + "a" * 30

RUFF = json.dumps(
    [
        {
            "code": "F401",
            "message": "unused import " + SECRET,
            "filename": "/w/src/a.py",
            "location": {"row": 3, "column": 8},
            "end_location": {"row": 3, "column": 12},
            "fix": {"applicability": "safe"},
        },
        {
            "code": None,
            "message": "syntax",
            "filename": "b.py",
            "location": {"row": 1, "column": 1},
        },
        {"code": "E1", "message": "x", "filename": "/etc/passwd", "location": {"row": 1}},
        {"code": "E1", "message": "x", "filename": "../x.py", "location": {"row": 1}},
    ]
)

ESLINT = json.dumps(
    [
        {
            "filePath": "/w/a.js",
            "source": "secret code here",
            "messages": [
                {
                    "ruleId": "no-var",
                    "severity": 2,
                    "message": "m",
                    "line": 2,
                    "column": 3,
                    "endLine": 4,
                    "fix": {"range": [0, 1], "text": "let"},
                },
                {"ruleId": None, "severity": 1, "message": "w", "line": 1, "column": 1},
            ],
        }
    ]
)

FLAKE8 = "src/a.py:3:8: F401 unused\nsrc/a.py:9:1: W291 trailing\nsrc/a.py:4: [E902] io\n"


def test_ruff() -> None:
    r = L.parse_ruff(RUFF, "/w")
    assert isinstance(r, L.LintResult) and r.tool == "ruff"
    a, b = r.issues
    assert (a.file, a.line, a.column, a.end_line, a.rule, a.fix_available) == (
        "src/a.py",
        3,
        8,
        3,
        "F401",
        True,
    )
    assert b.rule == "invalid-syntax" and b.fix_available is False
    assert SECRET not in a.message
    assert len(r.warnings) == 2  # absolute outside root, and ../ escape


def test_eslint() -> None:
    r = L.parse_eslint(ESLINT, "/w")
    assert isinstance(r, L.LintResult)
    a, b = r.issues
    assert (a.file, a.severity, a.end_line, a.fix_available) == ("a.js", "error", 4, True)
    assert (b.severity, b.rule, b.fix_available) == ("warning", "parse-error", False)
    assert "secret code" not in r.model_dump_json()


def test_flake8_both_formats_and_severity() -> None:
    r = L.parse_flake8(FLAKE8)
    assert isinstance(r, L.LintResult)
    assert [(i.rule, i.severity, i.column) for i in r.issues] == [
        ("F401", "error", 8),
        ("W291", "warning", 1),
        ("E902", "error", 1),
    ]


def test_flake8_traversal_and_secret() -> None:
    r = L.parse_flake8(f"../../x.py:1:1: E1 a\nok.py:1:1: E1 {SECRET}\n")
    assert isinstance(r, L.LintResult)
    assert len(r.issues) == 1 and SECRET not in r.issues[0].message and r.warnings


@pytest.mark.parametrize(
    "fn,bad",
    [
        (L.parse_ruff, "{not json"),
        (L.parse_ruff, "{}"),
        (L.parse_ruff, "[1]"),
        (L.parse_eslint, "[{}]"),
        (L.parse_flake8, "garbage line"),
        (L.parse_ruff, b"x" * (MAX_INPUT_BYTES + 1)),
    ],
)
def test_malformed(fn, bad) -> None:  # type: ignore[no-untyped-def]
    assert isinstance(fn(bad), ParseError)


def test_empty_valid() -> None:
    for r in (L.parse_ruff("[]"), L.parse_eslint("[]"), L.parse_flake8("")):
        assert isinstance(r, L.LintResult) and not r.issues


def test_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(L, "MAX_ISSUES", 2)
    r = L.parse_flake8("a.py:1:1: E1 x\n" * 5)
    assert isinstance(r, L.LintResult) and len(r.issues) == 2 and r.truncated
    r = L.parse_flake8("a.py:1:1: E1 " + "y" * 2000)
    assert isinstance(r, L.LintResult) and len(r.issues[0].message) == L.MAX_LINT_MESSAGE
