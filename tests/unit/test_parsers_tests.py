import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pr_review.parsers import coverage as C
from pr_review.parsers import tests as P

JEST = json.dumps(
    {
        "testResults": [
            {
                "name": "/w/src/a.test.js",
                "startTime": 0,
                "endTime": 2000,
                "assertionResults": [
                    {"fullName": "ok", "status": "passed", "failureMessages": []},
                    {
                        "fullName": "bad",
                        "status": "failed",
                        "location": {"line": 7},
                        "failureMessages": ["boom ghp_" + "a" * 30],
                    },
                    {"fullName": "skip", "status": "pending", "failureMessages": []},
                ],
            }
        ]
    }
)

JUNIT = """<testsuites><testsuite>
<testcase classname="t.A" name="ok" time="0.5"/>
<testcase classname="t.A" name="bad" file="t/a.py" line="3" time="0.5">
<failure message="nope"/></testcase>
<testcase classname="t.A" name="err"><error message="e"/></testcase>
<testcase classname="t.A" name="sk"><skipped/></testcase></testsuite></testsuites>"""

PYJSON = json.dumps(
    {
        "duration": 1.5,
        "tests": [
            {"nodeid": "t/a.py::ok", "outcome": "passed"},
            {
                "nodeid": "t/a.py::bad",
                "outcome": "failed",
                "call": {"outcome": "failed", "crash": {"lineno": 9, "message": "assert 1 == 2"}},
            },
            {"nodeid": "t/a.py::sk", "outcome": "skipped"},
        ],
    }
)

GO = "\n".join(
    json.dumps(e)
    for e in [
        {"Action": "output", "Package": "p", "Test": "TestA", "Output": "    a_test.go:12: bad\n"},
        {"Action": "fail", "Package": "p", "Test": "TestA"},
        {"Action": "pass", "Package": "p", "Test": "TestB"},
        {"Action": "skip", "Package": "p", "Test": "TestC"},
        {"Action": "fail", "Package": "p", "Elapsed": 0.25},
    ]
)

CARGO = """running 3 tests
test a::ok ... ok
test a::bad ... FAILED
test a::ign ... ignored

failures:

---- a::bad stdout ----
thread 'a::bad' panicked at src/lib.rs:10:5:
assertion failed

failures:
    a::bad

test result: FAILED. 1 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.02s
"""

LCOV = "SF:/w/src/a.py\nDA:1,1\nDA:2,0\nDA:3,0\nDA:4,5\nend_of_record\n"
COBERTURA = (
    '<coverage><packages><package><classes><class filename="src/a.py"><lines>'
    '<line number="1" hits="1"/><line number="2" hits="0"/></lines></class>'
    "</classes></package></packages></coverage>"
)
COVJSON = json.dumps(
    {"files": {"src/a.py": {"executed_lines": [1, 2], "missing_lines": [3, 4, 6]}}}
)
GOCOVER = "mode: set\nm/a.go:1.1,3.2 2 1\nm/a.go:4.1,5.2 1 0\n"

BOMB = (
    '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
    "<testsuite>&b;</testsuite>"
)


def test_jest_mixed_totals_and_redaction() -> None:
    r = P.parse_jest(JEST, "/w")
    assert isinstance(r, P.TestRunResult)
    assert (r.passed, r.failed, r.skipped, r.errored, r.duration) == (1, 1, 1, 0, 2.0)
    f = r.failures[0]
    assert (f.test_id, f.file, f.line) == ("bad", "src/a.test.js", 7)
    assert "ghp_" not in f.message
    assert r.schema_version == 2


def test_junit() -> None:
    r = P.parse_junit(JUNIT)
    assert isinstance(r, P.TestRunResult)
    assert (r.passed, r.failed, r.skipped, r.errored, r.duration) == (1, 1, 1, 1, 1.0)
    assert r.failures[0].file == "t/a.py" and r.failures[0].line == 3


def test_pytest_json() -> None:
    r = P.parse_pytest_json(PYJSON)
    assert isinstance(r, P.TestRunResult)
    assert (r.passed, r.failed, r.skipped) == (1, 1, 1)
    assert r.failures[0].line == 9 and r.failures[0].file == "t/a.py"


def test_go_test() -> None:
    r = P.parse_go_test(GO)
    assert isinstance(r, P.TestRunResult)
    assert (r.passed, r.failed, r.skipped, r.duration) == (1, 1, 1, 0.25)
    assert (r.failures[0].file, r.failures[0].line) == ("a_test.go", 12)


def test_cargo() -> None:
    r = P.parse_cargo_test(CARGO)
    assert isinstance(r, P.TestRunResult)
    assert (r.passed, r.failed, r.skipped) == (1, 1, 1)
    assert (r.failures[0].file, r.failures[0].line) == ("src/lib.rs", 10)


def test_coverage_formats() -> None:
    lc = C.parse_lcov(LCOV, "/w")
    assert isinstance(lc, C.CoverageResult)
    assert lc.files[0].path == "src/a.py"
    assert lc.files[0].covered == ((1, 1), (4, 4)) and lc.files[0].uncovered == ((2, 3),)
    cb = C.parse_cobertura(COBERTURA)
    assert isinstance(cb, C.CoverageResult) and (cb.covered_lines, cb.uncovered_lines) == (1, 1)
    cj = C.parse_coverage_json(COVJSON)
    assert isinstance(cj, C.CoverageResult)
    assert cj.files[0].uncovered == ((3, 4), (6, 6)) and cj.covered_lines == 2
    gc = C.parse_go_cover(GOCOVER)
    assert isinstance(gc, C.CoverageResult)
    assert (gc.covered_lines, gc.uncovered_lines) == (3, 2)


@pytest.mark.parametrize(
    "fn,bad",
    [
        (P.parse_jest, '{"testResults": [{"name"'),
        (P.parse_jest, "[]"),
        (P.parse_junit, "<testsuite><testcase"),
        (P.parse_junit, "<html/>"),
        (P.parse_pytest_json, '{"tests": 3}'),
        (P.parse_go_test, "not json"),
        (P.parse_cargo_test, "running 1 test\ntest a ... ok\n"),
        (C.parse_lcov, "SF:a.py\nDA:1,1\n"),
        (C.parse_lcov, "DA:x,y"),
        (C.parse_cobertura, "<coverage><class"),
        (C.parse_coverage_json, '{"files": []}'),
        (C.parse_go_cover, "garbage"),
        (P.parse_jest, b""),
    ],
)
def test_malformed_gives_parse_error(fn, bad) -> None:  # type: ignore[no-untyped-def]
    r = fn(bad)
    assert isinstance(r, P.ParseError)
    assert r.error_class and len(r.message) <= 200


@pytest.mark.parametrize("fn", [P.parse_junit, C.parse_cobertura])
def test_xxe_and_entity_expansion_rejected_fast(fn) -> None:  # type: ignore[no-untyped-def]
    xxe = '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><testsuite>&e;</testsuite>'
    for payload in (BOMB, xxe):
        t = time.monotonic()
        assert isinstance(fn(payload), P.ParseError)
        assert time.monotonic() - t < 1


def test_oversized_input_rejected() -> None:
    r = P.parse_jest(" " * (P.MAX_INPUT_BYTES + 1))
    assert isinstance(r, P.ParseError) and r.error_class == "ValueError"


def test_case_cap() -> None:
    cases = [{"fullName": f"t{i}", "status": "failed"} for i in range(P.MAX_CASES + 5)]
    r = P.parse_jest(json.dumps({"testResults": [{"name": "a.js", "assertionResults": cases}]}))
    assert isinstance(r, P.TestRunResult)
    assert r.truncated and r.failed == P.MAX_CASES


def test_message_truncated() -> None:
    msg = "x" * 5000
    r = P.parse_jest(
        json.dumps(
            {
                "testResults": [
                    {
                        "name": "a.js",
                        "assertionResults": [
                            {"fullName": "t", "status": "failed", "failureMessages": [msg]}
                        ],
                    }
                ]
            }
        )
    )
    assert isinstance(r, P.TestRunResult)
    assert len(r.failures[0].message) == P.MAX_MESSAGE and r.failures[0].message_truncated


def test_file_cap() -> None:
    lcov = "".join(f"SF:f{i}.py\nDA:1,1\nend_of_record\n" for i in range(C.MAX_FILES + 3))
    r = C.parse_lcov(lcov)
    assert isinstance(r, C.CoverageResult)
    assert r.truncated and len(r.files) == C.MAX_FILES


def test_path_traversal_dropped_with_warning() -> None:
    lcov = (
        "SF:../../etc/passwd\nDA:1,1\nend_of_record\nSF:/etc/shadow\nDA:1,1\nend_of_record\n"
        "SF:/w/./src/../ok.py\nDA:1,1\nend_of_record\n"
    )
    r = C.parse_lcov(lcov, "/w")
    assert isinstance(r, C.CoverageResult)
    assert [f.path for f in r.files] == ["ok.py"] and len(r.warnings) == 2
    j = P.parse_junit(
        '<testsuite><testcase name="a" file="../x.py"><failure message="m"/></testcase></testsuite>'
    )
    assert isinstance(j, P.TestRunResult)
    assert j.failures[0].file is None and j.warnings


def _locs(r: object) -> list[tuple[str, int]]:
    assert isinstance(r, P.TestRunResult)
    return [(x.file, x.line) for x in r.failures[0].locations]


def test_locations_real_pytest_run_junit_and_json_report(tmp_path: Path) -> None:
    """Real pytest run (traceback through two project frames, plus a stdlib frame)."""
    (tmp_path / "lib.py").write_text("import json\n\n\ndef boom(x):\n    return json.loads(x)\n")
    (tmp_path / "test_lib.py").write_text(
        "from lib import boom\n\n\ndef test_boom():\n    boom('{')\n"
    )
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=long",
         f"--junitxml={tmp_path / 'j.xml'}"],
        cwd=tmp_path, capture_output=True, check=False,
    )  # fmt: skip
    r = P.parse_junit((tmp_path / "j.xml").read_text(), str(tmp_path))
    assert _locs(r) == [("lib.py", 5), ("test_lib.py", 5)]  # innermost first, stdlib dropped


def test_locations_pytest_json_frames() -> None:
    doc = {
        "tests": [
            {
                "nodeid": "t/a.py::x",
                "outcome": "failed",
                "call": {
                    "outcome": "failed",
                    "crash": {"path": "/w/src/m.py", "lineno": 4, "message": "E"},
                    "traceback": [
                        {"path": "t/a.py", "lineno": 9},
                        {"path": "/w/src/m.py", "lineno": 4},
                        {"path": "/w/.venv/lib/python3.12/site-packages/x.py", "lineno": 1},
                        {"path": "/elsewhere/y.py", "lineno": 2},
                    ],
                },
            }
        ]
    }
    assert _locs(P.parse_pytest_json(json.dumps(doc), "/w")) == [("src/m.py", 4), ("t/a.py", 9)]


# Samples (no node, go or cargo toolchain in this environment): shaped like the real tools' output.
def test_locations_jest_go_cargo_samples() -> None:
    jest = {
        "testResults": [
            {
                "name": "/w/src/a.test.js",
                "assertionResults": [
                    {
                        "fullName": "bad",
                        "status": "failed",
                        "failureMessages": [
                            "Error: nope\n    at f (/w/src/a.js:3:9)\n"
                            "    at Object.<anonymous> (/w/src/a.test.js:7:5)\n"
                            "    at run (/w/node_modules/jest-circus/build/run.js:1:1)"
                        ],
                    }
                ],
            }
        ]
    }
    assert _locs(P.parse_jest(json.dumps(jest), "/w")) == [("src/a.js", 3), ("src/a.test.js", 7)]
    events = [
        {"Action": "output", "Package": "p", "Test": "T", "Output": "    a_test.go:12: bad\n"},
        {"Action": "fail", "Package": "p", "Test": "T"},
    ]
    assert _locs(P.parse_go_test("\n".join(map(json.dumps, events)))) == [("a_test.go", 12)]
    cargo = (
        "test t ... FAILED\n\nfailures:\n\n---- t stdout ----\n"
        "thread 't' panicked at src/lib.rs:12:5:\nboom\n\nfailures:\n    t\n\n"
        "test result: FAILED. 0 passed; 1 failed; 0 ignored; finished in 0.01s\n"
    )
    assert _locs(P.parse_cargo_test(cargo)) == [("src/lib.rs", 12)]


def test_locations_capped_and_escapes_dropped() -> None:
    trace = "\n".join(f"m{i}.py:{i + 1}: in f" for i in range(50)) + "\n../../etc/x.py:1: in g"
    locs = P.extract_locations(trace, "")
    assert len(locs) == P.MAX_LOCATIONS and all(".." not in x.file for x in locs)
