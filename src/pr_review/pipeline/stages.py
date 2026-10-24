"""Pipeline stages (FR-22, FR-23, NFR-10): fetch, detect, test, parse, analyse, aggregate, post.

Each stage is idempotent and persists a typed, versioned output (an append-only ``stage_output``
event, latest wins) so a resumed job continues from the last completed stage (lease.py contract).
Outputs hold metadata and findings only: never source, diffs, prompts or raw sandbox artifacts.
Anything needing the diff or the checkout (test, analyse, aggregate) re-fetches it, so nothing
sensitive survives between stages, and the diff hash recorded by ``fetch`` proves it is unchanged.
Raw sandbox artifacts live in the sandbox's per-run temp dir and are deleted there.

A permanently failed stage dead-letters the job; ``Pipeline.post_failure`` then posts a
summary-only review naming the stage. A stale head or an already posted review makes that a no-op.
"""

import asyncio
import hashlib
import json
import logging
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session, sessionmaker

from pr_review.connectors.base import CloneCredential, PullRequestRef
from pr_review.connectors.github.auth import GitHubAppAuth
from pr_review.connectors.github.client import GitHubClient, PullRequest
from pr_review.connectors.github.posting import Posted, ReviewPoster
from pr_review.core.repo_config import RepoConfig, RepoLimits, load_repo_config
from pr_review.core.settings import Settings
from pr_review.llm.analyze import analyze_chunk
from pr_review.llm.budget import degraded_notice
from pr_review.llm.provider import BudgetExceeded, Provider
from pr_review.parsers.chunker import ChunkPlan, chunk_diff
from pr_review.parsers.context import attach_context
from pr_review.parsers.coverage import CoverageResult
from pr_review.parsers.tests import ParseError, TestRunResult
from pr_review.pipeline import state
from pr_review.pipeline.supersede import CANCELLED
from pr_review.review.aggregate import Counts, ReviewPlan, aggregate
from pr_review.review.correlate import correlate
from pr_review.review.models import FailedCategory, FailedChunk, Finding
from pr_review.review.static_checks import static_findings
from pr_review.review.summary import render_summary
from pr_review.runner.detect import Detection, Language, Project
from pr_review.runner.detect import detect as detect_projects
from pr_review.runner.install import install_deps
from pr_review.runner.sandbox import SandboxSpec
from pr_review.runner.select import TestOutcome, changed_paths, run_project_tests, strip_control
from pr_review.runner.workspace import Workspace, workspace
from pr_review.storage.events import EventPayload, EventRepository, register
from pr_review.storage.models import Comment, Event, Job
from pr_review.storage.models import Finding as FindingRow
from pr_review.storage.models import TestRun as TestRunRow
from pr_review.storage.sanitize import sanitize_obj, sanitize_opt, sanitize_text

log = logging.getLogger(__name__)

Stage = Callable[[uuid.UUID], Awaitable[None]]
MAX_OUTPUT_BYTES = 1_000_000
MAX_MESSAGE = 1000
GITHUB = "https://github.com"


class StageError(ValueError):
    """Permanent stage failure (a ValueError, so the retry policy dead-letters it). The message
    is written by this module and safe to show on the PR."""


@register
class StageOutputV1(EventPayload):
    event_type: ClassVar[str] = "stage_output"
    version: ClassVar[int] = 1
    data: dict[str, Any]

    @field_validator("data")
    @classmethod
    def _bounded(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(v)) > MAX_OUTPUT_BYTES:
            raise ValueError("stage output too large")
        return v


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    v: Literal[1] = 1  # payload schema version; a mismatch fails validation loudly


class FetchOut(_Out):
    pr: PullRequest
    diff_sha256: str
    diff_bytes: int


class DetectOut(_Out):
    detection: Detection
    config: RepoConfig
    config_notice: str | None = None


class RunOut(_Out):
    framework: str | None = None
    exit_code: int | None = None
    duration_s: float = 0.0
    ran_full: bool = False
    timed_out: bool = False
    notice: str | None = None
    warnings: tuple[str, ...] = ()
    tests: TestRunResult | None = None
    tests_error: ParseError | None = None
    coverage: CoverageResult | None = None
    coverage_error: ParseError | None = None


class ParseOut(_Out):
    tests: TestRunResult | None = None
    coverage: CoverageResult | None = None
    tests_status: str = "no tests detected"
    notices: tuple[str, ...] = ()


class AnalyseOut(_Out):
    findings: tuple[Finding, ...] = ()  # static + model findings, exactly what correlate receives
    failed_categories: tuple[FailedCategory, ...] = ()
    failed_chunks: tuple[FailedChunk, ...] = ()
    notices: tuple[str, ...] = ()


class AggregateOut(_Out):
    counts: Counts  # the rendered review holds source-derived text: post re-renders, never reads it


class PostOut(_Out):
    outcome: Literal["posted", "duplicate", "stale", "cancelled"]
    review_id: int | None = None
    inline: int = 0
    unanchored: int = 0


POST_LOCK_WAIT_S = 600.0


class DbReviewStore:
    """ReviewStore backed by the comments table (kind=review|inline), one job's view."""

    # ponytail: sync DB calls on the event loop, like the rest of the pipeline; they are short.
    def __init__(self, factory: sessionmaker[Session], job_id: uuid.UUID) -> None:
        self._factory = factory
        self._job_id = job_id

    async def posted_review(self, repo: str, number: int, sha: str) -> int | None:
        with self._factory() as s:
            found = s.scalar(
                select(Comment.github_comment_id)
                .join(Job, Job.id == Comment.job_id)
                .where(
                    Job.repo == repo,
                    Job.pr_number == number,
                    Job.head_sha == sha,
                    Comment.kind == "review",
                )
                .limit(1)
            )
        return int(found) if found is not None else None

    @asynccontextmanager
    async def exclusive(self, repo: str, number: int, sha: str) -> AsyncIterator[None]:
        """Session-level advisory lock on (repo, PR, sha), polled so the loop never blocks."""
        key = int.from_bytes(
            hashlib.blake2b(f"{repo}#{number}@{sha}".encode(), digest_size=8).digest(),
            "big",
            signed=True,
        )
        bind = self._factory.kw["bind"]
        # a dedicated autocommit connection: the lock lives and dies with it
        with bind.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            deadline = time.monotonic() + POST_LOCK_WAIT_S
            while not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar():
                if time.monotonic() > deadline:
                    raise StageError("timed out waiting for another worker to finish posting")
                await asyncio.sleep(0.25)
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})

    async def record(
        self, repo: str, number: int, sha: str, review_id: int, comment_ids: tuple[int, ...]
    ) -> None:
        with self._factory() as s:
            for kind, gid in [("review", review_id), *(("inline", c) for c in comment_ids)]:
                if s.scalar(select(Comment.id).where(Comment.github_comment_id == gid)) is None:
                    s.add(Comment(job_id=self._job_id, github_comment_id=gid, kind=kind))
            s.commit()


class _JobInfo(BaseModel):
    repo: str
    number: int
    head_sha: str
    installation_id: int
    status: str

    @property
    def ref(self) -> PullRequestRef:
        return PullRequestRef(self.installation_id, self.repo, self.number)


def _scrub_tests(t: TestRunResult) -> TestRunResult:
    """Control characters and ANSI out of every free-text field (they reach PR markdown)."""
    failures = tuple(
        f.model_copy(
            update={
                "test_id": strip_control(f.test_id, 512),
                "file": strip_control(f.file, 512) if f.file else None,
                "message": strip_control(f.message, MAX_MESSAGE),
            }
        )
        for f in t.failures
    )
    warnings = tuple(strip_control(w, 300) for w in t.warnings)
    return t.model_copy(update={"failures": failures, "warnings": warnings})


def _sandbox_tests(
    root: Path,
    project: Project | None,
    changed: tuple[str, ...],
    cfg: RepoConfig,
    spec: SandboxSpec,
    timeout_s: float,
    index_url: str | None = None,
) -> TestOutcome:
    """Blocking: install pip dependencies (allowlisted egress only), then run the selected tests
    with no network. A failing install is a permanent stage failure."""
    base, deps = root, None
    try:
        if (
            not cfg.tests_disabled
            and project is not None
            and project.language == Language.PYTHON
            and project.root in ("", ".")
            and (root / "requirements.txt").is_file()
        ):
            deps = install_deps(root, spec=spec, timeout_s=timeout_s, index_url=index_url)
            if not deps.ok or deps.workspace is None:
                raise StageError(strip_control(deps.notice, 200))
            base = deps.workspace
        return run_project_tests(base, project, changed, cfg, spec=spec, timeout_s=timeout_s)
    finally:
        if deps is not None and deps.workspace is not None:
            shutil.rmtree(deps.workspace, ignore_errors=True)


class Pipeline:
    """Stage handlers bound to one worker process: settings, DB, ONE shared LLM Provider and a
    GitHub client."""

    def __init__(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        provider: Provider,
        http: httpx.AsyncClient,
    ) -> None:
        self.cfg = settings
        self.factory = factory
        self.provider = provider
        self.http = http
        self.auth = GitHubAppAuth(settings.github_app_id, settings.github_private_key, http)
        self.github = GitHubClient(
            self.auth,
            http,
            max_diff_bytes=settings.github_max_diff_bytes,
            max_files=settings.github_max_files,
        )
        root = Path(settings.workspace_root) if settings.workspace_root else None
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
        self.root = root

    def handlers(self) -> dict[str, Stage]:
        return {
            "fetch": self.fetch,
            "detect": self.detect,
            "test": self.test,
            "parse": self.parse,
            "analyse": self.analyse,
            "aggregate": self.aggregate,
            "post": self.post,
        }

    # ---------------------------------------------------------------- persistence helpers

    def _job(self, job_id: uuid.UUID) -> _JobInfo:
        with self.factory() as s:
            j = s.get(Job, job_id)
            if j is None:
                raise StageError("job not found")
            return _JobInfo(
                repo=j.repo,
                number=j.pr_number,
                head_sha=j.head_sha,
                installation_id=j.installation_id,
                status=j.status,
            )

    def _save(self, job_id: uuid.UUID, stage: str, out: BaseModel) -> None:
        with self.factory() as s:
            EventRepository(s).append(
                job_id, StageOutputV1(data=sanitize_obj(out.model_dump(mode="json"))), stage
            )
            s.commit()

    def _load[M: BaseModel](self, job_id: uuid.UUID, stage: str, model: type[M]) -> M:
        with self.factory() as s:
            row = s.scalars(
                select(Event)
                .where(Event.job_id == job_id, Event.type == "stage_output", Event.stage == stage)
                .order_by(Event.id.desc())
                .limit(1)
            ).first()
        if row is None:
            raise StageError(f"output of stage {stage} is missing")
        return model.model_validate(row.payload["data"])

    # ---------------------------------------------------------------- GitHub helpers

    @asynccontextmanager
    async def _checkout(self, job: _JobInfo, pr: PullRequest) -> AsyncIterator[Workspace]:
        token = (await self.auth.token(job.installation_id)).reveal()
        async with workspace(
            f"{GITHUB}/{job.repo}.git",
            pr.head_sha,
            pr.base_sha,
            credential=CloneCredential("x-access-token", token),
            root=self.root,
            max_bytes=self.cfg.workspace_max_bytes,
        ) as ws:
            yield ws

    async def _diff(self, job: _JobInfo, fetched: FetchOut) -> str:
        diff = await self.github.fetch_diff(job.ref)
        if hashlib.sha256(diff.encode()).hexdigest() != fetched.diff_sha256:
            raise StageError("pull request diff changed after fetch")
        return diff

    def _poster(self, job_id: uuid.UUID) -> ReviewPoster:
        return ReviewPoster(
            self.auth,
            self.http,
            self.github,
            request_changes_on_critical=self.cfg.request_changes_on_critical,
            store=DbReviewStore(self.factory, job_id),
        )

    # ---------------------------------------------------------------- stages

    async def fetch(self, job_id: uuid.UUID) -> None:
        job = self._job(job_id)
        pr = await self.github.fetch_pull_request(job.ref)
        if pr.head_sha != job.head_sha:
            raise StageError("pull request head moved on; this commit is stale")
        diff = await self.github.fetch_diff(job.ref)
        async with self._checkout(job, pr):  # the exact head/base must check out, or fail early
            pass
        digest = hashlib.sha256(diff.encode()).hexdigest()
        self._save(job_id, "fetch", FetchOut(pr=pr, diff_sha256=digest, diff_bytes=len(diff)))

    async def detect(self, job_id: uuid.UUID) -> None:
        job = self._job(job_id)
        pr = self._load(job_id, "fetch", FetchOut).pr
        limits = RepoLimits(self.cfg.test_timeout_seconds)
        async with self._checkout(job, pr) as ws:
            detection = await asyncio.to_thread(detect_projects, ws.path)
            config, notice = await asyncio.to_thread(load_repo_config, ws.path, limits)
        self._save(
            job_id, "detect", DetectOut(detection=detection, config=config, config_notice=notice)
        )

    async def test(self, job_id: uuid.UUID) -> None:
        job = self._job(job_id)
        fetched = self._load(job_id, "fetch", FetchOut)
        det = self._load(job_id, "detect", DetectOut)
        changed = changed_paths(await self._diff(job, fetched))
        project = det.detection.projects[0] if det.detection.projects else None  # one per run
        spec = SandboxSpec(
            mem_bytes=self.cfg.sandbox_memory_bytes,
            cpus=self.cfg.sandbox_cpus,
            max_repo_bytes=self.cfg.sandbox_max_repo_bytes,
        )
        async with self._checkout(job, fetched.pr) as ws:
            outcome = await asyncio.to_thread(
                _sandbox_tests, ws.path, project, changed, det.config, spec,
                self.cfg.test_timeout_seconds, self.cfg.sandbox_pip_index_url,
            )  # fmt: skip
        last = outcome.runs[-1] if outcome.runs else None
        tests, cov = outcome.tests, outcome.coverage
        out = RunOut(
            framework=outcome.plan.command.framework if outcome.plan.command else None,
            exit_code=last.exit_code if last else None,
            duration_s=outcome.duration_s,
            ran_full=outcome.ran_full,
            timed_out="timed out" in (outcome.notice or ""),
            notice=outcome.notice,
            warnings=outcome.warnings,
            tests=tests if isinstance(tests, TestRunResult) else None,
            tests_error=tests if isinstance(tests, ParseError) else None,
            coverage=cov if isinstance(cov, CoverageResult) else None,
            coverage_error=cov if isinstance(cov, ParseError) else None,
        )
        self._save(job_id, "test", out)

    async def parse(self, job_id: uuid.UUID) -> None:
        run = self._load(job_id, "test", RunOut)
        tests = _scrub_tests(run.tests) if run.tests else None
        notices = [*run.warnings]
        if run.tests_error:
            notices.append(
                f"test results unreadable: {strip_control(run.tests_error.message, 120)}"
            )
        if run.coverage_error:
            notices.append("coverage report unreadable")
        if tests is None:
            status = run.notice or "no test results produced"
        else:
            status = ""
            if run.notice:
                notices.append(run.notice)
        out = ParseOut(
            tests=tests, coverage=run.coverage, tests_status=status, notices=tuple(notices)
        )
        with self.factory() as s:  # idempotent: replace this job's row
            s.execute(delete(TestRunRow).where(TestRunRow.job_id == job_id))
            if tests is not None:
                s.add(
                    TestRunRow(
                        job_id=job_id,
                        framework=tests.framework,
                        exit_code=run.exit_code,
                        passed=tests.passed,
                        failed=tests.failed,
                        skipped=tests.skipped,
                        duration_ms=int(tests.duration * 1000),
                        timed_out=run.timed_out,
                        summary=sanitize_obj({"failing": [f.test_id for f in tests.failures[:50]]}),
                    )
                )
            s.commit()
        self._save(job_id, "parse", out)

    async def analyse(self, job_id: uuid.UUID) -> None:
        job = self._job(job_id)
        fetched = self._load(job_id, "fetch", FetchOut)
        cfg = self._load(job_id, "detect", DetectOut).config
        diff = await self._diff(job, fetched)
        plan = chunk_diff(diff, ignore=cfg.ignore)
        async with self._checkout(job, fetched.pr) as ws:
            chunks = await asyncio.to_thread(attach_context, plan.chunks, ws)
        static = static_findings(diff, (), cfg)
        found: list[Finding] = []
        failed_cats: list[FailedCategory] = []
        failed_chunks: list[FailedChunk] = []
        notices: list[str] = []
        # ponytail: chunks run sequentially; fan out with a semaphore if latency matters.
        with self.factory() as s:
            events = EventRepository(s)
            for chunk in chunks:
                try:
                    res = await analyze_chunk(
                        self.provider, chunk, cfg.categories, job_id,
                        min_severity=cfg.min_severity, events=events,
                    )  # fmt: skip
                except BudgetExceeded as exc:  # degrade: static findings still go out
                    notices.append(degraded_notice(exc))
                    break
                finally:
                    s.commit()  # guard events
                if isinstance(res, FailedChunk):
                    failed_chunks.append(res)
                else:
                    found += res.findings
                    failed_cats += res.failed_categories
            s.execute(delete(FindingRow).where(FindingRow.job_id == job_id))
            for agent, items in (("static", static), ("llm", found)):
                s.add_all(
                    FindingRow(
                        job_id=job_id,
                        agent_type=agent,
                        severity=f.severity,
                        category=f.category,
                        summary=sanitize_text(f.summary),
                        file_path=f.file_path,
                        line_start=f.line_start,
                        line_end=f.line_end,
                        suggestion=sanitize_opt(f.suggestion),
                        confidence=f.confidence,
                        rationale=sanitize_opt(f.rationale),
                    )
                    for f in items
                )
            s.commit()
        out = AnalyseOut(
            findings=(*static, *found),
            failed_categories=tuple(failed_cats),
            failed_chunks=tuple(failed_chunks),
            notices=tuple(notices),
        )
        self._save(job_id, "analyse", out)

    async def _render(self, job_id: uuid.UUID) -> tuple[ReviewPlan, str]:
        job = self._job(job_id)
        fetched = self._load(job_id, "fetch", FetchOut)
        det = self._load(job_id, "detect", DetectOut)
        parsed = self._load(job_id, "parse", ParseOut)
        an = self._load(job_id, "analyse", AnalyseOut)
        plan = chunk_diff(await self._diff(job, fetched), ignore=det.config.ignore)
        corr = correlate(an.findings, parsed.tests, parsed.coverage, plan.chunks)
        notices = [
            *([det.config_notice] if det.config_notice else []),
            *parsed.notices,
            *an.notices,
        ]
        review = aggregate(
            [corr], plan,
            config=det.config,
            failed_categories=an.failed_categories,
            failed_chunks=an.failed_chunks,
            notices=notices,
            received=len(an.findings),
        )  # fmt: skip
        md = render_summary(
            review, parsed.tests, parsed.coverage, plan.chunks, tests_status=parsed.tests_status
        )
        return review, md

    async def aggregate(self, job_id: uuid.UUID) -> None:
        review, _ = await self._render(job_id)
        self._save(job_id, "aggregate", AggregateOut(counts=review.counts))

    async def post(self, job_id: uuid.UUID) -> None:
        job = self._job(job_id)
        if job.status in CANCELLED:  # superseded or cancelled: stop without posting
            self._save(job_id, "post", PostOut(outcome="cancelled"))
            return
        self._load(job_id, "aggregate", AggregateOut)  # the stage must have run
        if (await self.github.fetch_pull_request(job.ref)).head_sha != job.head_sha:
            self._save(job_id, "post", PostOut(outcome="stale"))  # re-rendering needs the old diff
            return
        plan, md = await self._render(job_id)
        res = await self._poster(job_id).post_review(job.ref, job.head_sha, plan, md)
        if isinstance(res, Posted):
            out = PostOut(
                outcome="posted",
                review_id=res.review_id,
                inline=len(res.comment_ids),
                unanchored=res.unanchored,
            )
        else:
            out = PostOut(outcome=res.reason, review_id=res.review_id)
        self._save(job_id, "post", out)

    # ---------------------------------------------------------------- partial failure

    async def post_failure(self, job_id: uuid.UUID, stage: str, reason: str) -> None:
        """Summary-only review for a dead-lettered job (FR-23, NFR-10). No inline comments, so
        nothing unverified is asserted; a stale head or an existing review makes it a no-op."""
        job = self._job(job_id)
        if job.status != state.DEAD_LETTER:
            return
        try:
            parsed: ParseOut | None = self._load(job_id, "parse", ParseOut)
        except (StageError, ValueError):
            parsed = None
        notice = f"Review incomplete: stage '{stage}' failed and was not retried further. {reason}"
        review = aggregate(
            [], ChunkPlan((), (), False, 0, ()), notices=[strip_control(notice, 500)], received=0
        ).model_copy(update={"truncated": True})  # renders "analysis was incomplete"
        md = render_summary(
            review,
            parsed.tests if parsed else None,
            parsed.coverage if parsed else None,
            tests_status=parsed.tests_status if parsed else "not run",
        )
        await self._poster(job_id).post_review(job.ref, job.head_sha, review, md)
