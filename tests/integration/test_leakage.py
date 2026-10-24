"""AC-19 leakage test on a real run: GitHub App on the sandbox repo, Postgres container, Docker
sandbox and Ollama (see test_stages.py). The PR carries canary source lines, seeded secrets built
at runtime and a failing test that prints a canary. After the full pipeline, a pg_dump, every log
record and stderr, the temp dirs, the leftover Docker resources and the posted review are searched.
"""

# ruff: noqa: F811  (shared pytest fixtures are imported, then requested by name)

import logging
import random
import re
import string
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

# fixtures and helpers are shared with the stage tests
from test_stages import (  # noqa: F401
    _WHY,
    SANDBOX_LABEL,
    Env,
    _docker,
    assert_clean_up,
    bot_reviews,
    drive,
    engine,
    env,
    factory,
    inline_comments,
    load,
    loop,
    new_job,
    open_pr,
    repo_files,
    workspaces,
)

from pr_review.core.logging import JsonFormatter
from pr_review.storage.sanitize import leaks

pytestmark = [pytest.mark.github, pytest.mark.llm, pytest.mark.docker]
pytestmark.append(pytest.mark.skipif(bool(_WHY), reason=_WHY or "x"))

_ALNUM = string.ascii_letters + string.digits
_LOGGERS = ("", "LiteLLM", "LiteLLM Router", "LiteLLM Proxy")


def _rand(n: int, alphabet: str = _ALNUM) -> str:
    return "".join(random.SystemRandom().choice(alphabet) for _ in range(n))


class Canaries:
    def __init__(self) -> None:
        u = uuid.uuid4().hex
        self.src_names = [f"canary_src_{u}_{i}" for i in range(3)]
        self.literal = f"canary-literal-{u}-{_rand(6)}"
        self.out = f"canary_out_{u}"
        self.gh_token = "gh" + "p_" + _rand(36)
        self.aws_key = "AK" + "IA" + _rand(16, string.ascii_uppercase + string.digits)
        self.password = _rand(32)
        self.pem_body = [_rand(64, _ALNUM + "+/") for _ in range(4)]
        self.pem = "\n".join(
            [
                "-----BEGIN " + "RSA PRIVATE KEY-----",
                *self.pem_body,
                "-----END RSA PRIVATE KEY-----",
            ]
        )
        self.source_lines = [
            f"def {self.src_names[0]}(x):",
            f"    {self.src_names[1]} = {self.literal!r}",
            f"    return {self.src_names[1]} + str({self.src_names[2]}(x))",
            f"def {self.src_names[2]}(x):",
            f"    return len(str(x)) * 7919  # {self.src_names[0]}",
        ]
        # the library's committed token and SQL line are source the run must never repeat
        billing = load("hardcoded_secret").files["billing_client.py"].splitlines()
        self.library_token = next(ln.split('"')[1] for ln in billing if "API_TOKEN =" in ln)
        sql = load("sql_injection")
        self.library_line = (
            sql.files["user_store.py"].splitlines()[sql.findings[0].line - 1].strip()
        )
        self.secrets = [
            self.gh_token,
            self.aws_key,
            self.password,
            *self.pem_body,
            self.library_token,
        ]

    @property
    def hard(self) -> list[str]:
        """Never allowed anywhere: secrets, whole source lines, the string literal."""
        return [self.literal, *self.secrets, *self.source_lines, self.library_line]

    @property
    def excerpts(self) -> list[str]:
        """Identifier names and test output: allowed only as capped, redacted excerpts (NFR-5)
        in finding text and stored test failure messages, never in logs or elsewhere."""
        return [*self.src_names, self.out]

    @property
    def all(self) -> list[str]:
        return [
            *self.src_names, self.literal, self.out, *self.secrets, *self.source_lines,
            self.library_line,
        ]  # fmt: skip

    def files(self) -> dict[str, str]:
        src = "\n".join(self.source_lines[:3]) + "\n\n\n" + "\n".join(self.source_lines[3:])
        secrets = (
            f'GITHUB_TOKEN = "{self.gh_token}"\nAWS_ACCESS_KEY_ID = "{self.aws_key}"\n'
            f'DB_PASSWORD = "{self.password}"\nPRIVATE_KEY = """{self.pem}"""\n'
        )
        tests = (
            "from leaky import " + self.src_names[0] + "\n\n\n"
            "def test_canary_output():\n"
            f"    print({self.out!r})\n"
            f"    assert {self.src_names[0]}(1) == {self.out!r}, {self.out!r}\n"
        )
        return {
            **repo_files("hardcoded_secret", "sql_injection", requirements=False),
            "leaky.py": src + "\n",
            "config_secrets.py": secrets,
            "test_leaky.py": tests,
            "requirements.txt": "pytest\ncoverage\n",
            ".pr-review.yml": "categories: [correctness]\n",
        }


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(JsonFormatter())  # the production formatter, with its redaction
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def log_capture() -> Iterator[_Collect]:
    h = _Collect()
    saved = []
    for name in _LOGGERS:
        lg = logging.getLogger(name)
        saved.append((lg, lg.level))
        lg.addHandler(h)
        if name == "":  # LiteLLM's loggers keep the production WARNING lock
            lg.setLevel(logging.DEBUG)
    yield h
    for lg, level in saved:
        lg.removeHandler(h)
        lg.setLevel(level)


def _pg_dump(engine: Engine) -> str:
    port = engine.url.port
    cid = _docker("ps", "-q", "--filter", f"publish={port}").stdout.split()[0]
    out = subprocess.run(
        ["docker", "exec", cid, "pg_dump", "-U", "postgres", "--inserts", "postgres"],
        capture_output=True, text=True, check=True, timeout=120,
    )  # fmt: skip
    return out.stdout


def _tmp_entries() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).iterdir()}


def _strings(obj: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, f"{path}/{k}")
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v, path)


def _stored_text_problems(env: Env, c: Canaries) -> list[str]:
    """Every text/JSON value in findings and events: capped, short, and free of raw names/output.
    Identifier names may appear only in finding text; test output only in a failure message."""
    with env.factory() as s:
        rows = [
            (f"findings.{col}", val)
            for col, val in s.execute(
                text(
                    "select 'summary', summary from findings union all "
                    "select 'rationale', rationale from findings union all "
                    "select 'suggestion', suggestion from findings"
                )  # fmt: skip
            )
            if val is not None
        ]
        for (payload,) in s.execute(text("select payload from events")):
            rows += [(f"events{p}", v) for p, v in _strings(payload)]
    bad = []
    for where, val in rows:
        if len(val) > 500 or val.count("\n") + 1 > 3:
            bad.append(f"{where}: over cap")
        findings_text = where.startswith("findings.") or "/findings/" in where
        if leaks(val, c.src_names) and not findings_text:
            bad.append(f"{where}: identifier name outside finding text")
        if leaks(val, [c.out]) and not where.endswith("/message"):
            bad.append(f"{where}: test output outside a failure message")
    return bad


def test_full_run_leaks_nothing(
    env: Env, engine: Engine, open_pr: Any, workspaces: Path, log_capture: _Collect,
    capfd: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    c = Canaries()
    pr = open_pr(c.files())
    job_id = new_job(env, pr)
    before = _tmp_entries()

    assert drive(env, job_id) == "succeeded"

    problems: dict[str, list[str]] = {}

    dump = _pg_dump(engine)
    assert "CREATE TABLE" in dump and str(job_id) in dump  # the dump really holds the run
    for table in ("test_runs", "findings", "llm_calls", "events"):
        assert f"INSERT INTO public.{table} " in dump, table
    problems["db dump"] = leaks(dump, c.hard)
    problems["db dump (names/output outside findings/events)"] = [
        x
        for ln in dump.splitlines()
        if not ln.startswith(("INSERT INTO public.findings ", "INSERT INTO public.events "))
        for x in leaks(ln, c.excerpts)
    ]
    problems["stored text"] = _stored_text_problems(env, c)

    stderr = capfd.readouterr()
    logs = "\n".join(log_capture.lines)
    problems["logs"] = leaks(logs, c.all)
    problems["stdout/stderr"] = leaks(stderr.out + stderr.err, c.all)

    reviews = bot_reviews(env, pr)
    assert len(reviews) == 1
    posted = reviews[0]["body"]
    for cm in inline_comments(env, pr):
        # a line from the commented source is allowed only inside a suggestion block
        posted += "\n" + re.sub(r"```suggestion.*?```", "", cm["body"], flags=re.S)
    problems["github review"] = leaks(posted, c.hard)

    found = {k: v for k, v in problems.items() if v}
    where = [ln[:300] for ln in dump.splitlines() if leaks(ln, c.all)]
    assert found == {}, f"leaked canaries per surface; db rows: {where}"

    assert_clean_up(env, workspaces)
    assert {n for n in _tmp_entries() - before if "pr-review" in n or "prr" in n} == set()
    assert _docker("ps", "-aq", "--filter", SANDBOX_LABEL).stdout.split() == []
