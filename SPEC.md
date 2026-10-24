# Product specification — pr-review

> Status: draft. A coding agent must not implement product code until this specification is approved through Genesis.

## Problem

Enterprise engineering teams need pull requests reviewed quickly and consistently, but human review is slow and misses security and correctness defects, while naive AI reviewers hallucinate, ignore whether tests actually pass, and risk leaking proprietary code. pr-review is a commercial, self-hosted AI review agent: on each GitHub pull request it checks out the exact commit in an isolated sandbox, runs the project's tests and coverage, analyses the diff with an LLM across correctness, security, performance and maintainability, grounds findings in real test failures and uncovered lines, and posts inline comments plus a summary review. Reference design: https://www.antern.co/blogs/production-grade-ai-pr-review-agent/ (adopted: signed webhook ingress, idempotency, durable queue, typed findings, retries/fallbacks, append-only audit trail, selective high-value findings, injection guard; deliberately not adopted in v1: vector RAG, LangGraph, dashboard UI, HITL gate).

Desired outcome: every opened/updated PR receives a trustworthy review within minutes, no PR is silently lost or reviewed twice, and no customer code or secret is exposed outside the customer's own infrastructure and the configured LLM API.

## Users

- Reviewer/developer (PR author and reviewers): receives inline comments and a summary on their PR; needs low-noise, actionable, severity-ranked findings.
- Platform/DevOps engineer (operator): installs the GitHub App, deploys docker-compose, configures env vars, monitors queue/health, replays dead-lettered jobs.
- Security/compliance owner: needs assurance on data handling, sandboxing, audit history and secret redaction.
- Product buyer (enterprise engineering lead): needs cost visibility and reliability evidence.
- Affected: repository maintainers whose code is cloned and executed in sandboxes.

## Functional requirements

Intake and connector
- FR-1: The API exposes a GitHub webhook endpoint that verifies `X-Hub-Signature-256` (HMAC-SHA256, constant-time compare) on the raw body before any parsing or work; invalid or missing signatures are rejected with 401 and no job is created.
- FR-2: The webhook handler acts only on `pull_request` events (opened, synchronize, reopened, ready_for_review), ignores other events/actions with 2xx, and persists + enqueues a review job, then acknowledges in a single fast response without doing analysis inline.
- FR-3: Webhook handling is idempotent on `X-GitHub-Delivery` and on (repo, PR, head SHA): redeliveries are acknowledged and dropped; a new push to the same PR supersedes and cancels queued/running jobs for the older SHA.
- FR-4: The API rate-limits the webhook endpoint per source IP and per installation (Redis-backed), returning 429 with `Retry-After` when exceeded.
- FR-5: All platform interaction (verify event, fetch PR metadata/diff, clone token, post review) goes through a `Connector` interface; GitHub is the only v1 implementation, authenticating as a GitHub App (installation tokens, short-lived, never persisted).

Workspace and sandbox
- FR-6: For each job the system clones/fetches the repository and checks out the exact head commit SHA (and base SHA for diffing) into a unique ephemeral workspace, verifying `HEAD == expected SHA`; the workspace is destroyed on job completion or failure.
- FR-7: Repository code (tests, linters, build) executes only inside a rootless-Docker sandbox container created per test run (gVisor is a later option, not v1) that is resource-limited (CPU, memory, pids, wall-clock), network-disabled by default, non-root, with a read-only base image and an ephemeral workdir (only workspace/tmp writable), no Docker socket, and no environment secrets.
- FR-8: The runner auto-detects project type (Node/TypeScript, Python, Go, Rust) from manifest files (package.json, pyproject.toml/requirements.txt/setup.py, go.mod, Cargo.toml), supports polyglot repos, and reports "unsupported/unknown" without failing the review.
- FR-9: The runner executes the matching test tool (jest, pytest, `go test`, `cargo test`) with coverage enabled where available, using diff-aware filtering (tests related to changed files/packages) and falling back to the full suite under a time cap when no mapping is found.
- FR-10: Dependency installation, when required, is performed in a separate sandbox step whose only network path is a configurable allowlisted package registry/proxy; the test step then runs with network disabled. Operator can disable test execution globally or per repo.
- FR-11: A per-repo optional config file (`.pr-review.yml`) may set test command override, timeouts, path ignores, enabled categories, and severity threshold; missing or invalid config falls back to defaults and surfaces a notice in the summary rather than failing.

Parsing
- FR-12: Parsers convert test output (jest JSON, pytest junit XML/JSON, `go test -json`, cargo test output), coverage reports (lcov, coverage.xml/json, go cover profile) and linter output (Ruff, Flake8, ESLint) into versioned Pydantic models: per-test status, failure message, truncated sanitized log excerpts, per-file uncovered line ranges.
- FR-13: Diff parsing produces tokenized chunks bounded to a configurable token budget, each with changed hunks plus enclosing function/class context (AST via `ast` for Python and tree-sitter for other languages; line-window fallback for unparseable files), skipping binary, generated, lock and vendored files.

AI analysis
- FR-14: The LLM layer analyses each chunk across categories: correctness/logic, security (OWASP Top 10, hard-coded secrets, injection), performance (algorithmic complexity, N+1 queries), and style/maintainability aligned with the repo's detected linter configuration.
- FR-15: All LLM output is validated against a Pydantic `Finding` schema (severity CRITICAL/MAJOR/MINOR/INFO, category, file_path, line_start/line_end, summary, rationale, optional suggestion, confidence); invalid output (more likely from local Ollama models) triggers a bounded Pydantic-validation repair/re-ask retry, then the chunk is recorded as failed (not silently dropped).
- FR-16: Findings are correlated with test and coverage results: a finding on a failing-test location or uncovered changed line is annotated and may have severity raised by deterministic rules; failing tests with no matching finding are reported in the summary. Severity mapping and final routing rules are deterministic code, not LLM-decided.
- FR-17: The aggregator deduplicates findings by (file, line range, category), drops findings not located on lines present in the PR diff (they go to the summary instead), applies a configurable minimum severity/confidence, and caps comment count.
- FR-18: LLM calls go through a provider-agnostic layer built on LiteLLM (Python). The default/dev provider is Ollama running locally (free, no API key, code never leaves the host); operators may switch to any LiteLLM-supported provider (OpenAI, Gemini, etc.) by changing `.env` (model name and bring-your-own key). A configurable model per role (review, triage) and a configurable fallback model chain are supported; retries use exponential backoff and jitter on 429/5xx/timeouts honoring `Retry-After`; the next model in the fallback chain is used after retries are exhausted; per-job token/cost budget and a daily cost cap that stops new LLM calls when exceeded.
- FR-19: Untrusted PR content (diff, comments, file contents, test logs) is passed to the LLM as clearly delimited data under a fixed system prompt; model output can never trigger tool execution, config change or secret access; an injection guard flags known override patterns and the event is recorded.

Posting
- FR-20: The system posts one GitHub review containing inline comments (with suggestion blocks where applicable) and a summary (verdict counts by severity, test/coverage status, degraded-mode notices, skipped files/chunks); posting is idempotent per (PR, head SHA) so retries never duplicate comments, and stale reviews for superseded SHAs are not posted.
- FR-21: The review event is `COMMENT` only by default (decided); an operator config option may enable `REQUEST_CHANGES` when CRITICAL findings exist. There is no human approval gate before posting in v1. GitHub secondary rate limits and 5xx are retried with backoff; inline-position failures degrade to summary text.

Pipeline and persistence
- FR-22: Jobs are delivered through Redis + ARQ, with Postgres as source of truth (job state, idempotency keys, review history, dead-letter); stages (fetch, detect, test, parse, analyse, aggregate, post) are individually recorded, idempotent and resumable, so a crashed worker's job is re-leased and continues from the last completed stage.
- FR-23: Failed stages retry with exponential backoff up to a max attempts; exhausted jobs move to a dead-letter state with reason, and a CLI/admin endpoint lists, inspects and requeues dead-lettered jobs. A PR whose analysis fails partially still receives a summary stating what was not covered.
- FR-24: Postgres persists review/run history: jobs, stage attempts, test run summaries, findings (metadata and line references, not source code), LLM call metadata (model, tokens, cost, latency, outcome), posted comment IDs, and an append-only event log.
- FR-25: A retention job purges workspaces, raw logs and records older than the configured retention (default 30 days, configurable). Source code is never persisted; only sanitized findings and metadata are kept.
- FR-26: Operators have an authenticated admin API (token-based) for job status, history, DLQ operations and cost summaries; no UI in v1.

Delivery
- FR-27: The repository ships a docker-compose stack (api, worker, Postgres, Redis, migration job), a sandbox runner image per supported language, a README (install, GitHub App setup, env vars, local testing, deployment, troubleshooting), and `.env.example`.

## Non-functional requirements

Security
- NFR-1: Secrets (webhook secret, App private key, LLM provider API key, if any; none for default Ollama) come only from environment/secret files, are never logged, persisted to the DB, placed in prompts, or passed into sandboxes.
- NFR-2: Sandbox escape surface is minimised: no privileged mode, no host mounts beyond the workspace, seccomp default profile, dropped capabilities, and the worker never executes repo code outside the sandbox.
- NFR-3: Clone/checkout uses short-lived installation tokens kept out of git config/remote URLs persisted in the workspace and out of logs; git hooks and submodule/LFS auto-execution are disabled.
- NFR-4: Admin and webhook endpoints fail closed; constant-time secret comparison; request body size limit; dependencies pinned and audited in CI.

Privacy
- NFR-5: Source code and test logs are never written to application logs or persistent storage; only metadata, line references and redacted, length-capped excerpts are stored. A redaction filter (known secret patterns, high-entropy tokens, env values) is applied to logs, stored excerpts and outbound LLM payloads.
- NFR-6: The only external egress of code is to the configured LLM provider and GitHub (none to any third party with the default local Ollama); this is documented, and no telemetry is sent to the vendor.

Reliability and data pipeline
- NFR-7: No acknowledged webhook is lost: an event is durably persisted before a 2xx is returned; the worker supports at-least-once delivery with idempotent stages, giving effectively-once review posting.
- NFR-8: Recovery: after killing a worker mid-job, the job is resumed or retried within the lease timeout (default 5 min) with no duplicate comments.
- NFR-9: Every external call (GitHub, LLM, git, sandbox) has a timeout; one stalled stage cannot hang a job past its overall deadline; circuit breaker on repeated provider failures.
- NFR-10: Degradation is explicit: when tests, a model or a chunk fails, the review still posts with a clear notice (slower-but-correct, never silently incomplete).
- NFR-11: Schema changes use versioned migrations; stage payloads and Pydantic schemas are versioned for forward-compatible replay.

Observability
- NFR-12: Structured JSON logs with correlation IDs (delivery ID, job ID, stage); `/healthz` and `/readyz` endpoints; Prometheus `/metrics` with queue depth, stage latency, retries, DLQ size, LLM tokens/cost, sandbox failures.
- NFR-13: Every job has a queryable timeline of stage events (append-only) sufficient to explain any posted comment and its cost.

Performance and cost
- NFR-14: Webhook p95 response < 500 ms excluding network; for a median PR (<= 500 changed lines, test suite <= 5 min) review posted within 10 minutes of the event; sized for a target of ~100 PRs/hour and repos up to 1 GB; workers scale horizontally via configurable concurrency.
- NFR-15: Cost control: only diff chunks (not whole repos) are sent to the LLM, with per-job and daily budgets; cheap model used for triage; prompt caching used where the selected provider supports it (local Ollama has no per-token cost).

Quality and maintainability
- NFR-16: Typed, linted (Ruff, mypy) Python 3.12 with src layout; unit + mocked-integration test coverage >= 85% on non-sandbox code; final code contains no TODOs, stubs or pseudocode.
- NFR-17: The test suite runs offline with no API keys (mocked LiteLLM/LLM, GitHub, git remote; no network) and covers broken code, missing/invalid config, failing tests, LLM and GitHub timeouts/429s, and invalid LLM output.

## Constraints

- Python + FastAPI + Pydantic only; no JS/TS in the product (the human cannot maintain it). Prefer stdlib and well-known libraries (httpx, SQLAlchemy/psycopg, a Redis-backed job library, tenacity, tree-sitter, Docker SDK); avoid custom frameworks.
- v1 platform GitHub only, as a GitHub App; GitLab later via the Connector interface.
- v1 deployment self-hosted via docker-compose (api, worker(s), Postgres, Redis); Docker is required on the worker host for sandboxes.
- LLM: provider-agnostic via LiteLLM; default/dev provider is local Ollama (free, no key); other providers (OpenAI, Gemini, ...) selected via `.env` with bring-your-own keys.
- Human approval gate before posting, licensing/billing, and stronger isolation (gVisor/Firecracker) are out of scope for v1.
- Simplicity and low build cost take priority over breadth.
- Customer code is confidential; untrusted repository code must never run on the worker process or with network or secrets.

## Non-goals

- GitLab, Bitbucket or other connectors in v1 (interface only).
- Multi-tenant SaaS hosting, tenant isolation, billing/metering, user accounts and SSO.
- Web dashboard/UI; vector-DB code retrieval (RAG), embeddings and codebase indexing.
- Human-in-the-loop approval gate before posting (decided non-goal for v1), feedback/dispute learning loop.
- Licensing, license keys and billing (out of scope for v1).
- Auto-fix commits, auto-merge, or pushing code to the customer repo.
- Languages beyond Node/TS, Python, Go, Rust; Kubernetes/Helm manifests.
- Guaranteeing detection of all bugs or vulnerabilities; this is advisory review.

## Trust boundaries and threat model

Boundaries: (1) Internet to API; (2) API/worker to sandbox containers running untrusted repo code; (3) PR content to LLM prompt; (4) system to GitHub and LLM external APIs; (5) logs/DB to operators.

| Threat | Mitigation (requirements) |
|---|---|
| Webhook forgery/replay/flood | FR-1, FR-3, FR-4, NFR-4 |
| Malicious repo code in test runner (RCE, exfiltration, crypto-mining, fork bombs, container escape) | FR-7, FR-10, NFR-2, NFR-3 |
| Prompt injection via PR title/diff/comments/logs | FR-19, FR-15, FR-17 (no tool access, schema-validated output, deterministic severity) |
| Secret leakage via logs, DB, prompts, comments | NFR-1, NFR-5, FR-24 |
| Stolen installation token or checkout credentials | NFR-3, FR-5 |
| Duplicate/ghost comments, lost jobs | FR-3, FR-20, FR-22, NFR-7 |
| Cost-exhaustion via spam PRs | FR-4, FR-18, NFR-15 |

## Acceptance criteria

- AC-1: A webhook POST with missing or wrong `X-Hub-Signature-256` returns 401 and creates no job or DB row; a correctly signed one returns 2xx. (FR-1)
- AC-2: Replaying the same `X-GitHub-Delivery` twice yields exactly one job; a new head SHA on the same PR cancels the older SHA's pending job. (FR-3)
- AC-3: Exceeding the configured rate returns 429 with `Retry-After`; requests within limit succeed. (FR-4)
- AC-4: Non-PR events and ignored PR actions return 2xx and enqueue nothing. (FR-2)
- AC-5: After a job, the workspace HEAD equaled the requested SHA, and the workspace directory no longer exists. (FR-6)
- AC-6: In an integration test, a sandboxed test script that attempts network access, reads host env secrets, writes outside the workspace, or exceeds memory/time limits is blocked or killed, and the job still completes with a notice. (FR-7, NFR-2)
- AC-7: Fixture repos for Node/TS, Python, Go and Rust are detected correctly, and an empty or unknown repo yields "unsupported" without job failure. (FR-8)
- AC-8: For a fixture PR touching one module, only the related tests run; with no mapping, the full suite runs under the time cap. (FR-9)
- AC-9: Each supported test, coverage and linter fixture output parses into the expected Pydantic JSON; malformed output yields a parse-error record, not an exception. (FR-12)
- AC-10: Diff chunks for Python and one other language include the enclosing function/class, respect the token budget, and skip lock/binary/generated files. (FR-13)
- AC-11: With a mocked LLM returning schema-invalid output, the system repairs/re-asks (bounded retries) then records a failed chunk; valid output yields `Finding` objects with only the four allowed severities. (FR-15)
- AC-12: With a fixture where a changed line is uncovered or in a failing test, the resulting finding is annotated and the deterministic severity rule applies; failing tests without findings appear in the summary. (FR-16)
- AC-13: Findings outside the diff are moved to the summary; duplicates are merged; comment cap is enforced. (FR-17)
- AC-14: Mocked LLM 429 (with Retry-After), 5xx and timeout produce retries with exponentially increasing delays, then fallback to the next model in the configured fallback chain; switching provider/model via `.env` requires no code change; exceeding the daily budget blocks the call and the job posts a degraded notice. (FR-18, NFR-9, NFR-10)
- AC-15: A PR containing "ignore previous instructions" text and a fake secret never causes tool calls or altered behavior, is flagged in events, and the injection payload appears in no log. (FR-19, NFR-5)
- AC-16: A mocked GitHub receives exactly one review with inline comments and a summary; re-running the same job posts nothing new; a superseded SHA posts nothing; a position error degrades to summary text. (FR-20, FR-21)
- AC-17: Killing the worker between stages and restarting results in resumed completion with no repeated completed stage and no duplicate comments within the lease timeout. (FR-22, NFR-7, NFR-8)
- AC-18: A stage failing repeatedly moves the job to dead-letter after max attempts; the admin API lists and requeues it, and a partial-failure PR still gets a summary. (FR-23, FR-26)
- AC-19: After a full run, the DB contains job, stage, test summary, finding and LLM-call metadata, and a grep of DB dumps and logs for fixture source lines and seeded secrets finds nothing. (FR-24, NFR-1, NFR-5)
- AC-20: Retention job removes records and workspaces older than the retention (30 days default, configurable override honored); no source code is found in storage. (FR-25)
- AC-21: Missing/invalid `.pr-review.yml`, missing required env vars (clear startup error), and absent test config all produce defaults or a clear failure, never a crash loop. (FR-11, NFR-17)
- AC-22: `/healthz`, `/readyz` and `/metrics` respond; metrics include queue depth, DLQ size and LLM cost; logs are JSON with correlation IDs; a job timeline is retrievable. (NFR-12, NFR-13)
- AC-23: `docker compose up` on a clean machine with `.env` populated starts all services healthy; README steps for install, local testing and deployment are verified by following them. (FR-27)
- AC-24: `pytest` passes offline with no API keys, coverage >= 85% on non-sandbox code, Ruff and mypy are clean, and a grep finds no TODO/FIXME/pseudocode in `src/`. (NFR-16, NFR-17)
- AC-25: Webhook p95 latency < 500 ms in a local benchmark, and a benchmark PR of <= 500 changed lines completes with mocked LLM inside the 10-minute budget. (NFR-14)
- AC-26: A load test of 50 queued jobs with injected faults (killed workers, Redis restart, LLM/GitHub outages) ends with zero lost jobs: each is posted once or dead-lettered. (NFR-7, NFR-9, NFR-11)

## Proposed architecture (guidance, not binding)

Flow: GitHub webhook -> `api` (verify, rate-limit, dedupe, persist job in Postgres, enqueue Redis) -> `worker` pulls job -> stages: fetch/checkout -> detect -> sandbox test+coverage+lint -> parse -> chunk diff -> LLM analyse (provider w/ retries/fallback) -> correlate + aggregate -> post review. Postgres is the source of truth for job/stage state; Redis is the transient queue and rate-limit store. A single modular monolith image runs as API or worker by command.

Package layout (`src/pr_review/`):
- `api/`: FastAPI app, webhook, admin, health, metrics.
- `connectors/`: `base.py` Connector protocol, `github/` (App auth, client, posting).
- `pipeline/`: Redis/ARQ queue, job and stage state machine, leases, retries, dead-letter, supersession.
- `runner/`: workspace, git checkout, sandbox (Docker), project detection, test selection.
- `parsers/`: test, coverage and linter parsers; diff and AST chunker.
- `llm/`: LiteLLM-based provider layer (Ollama default), retry/fallback, budget guard, prompts (versioned), injection guard.
- `review/`: Finding models, correlation, aggregation, deterministic severity rules, summary rendering.
- `storage/`: SQLAlchemy models, migrations, repositories, event log, retention.
- `core/`: settings (pydantic-settings), redaction, logging, metrics.
- `tests/` mirrors the above with fixtures and fakes.

## Risks

- Sandbox escape or abuse of untrusted code: dedicated unprivileged containers, no network, limits, gVisor later option; documented host-hardening.
- LLM false positives/noise erode trust: severity/confidence thresholds, grounding in test results, comment cap, golden-PR evaluation set.
- Prompt injection: delimiting, schema validation, no tools, deterministic severity.
- Cost overrun: chunk-only context, budgets, cheap triage model.
- Test execution unreliability across arbitrary repos (missing deps, long suites, flaky tests): time caps, graceful "tests not run" mode, per-repo overrides.
- Docker-in-worker requires host Docker access, itself a privilege risk: use a dedicated sandbox host/daemon option or rootless Docker; document.
- Vendor/article bias: reference article is promotional and omits prompts, thresholds and sanitization rules; those are specified here from first principles.
- GitHub/LLM API changes or rate limits: provider and connector interfaces, backoff, pinned API versions.

## Success criteria

- 100 percent of signed PR events in the fault-injection test (AC-26) end in exactly one posted review or an inspectable dead-letter entry.
- Zero leaked secrets or source lines in logs and DB in the leakage test (AC-19).
- All ACs pass in CI; a pilot customer can deploy via README alone.
- Median review latency <= 10 minutes on pilot repos; reviewers rate at least 60 percent of posted findings as useful (pilot survey).

## Open questions

All resolved by the human on 2026-10-23 ("approve with defaults, LLM will be Ollama free version"):

1. LLM: LiteLLM provider-agnostic layer, Ollama local default, any LiteLLM provider via `.env` (FR-18, FR-15, NFR-1, NFR-6, NFR-17).
2. Sandbox: rootless Docker per test run; gVisor later (FR-7).
3. Queue: Redis + ARQ delivery, Postgres source of truth (FR-22).
4. Review event: COMMENT default, optional REQUEST_CHANGES on CRITICAL (FR-21).
5. Dependency install: only via allowlisted registry/proxy, configurable (FR-10).
6. Retention: 30 days default, configurable; no source code persisted (FR-25).
7. Human approval gate: non-goal for v1.
8. Scale: ~100 PRs/hour, repos up to 1 GB (NFR-14).
9. Licensing/billing: out of scope for v1.

None remain open.
