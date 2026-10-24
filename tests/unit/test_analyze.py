"""Chunk analysis against real Ollama and a real local HTTP server for scripted bad output.

Set PR_REVIEW_TEST_LLM_MODEL to pick the model (default ollama/qwen2.5-coder:1.5b). No mocks:
the repair paths use a real socket server that replies with scripted content, because a real
model cannot be made to emit invalid JSON on demand.
"""

import json
import os
import threading
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pr_review.core.settings import Settings
from pr_review.llm import analyze
from pr_review.llm.analyze import MAX_ATTEMPTS, analyze_chunk, build_messages, load_prompt
from pr_review.llm.provider import Provider
from pr_review.parsers.chunker import chunk_diff
from pr_review.review.models import FailedChunk, Finding, FindingSet

pytestmark = [pytest.mark.llm, pytest.mark.anyio]

OLLAMA = os.environ.get("PR_REVIEW_TEST_OLLAMA", "http://localhost:11434")
MODEL = os.environ.get("PR_REVIEW_TEST_LLM_MODEL", "ollama/qwen2.5-coder:1.5b")

DIFF = """diff --git a/app/stats.py b/app/stats.py
--- a/app/stats.py
+++ b/app/stats.py
@@ -1,3 +1,9 @@
 import sqlite3
 
+def average(values):
+    return sum(values) / len(values)
+
+def find_user(db, name):
+    return db.execute("SELECT * FROM users WHERE name = '" + name + "'")
+
 def other():
     pass
"""
CHUNK = chunk_diff(DIFF).chunks[0]  # added lines 4-5 and 7-9


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module", autouse=True)
def _ollama_up() -> None:
    try:
        urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3).read()
    except OSError as e:
        pytest.skip(f"Ollama unreachable at {OLLAMA}: {e}")


def provider(base: str) -> Provider:
    s = Settings.model_construct(
        llm_review_model=MODEL, llm_triage_model=MODEL, llm_fallback_models=[], ollama_api_base=base
    )
    return Provider(s, rng=lambda: 0.0)


class Scripted(ThreadingHTTPServer):
    """Real local server speaking Ollama's generate/chat API; replies are popped from `script`
    (the last one repeats)."""

    def __init__(self, script: list[str], by_category: dict[str, str] | None = None) -> None:
        super().__init__(("127.0.0.1", 0), _H)
        self.script = script
        self.by_category = by_category or {}  # reply keyed by the 'Category: x' in the prompt
        self.bodies: list[dict[str, object]] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _H(BaseHTTPRequestHandler):
    server: Scripted

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        req = json.loads(raw or b"{}")
        if "prompt" not in req and "messages" not in req:  # litellm's /api/show model probe
            text = ""
        else:
            self.server.bodies.append(req)
            s = self.server.script
            text = s.pop(0) if len(s) > 1 else s[0]
            prompt = json.dumps(req)
            for cat, forced in self.server.by_category.items():
                if f"Category: {cat}" in prompt:
                    text = forced
        reply = (
            {"message": {"role": "assistant", "content": text}}
            if "chat" in self.path
            else {"response": text}
        )
        body = json.dumps({**reply, "done": True, "prompt_eval_count": 5, "eval_count": 3}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def scripted() -> Iterator[type[Scripted]]:
    made: list[Scripted] = []

    def make(*script: str, **by_category: str) -> Scripted:
        made.append(Scripted(list(script), by_category))
        return made[-1]

    yield make  # type: ignore[misc]
    for m in made:
        m.shutdown()
        m.server_close()


def finding(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "severity": "MAJOR", "category": "correctness", "file_path": "app/stats.py",
        "line_start": 4, "line_end": 4, "summary": "Division by zero", "rationale": "empty list",
        "suggestion": None, "confidence": 0.9,
    }  # fmt: skip
    return {**base, **kw}


def reply(*fs: dict[str, object]) -> str:
    return json.dumps({"findings": list(fs)})


def test_chunk_layout() -> None:
    assert sorted(CHUNK.changed_new) == [3, 4, 5, 6, 7, 8]


def test_prompts_load_versioned_and_delimited() -> None:
    for name in ("system", "correctness", "security", "performance", "style"):
        assert load_prompt(name).strip()
    msgs = build_messages(CHUNK, "security")
    assert msgs[0]["role"] == "system" and "JSON only" in msgs[0]["content"]
    user = msgs[1]["content"]
    assert "<<<UNTRUSTED_DIFF>>>" in user and "app/stats.py" in user and "3-8" in user
    assert "SQL" in user and "{{" not in user


def test_finding_schema_strict() -> None:
    Finding.model_validate(finding())
    for bad in (
        finding(severity="BLOCKER"), finding(line_end=2), finding(confidence=1.5),
        finding(extra=1), finding(line_start=0),
    ):  # fmt: skip
        with pytest.raises(ValueError):
            Finding.model_validate(bad)


async def test_real_ollama_returns_valid_anchored_findingset() -> None:
    res = await analyze_chunk(provider(OLLAMA), CHUNK, ["correctness", "security"], None)
    assert isinstance(res, FindingSet), res
    assert res.chunk_id == CHUNK.id and res.schema_version == 1
    for f in res.findings:
        assert f.file_path == CHUNK.path
        assert f.line_start in CHUNK.changed_new and f.line_end >= f.line_start
        assert f.line_end in CHUNK.changed_new
    print(
        f"\nreal model findings: {[(f.severity, f.category, f.line_start) for f in res.findings]}"
    )


async def test_repair_invalid_then_valid(scripted) -> None:  # type: ignore[no-untyped-def]
    srv = scripted("sorry, no json", '{"findings": [{"severity": "HUGE"}]}',
                   "Here you go:\n```json\n" + reply(finding()) + "\n```")  # fmt: skip
    res = await analyze_chunk(provider(srv.url), CHUNK, ["correctness"], None)
    assert isinstance(res, FindingSet) and len(res.findings) == 1
    assert len(srv.bodies) == 3
    assert "invalid" in json.dumps(srv.bodies[2])  # the validation error was fed back


async def test_always_invalid_fails_chunk_after_bounded_attempts(scripted) -> None:  # type: ignore[no-untyped-def]
    srv = scripted("not json at all")
    res = await analyze_chunk(provider(srv.url), CHUNK, ["correctness", "style"], None)
    assert isinstance(res, FailedChunk)
    assert res.chunk_id == CHUNK.id and res.attempts == 2 * MAX_ATTEMPTS
    assert len(srv.bodies) == 2 * MAX_ATTEMPTS
    assert "correctness" in res.reason and "style" in res.reason


async def test_one_failed_category_keeps_the_others(scripted) -> None:  # type: ignore[no-untyped-def]
    srv = scripted("x", security="not json at all", correctness=reply(finding()))
    res = await analyze_chunk(provider(srv.url), CHUNK, ["correctness", "security"], None)
    assert isinstance(res, FindingSet)
    assert [f.category for f in res.findings] == ["correctness"]
    assert [(c.category, c.attempts) for c in res.failed_categories] == [("security", MAX_ATTEMPTS)]
    assert res.failed_categories[0].reason
    assert len(srv.bodies) == 1 + MAX_ATTEMPTS


async def test_defaulted_confidence_is_marked(scripted) -> None:  # type: ignore[no-untyped-def]
    no_conf = {k: v for k, v in finding().items() if k != "confidence"}
    srv = scripted(reply(no_conf, finding(line_start=6, line_end=6, confidence_defaulted=True)))
    res = await analyze_chunk(provider(srv.url), CHUNK, ["correctness"], None)
    assert isinstance(res, FindingSet)
    assert [(f.confidence, f.confidence_defaulted) for f in res.findings] == [
        (0.5, True),
        (0.9, False),
    ]


async def test_out_of_chunk_findings_rejected_and_counted(scripted) -> None:  # type: ignore[no-untyped-def]
    srv = scripted(reply(
        finding(),
        finding(line_start=99, line_end=99),  # not in the diff
        finding(line_start=1, line_end=1),  # context line, not changed
        finding(file_path="other.py"),
        finding(line_start=6, line_end=50),  # end clamped to last changed line
    ))  # fmt: skip
    res = await analyze_chunk(provider(srv.url), CHUNK, ["correctness"], None)
    assert isinstance(res, FindingSet)
    assert [(f.line_start, f.line_end) for f in res.findings] == [(4, 4), (6, 8)]
    assert res.rejected == 3


async def test_category_and_severity_filtering(scripted) -> None:  # type: ignore[no-untyped-def]
    srv = scripted(reply(
        finding(severity="critical"),  # normalized to CRITICAL
        finding(severity="MINOR"),
        finding(severity="INFO"),
        finding(confidence=7),  # clamped to 1
    ))  # fmt: skip
    res = await analyze_chunk(provider(srv.url), CHUNK, ["correctness"], None, min_severity="MAJOR")
    assert isinstance(res, FindingSet)
    assert [f.severity for f in res.findings] == ["CRITICAL", "MAJOR"]
    assert res.findings[1].confidence == 1.0


async def test_text_is_redacted(scripted) -> None:  # type: ignore[no-untyped-def]
    tok = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2"
    srv = scripted(reply(finding(rationale=f"token {tok} leaked", suggestion=f"rotate {tok}")))
    res = await analyze_chunk(provider(srv.url), CHUNK, ["correctness"], None)
    assert isinstance(res, FindingSet)
    f = res.findings[0]
    assert tok not in f.rationale and f.suggestion is not None and tok not in f.suggestion


def test_extraction_prose_and_fences() -> None:
    assert analyze._parse("Sure! " + reply(finding()) + " hope that helps", "correctness").findings
    assert analyze._parse("[]", "correctness").findings == []


def test_unescaped_quotes_repaired() -> None:
    bad = reply(finding(suggestion="X")).replace(
        '"suggestion": "X"', '"suggestion": "use "?" instead"'
    )
    bad = bad.replace(", ", ",\n")  # one field per line, as models print it
    assert analyze._parse(bad, "correctness").findings[0].suggestion == 'use "?" instead'


def test_category_forced_and_severity_synonyms_normalized() -> None:
    fs = [
        finding(category="bogus", severity="high"), finding(severity="Low"),
        finding(severity="note"), finding(severity="critical"), finding(category=123),
    ]  # fmt: skip
    got = analyze._parse(reply(*fs), "security").findings
    assert [f.category for f in got] == ["security"] * 5
    assert [f.severity for f in got] == ["MAJOR", "MINOR", "INFO", "CRITICAL", "MAJOR"]
