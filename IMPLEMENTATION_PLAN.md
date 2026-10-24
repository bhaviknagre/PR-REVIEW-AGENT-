# IMPLEMENTATION_PLAN — pr-review

Planning document only (not `.genesis/PLAN.md`). Each task maps 1:1 to a later `genesis task add`.

## Intro
- **Objective:** Self-hosted, Python/FastAPI AI pull-request review agent for GitHub that runs a repo's tests in a sandbox, reviews diffs with an LLM (LiteLLM, Ollama default), and posts structured inline findings.
- **Stack:** Python 3.12, FastAPI, Pydantic v2, ARQ + Redis, Postgres via SQLAlchemy 2 + Alembic, LiteLLM (Ollama default), rootless Docker sandbox, uv, pytest, ruff, mypy.
- **Layout (`src/pr_review/`):** `api/` `connectors/` (`base.py`, `github/`) `pipeline/` `runner/` `parsers/` `llm/` `review/` `storage/` `core/`; tests in `tests/{unit,integration}/` mirroring these.
- **Conventions:** risk medium/high tasks get the `independent-review` gate automatically (human review). Gate IDs: `tests`, `lint`, `types`. Tests are offline (mocked LLM/GitHub/git). Docker-dependent tests carry marker `docker`.

## Milestones
| M | Name | Tasks |
|---|---|---|
| M0 | Scaffold, tooling, CI | T-01..T-04 |
| M1 | Webhook ingress, idempotency, persistence, connector | T-05..T-12 |
| M2 | Durable pipeline, queue, dead-letter, observability | T-13..T-17 |
| M3 | Git workspace isolation | T-18 |
| M4 | Sandbox, detection, test selection, parsing | T-19..T-26 |
| M5 | Diff / context chunking | T-27..T-28 |
| M6 | LLM layer, findings, injection guard, analyzers | T-29..T-34 |
| M7 | Review assembly, GitHub posting, stage wiring | T-35..T-39 |
| M8 | Security hardening, leakage, retention | T-40..T-41 |
| M9 | Deploy, README, E2E, fault injection | T-42..T-46 |

Gate shorthand: `L` = `uv run ruff check src tests`, `T` = `uv run mypy src`. Every task also runs L and T unless stated.

## Tasks
### M0 Scaffold
| ID | Outcome | Risk | Deps | Scope | Reqs | Gate |
|---|---|---|---|---|---|---|
| T-01 | uv project with src layout, package skeleton, ruff/mypy/pytest config, passing smoke test | low | - | `pyproject.toml`, `src/pr_review/__init__.py`, `src/pr_review/api/__init__.py`, `src/pr_review/connectors/__init__.py`, `src/pr_review/connectors/github/__init__.py`, `src/pr_review/core/__init__.py`, `src/pr_review/llm/__init__.py`, `src/pr_review/parsers/__init__.py`, `src/pr_review/pipeline/__init__.py`, `src/pr_review/review/__init__.py`, `src/pr_review/runner/__init__.py`, `src/pr_review/storage/__init__.py`, `tests/unit/test_smoke.py`, `.gitignore`, `uv.lock` | NFR-16 | `uv run pytest tests/unit/test_smoke.py -q` |
| T-02 | pydantic-settings config loads all env vars and fails fast with a clear message on missing required ones | low | T-01 | `src/pr_review/core/settings.py`, `tests/unit/test_settings.py`, `pyproject.toml`, `uv.lock` | NFR-1, AC-21 | `uv run pytest tests/unit/test_settings.py -q` |
| T-03 | Redaction filter (secret patterns, high entropy, env values) and JSON logging with correlation IDs (delivery/job/stage) | medium | T-02 | `src/pr_review/core/redaction.py`, `core/logging.py`, `tests/unit/test_redaction.py` | NFR-5, NFR-12 | `uv run pytest tests/unit/test_redaction.py -q` |
| T-04 | GitHub Actions CI running ruff, mypy, pytest with 85% coverage gate, pip-audit, TODO/FIXME grep, pinned lockfile | low | T-01 | `.github/workflows/ci.yml`, `scripts/check_no_todos.sh`, `pyproject.toml`, `uv.lock` | NFR-4, NFR-16, AC-24 | `bash scripts/check_no_todos.sh && uv lock --check` |

### M1 Ingress and persistence
| ID | Outcome | Risk | Deps | Scope | Reqs | Gate |
|---|---|---|---|---|---|---|
| T-05 | SQLAlchemy 2 models and Alembic initial migration for jobs, stage attempts, delivery keys, test runs, findings, LLM calls, comments, events | medium | T-02 | `src/pr_review/storage/models.py`, `storage/migrations/**`, `alembic.ini`, `tests/unit/test_models.py`, `pyproject.toml`, `uv.lock` | FR-24, NFR-11 | `uv run pytest tests/unit/test_models.py -q` |
| T-06 | Repositories and append-only event log (no update/delete path) with versioned stage payload schemas | medium | T-05 | `src/pr_review/storage/repositories.py`, `storage/events.py`, `tests/unit/test_events.py` | FR-24, NFR-13, NFR-11 | `uv run pytest tests/unit/test_events.py -q` |
| T-07 | Webhook endpoint verifies HMAC-SHA256 on raw body (constant-time), enforces body size limit, 401 before any parsing | high | T-02 | `src/pr_review/api/webhook.py`, `api/app.py`, `tests/unit/test_webhook_signature.py`, `pyproject.toml`, `uv.lock` | FR-1, NFR-4, AC-1 | `uv run pytest tests/unit/test_webhook_signature.py -q` |
| T-08 | Webhook filters to pull_request opened/synchronize/reopened/ready_for_review, persists job durably then enqueues, fast 2xx | medium | T-06, T-07 | `src/pr_review/api/webhook.py`, `tests/unit/test_webhook_events.py` | FR-2, NFR-7, AC-4 | `uv run pytest tests/unit/test_webhook_events.py -q` |
| T-09 | Idempotency on delivery ID and (repo, PR, head SHA); new SHA cancels queued/running older-SHA jobs | medium | T-08 | `src/pr_review/api/webhook.py`, `pipeline/supersede.py`, `tests/unit/test_idempotency.py` | FR-3, AC-2 | `uv run pytest tests/unit/test_idempotency.py -q` |
| T-10 | Redis-backed per-IP and per-installation rate limiter returning 429 with Retry-After | low | T-07 | `src/pr_review/api/ratelimit.py`, `tests/unit/test_ratelimit.py`, `pyproject.toml`, `uv.lock` | FR-4, AC-3 | `uv run pytest tests/unit/test_ratelimit.py -q` |
| T-11 | `Connector` protocol plus GitHub App auth minting short-lived installation tokens never persisted | high | T-02 | `src/pr_review/connectors/base.py`, `connectors/github/auth.py`, `tests/unit/test_github_auth.py`, `pyproject.toml`, `uv.lock` | FR-5, NFR-1 | `uv run pytest tests/unit/test_github_auth.py -q` |
| T-12 | GitHub connector fetches PR metadata and diff with timeouts, via httpx with pinned API version | medium | T-11 | `src/pr_review/connectors/github/client.py`, `tests/unit/test_github_client.py`, `pyproject.toml`, `uv.lock` | FR-5, NFR-9 | `uv run pytest tests/unit/test_github_client.py -q` |

### M2 Pipeline
| ID | Outcome | Risk | Deps | Scope | Reqs | Gate |
|---|---|---|---|---|---|---|
| T-13 | ARQ worker, job and stage state machine with leases and configurable concurrency; Postgres is truth | high | T-06 | `src/pr_review/pipeline/queue.py`, `pipeline/state.py`, `tests/unit/test_state_machine.py`, `pyproject.toml`, `uv.lock` | FR-22, NFR-7, NFR-14 | `uv run pytest tests/unit/test_state_machine.py -q` |
| T-14 | Stage retries with exponential backoff, max attempts, dead-letter with reason | medium | T-13 | `src/pr_review/pipeline/retry.py`, `tests/unit/test_dead_letter.py` | FR-23 | `uv run pytest tests/unit/test_dead_letter.py -q` |
| T-15 | Crash recovery: expired lease re-leased, resumes from last completed stage; per-stage timeout and overall job deadline | high | T-14 | `src/pr_review/pipeline/lease.py`, `tests/integration/test_recovery.py` | FR-22, NFR-8, NFR-9, AC-17 | `uv run pytest tests/integration/test_recovery.py -q` |
| T-16 | Token-authenticated admin API (fail closed): job status/history, DLQ list/inspect/requeue, cost summary, plus CLI | high | T-14 | `src/pr_review/api/admin.py`, `pipeline/cli.py`, `tests/unit/test_admin.py` | FR-23, FR-26, NFR-4, AC-18 | `uv run pytest tests/unit/test_admin.py -q` |
| T-17 | `/healthz`, `/readyz`, Prometheus `/metrics` (queue depth, DLQ, stage latency, LLM cost) and job timeline endpoint | low | T-06 | `src/pr_review/api/health.py`, `core/metrics.py`, `tests/unit/test_observability.py`, `pyproject.toml`, `uv.lock` | NFR-12, NFR-13, AC-22 | `uv run pytest tests/unit/test_observability.py -q` |

### M3 Workspace
| ID | Outcome | Risk | Deps | Scope | Reqs | Gate |
|---|---|---|---|---|---|---|
| T-18 | Ephemeral workspace: clone/fetch, checkout exact head+base SHA, verify HEAD, token kept out of config/URLs/logs, hooks/submodules/LFS disabled, destroy on finish/fail | high | T-12 | `src/pr_review/runner/workspace.py`, `tests/integration/test_workspace.py` | FR-6, NFR-3, AC-5 | `uv run pytest tests/integration/test_workspace.py -q` |

### M4 Sandbox and tests
| ID | Outcome | Risk | Deps | Scope | Reqs | Gate |
|---|---|---|---|---|---|---|
| T-19 | Docker SDK sandbox executor: rootless, non-root, cap-drop ALL, default seccomp, no privileged/socket/host mounts, read-only root, tmpfs, cpu/mem/pids/wall-clock limits, network off, empty env | high | T-18 | `src/pr_review/runner/sandbox.py`, `tests/unit/test_sandbox_spec.py`, `pyproject.toml`, `uv.lock` | FR-7, NFR-2 | `uv run pytest tests/unit/test_sandbox_spec.py -q` |
| T-20 | Hostile-script integration tests (network, env secrets, write outside workspace, OOM, timeout) blocked/killed and job completes with notice | high | T-19 | `tests/integration/test_sandbox_escape.py`, `tests/fixtures/hostile/**` | FR-7, NFR-2, NFR-10, AC-6 | `uv run pytest -m docker tests/integration/test_sandbox_escape.py -q` |
| T-21 | Project-type detection (Node/TS, Python, Go, Rust, polyglot, unknown) from manifests | low | T-01 | `src/pr_review/runner/detect.py`, `tests/fixtures/repos/**`, `tests/unit/test_detect.py` | FR-8, AC-7 | `uv run pytest tests/unit/test_detect.py -q` |
| T-22 | `.pr-review.yml` loader with defaults and notice on missing/invalid; overrides for test cmd, timeouts, ignores, categories, severity threshold, test disable | low | T-02 | `src/pr_review/core/repo_config.py`, `tests/unit/test_repo_config.py`, `pyproject.toml`, `uv.lock` | FR-11, FR-10, AC-21 | `uv run pytest tests/unit/test_repo_config.py -q` |
| T-23 | Separate dependency-install sandbox step with allowlisted registry/proxy as sole network path; test step then network-off | high | T-19, T-21 | `src/pr_review/runner/install.py`, `tests/unit/test_install.py` | FR-10, NFR-2 | `uv run pytest tests/unit/test_install.py -q` |
| T-24 | Test command builders (jest, pytest, go test, cargo test, coverage on) with diff-aware selection and full-suite fallback under time cap | medium | T-21, T-19 | `src/pr_review/runner/select.py`, `runner/commands.py`, `tests/unit/test_select.py` | FR-9, AC-8 | `uv run pytest tests/unit/test_select.py -q` |
| T-25 | Test and coverage parsers (jest JSON, pytest junit/JSON, go test -json, cargo; lcov, coverage.xml/json, go cover) to versioned Pydantic; malformed gives parse-error record | medium | T-01 | `src/pr_review/parsers/tests.py`, `parsers/coverage.py`, `tests/unit/test_parsers_tests.py` | FR-12, AC-9, NFR-11 | `uv run pytest tests/unit/test_parsers_tests.py -q` |
| T-26 | Linter parsers (Ruff, Flake8, ESLint) with truncated sanitized excerpts | low | T-25, T-03 | `src/pr_review/parsers/lint.py`, `tests/unit/test_parsers_lint.py` | FR-12, AC-9 | `uv run pytest tests/unit/test_parsers_lint.py -q` |

### M5 Chunking
| ID | Outcome | Risk | Deps | Scope | Reqs | Gate |
|---|---|---|---|---|---|---|
| T-27 | Unified-diff parser, token-budgeted chunks, skip binary/generated/lock/vendored, line-window fallback | medium | T-01 | `src/pr_review/parsers/diff.py`, `parsers/chunker.py`, `tests/unit/test_chunker.py`, `pyproject.toml`, `uv.lock` | FR-13, NFR-15, AC-10 | `uv run pytest tests/unit/test_chunker.py -q` |
| T-28 | Enclosing function/class context via `ast` (Python) and tree-sitter (other languages) | medium | T-27 | `src/pr_review/parsers/context.py`, `tests/unit/test_context.py`, `pyproject.toml`, `uv.lock` | FR-13, AC-10 | `uv run pytest tests/unit/test_context.py -q` |

### M6 LLM and findings
| ID | Outcome | Risk | Deps | Scope | Reqs | Gate |
|---|---|---|---|---|---|---|
| T-29 | LiteLLM provider layer, Ollama default, model per role (review/triage) via env, no telemetry | medium | T-02 | `src/pr_review/llm/provider.py`, `tests/unit/test_provider.py`, `pyproject.toml`, `uv.lock` | FR-18, NFR-6, NFR-1 | `uv run pytest tests/unit/test_provider.py -q` |
| T-30 | Retry with exponential backoff+jitter honoring Retry-After, timeouts, fallback model chain, circuit breaker | high | T-29 | `src/pr_review/llm/resilience.py`, `tests/unit/test_resilience.py` | FR-18, NFR-9, AC-14 | `uv run pytest tests/unit/test_resilience.py -q` |
| T-31 | Per-job token/cost budget and daily cost cap that blocks LLM calls and yields degraded notice; LLM call metadata recorded | medium | T-30, T-06 | `src/pr_review/llm/budget.py`, `tests/unit/test_budget.py` | FR-18, FR-24, NFR-15, NFR-10, AC-14 | `uv run pytest tests/unit/test_budget.py -q` |
| T-32 | `Finding` Pydantic schema, versioned prompts for four categories, validation with bounded repair/re-ask then failed-chunk record | medium | T-29, T-27 | `src/pr_review/review/models.py`, `llm/prompts/**`, `llm/analyze.py`, `tests/unit/test_analyze.py` | FR-14, FR-15, NFR-11, AC-11 | `uv run pytest tests/unit/test_analyze.py -q` |
| T-33 | Injection guard and prompt delimiting (untrusted data under fixed system prompt, no tools), outbound payload redaction, event recorded, payload never logged | high | T-32, T-03 | `src/pr_review/llm/guard.py`, `tests/unit/test_guard.py` | FR-19, NFR-5, AC-15 | `uv run pytest tests/unit/test_guard.py -q` |
| T-34 | Deterministic secret and pattern scan on diff (hard-coded secrets, injection sinks) plus linter findings merged as pre-LLM `Finding`s | medium | T-32, T-26 | `src/pr_review/review/static_checks.py`, `tests/unit/test_static_checks.py` | FR-14, NFR-5 | `uv run pytest tests/unit/test_static_checks.py -q` |

### M7 Review assembly and posting
| ID | Outcome | Risk | Deps | Scope | Reqs | Gate |
|---|---|---|---|---|---|---|
| T-35 | Correlate findings with failing tests/uncovered lines; deterministic severity raise; unmatched failing tests listed | medium | T-32, T-25 | `src/pr_review/review/correlate.py`, `tests/unit/test_correlate.py` | FR-16, AC-12 | `uv run pytest tests/unit/test_correlate.py -q` |
| T-36 | Aggregator: dedupe by (file, range, category), off-diff to summary, min severity/confidence, comment cap | medium | T-35 | `src/pr_review/review/aggregate.py`, `tests/unit/test_aggregate.py` | FR-17, AC-13 | `uv run pytest tests/unit/test_aggregate.py -q` |
| T-37 | Summary renderer: severity counts, test/coverage status, degraded-mode notices, skipped files/chunks, config notices | low | T-36 | `src/pr_review/review/summary.py`, `tests/unit/test_summary.py` | FR-20, NFR-10 | `uv run pytest tests/unit/test_summary.py -q` |
| T-38 | Idempotent GitHub review post per (PR, head SHA) with suggestion blocks, COMMENT default, optional REQUEST_CHANGES, backoff on secondary limits/5xx, position failure degrades to summary, stale SHA skipped | high | T-37, T-12, T-09 | `src/pr_review/connectors/github/posting.py`, `tests/unit/test_posting.py` | FR-20, FR-21, NFR-7, NFR-9, AC-16 | `uv run pytest tests/unit/test_posting.py -q` |
| T-39 | Stage orchestration fetch, detect, test, parse, analyse, aggregate, post; partial failure still posts a summary | high | T-15, T-24, T-25, T-28, T-31, T-33, T-36, T-38 | `src/pr_review/pipeline/stages.py`, `tests/integration/test_stages.py` | FR-22, FR-23, NFR-10, AC-18 | `uv run pytest tests/integration/test_stages.py -q` |

### M8 Hardening
| ID | Outcome | Risk | Deps | Scope | Reqs | Gate |
|---|---|---|---|---|---|---|
| T-40 | Leakage test: after full mocked run, DB dump and logs contain no fixture source lines or seeded secrets; only metadata and capped redacted excerpts stored | high | T-39, T-03 | `tests/integration/test_leakage.py`, `src/pr_review/storage/sanitize.py` | FR-24, NFR-1, NFR-5, AC-19 | `uv run pytest tests/integration/test_leakage.py -q` |
| T-41 | Retention job purges records, workspaces, raw logs older than configurable days (default 30) | medium | T-06, T-18 | `src/pr_review/storage/retention.py`, `tests/unit/test_retention.py` | FR-25, AC-20 | `uv run pytest tests/unit/test_retention.py -q` |

### M9 Deploy and verification
| ID | Outcome | Risk | Deps | Scope | Reqs | Gate |
|---|---|---|---|---|---|---|
| T-42 | Dockerfile (api/worker by command), per-language sandbox images, docker-compose (api, worker, Postgres, Redis, migrate), `.env.example` | medium | T-39 | `Dockerfile`, `docker/sandbox/**`, `docker-compose.yml`, `.env.example` | FR-27, AC-23 | `docker compose config -q` |
| T-43 | README: install, GitHub App setup, env vars, Ollama/LiteLLM switching, local testing, deployment, host hardening, egress/no-telemetry statement, troubleshooting | low | T-42 | `README.md` | FR-27, NFR-6, AC-23 | `test -s README.md && grep -q "GitHub App" README.md` |
| T-44 | Real end-to-end test (signed webhook to posted review on the sandbox GitHub repo, real Ollama/Postgres/Redis/Docker) over a varied scenario library plus a clean PR; integration tests moved off the single zero-division fixture; offline coverage >= 85% on non-sandbox code; pip timeout/retries in sandbox install; test containers removed with volumes; filelock CVE bump; `pr-review` console script and `github` pytest marker | medium | T-39 | `IMPLEMENTATION_PLAN.md`, `tests/e2e/`, `tests/integration/test_stages.py`, `tests/integration/test_leakage.py`, `tests/integration/test_llm_real.py`, `tests/integration/test_recovery.py`, `tests/integration/test_storage_postgres.py`, `tests/unit/test_retention.py`, `tests/unit/test_budget.py`, `src/pr_review/runner/install.py`, `pyproject.toml`, `uv.lock` | NFR-16, NFR-17, AC-24 | `uv run pytest -q -m "not docker" --cov=pr_review --cov-fail-under=85`; `uv run pytest tests/e2e -q`; `uv run pip-audit` |
| T-45 | Local benchmark: webhook p95 < 500 ms and <=500-line PR with mocked LLM within 10 min | medium | T-44 | `tests/perf/test_bench.py` | NFR-14, AC-25 | `uv run pytest tests/perf/test_bench.py -q` |
| T-46 | Fault-injection load test: 50 jobs with worker kills, Redis restart, LLM/GitHub outages; zero lost, each posted once or dead-lettered | high | T-44, T-42 | `tests/load/test_fault_injection.py` | NFR-7, NFR-9, NFR-11, AC-26 | `uv run pytest -m load tests/load/test_fault_injection.py -q` |

Totals: 10 milestones, 46 tasks. Medium/high (need independent human review): all except T-01, T-02, T-04, T-10, T-17, T-21, T-22, T-26, T-37, T-43. High: T-07, T-11, T-13, T-15, T-16, T-18, T-19, T-20, T-23, T-30, T-33, T-38, T-39, T-40, T-46.

## Registration syntax (later, human-approved)
`genesis task add . --id T-07 --outcome "..." --risk high --requirement FR-1 --requirement NFR-4 --requirement AC-1 --depends T-02 --scope src/pr_review/api/webhook.py --gate "tests:uv run pytest tests/unit/test_webhook_signature.py -q" --gate "lint:uv run ruff check src tests" --gate "types:uv run mypy src"`
Repeatable flags: `--requirement --depends --scope --gate --scenario` (scenario = JSON with id, actor, scope, environment, duration, observable, gate; gate must be an executable mandatory gate id). Medium/high auto-add `independent-review`. At least one `--requirement` per task; IDs must exist in SPEC.md.

## Coverage table
| Req | Tasks | Req | Tasks | Req | Tasks |
|---|---|---|---|---|---|
| FR-1 | T-07 | NFR-1 | T-02, T-11, T-29, T-40 | AC-1 | T-07 |
| FR-2 | T-08 | NFR-2 | T-19, T-20, T-23 | AC-2 | T-09 |
| FR-3 | T-09 | NFR-3 | T-18 | AC-3 | T-10 |
| FR-4 | T-10 | NFR-4 | T-04, T-07, T-16 | AC-4 | T-08 |
| FR-5 | T-11, T-12 | NFR-5 | T-03, T-33, T-34, T-40 | AC-5 | T-18 |
| FR-6 | T-18 | NFR-6 | T-29, T-43 | AC-6 | T-20 |
| FR-7 | T-19, T-20 | NFR-7 | T-08, T-13, T-38, T-46 | AC-7 | T-21 |
| FR-8 | T-21 | NFR-8 | T-15 | AC-8 | T-24 |
| FR-9 | T-24 | NFR-9 | T-12, T-15, T-30, T-38, T-46 | AC-9 | T-25, T-26 |
| FR-10 | T-22, T-23 | NFR-10 | T-20, T-31, T-37, T-39 | AC-10 | T-27, T-28 |
| FR-11 | T-22 | NFR-11 | T-05, T-06, T-25, T-32, T-46 | AC-11 | T-32 |
| FR-12 | T-25, T-26 | NFR-12 | T-03, T-17 | AC-12 | T-35 |
| FR-13 | T-27, T-28 | NFR-13 | T-06, T-17 | AC-13 | T-36 |
| FR-14 | T-32, T-34 | NFR-14 | T-13, T-45 | AC-14 | T-30, T-31 |
| FR-15 | T-32 | NFR-15 | T-27, T-31 | AC-15 | T-33 |
| FR-16 | T-35 | NFR-16 | T-01, T-04, T-44 | AC-16 | T-38 |
| FR-17 | T-36 | NFR-17 | T-44 | AC-17 | T-15 |
| FR-18 | T-29, T-30, T-31 | | | AC-18 | T-16, T-39 |
| FR-19 | T-33 | | | AC-19 | T-40 |
| FR-20 | T-37, T-38 | | | AC-20 | T-41 |
| FR-21 | T-38 | | | AC-21 | T-02, T-22 |
| FR-22 | T-13, T-15, T-39 | | | AC-22 | T-17 |
| FR-23 | T-14, T-16, T-39 | | | AC-23 | T-42, T-43 |
| FR-24 | T-05, T-06, T-31, T-40 | | | AC-24 | T-04, T-44 |
| FR-25 | T-41 | | | AC-25 | T-45 |
| FR-26 | T-16 | | | AC-26 | T-46 |
| FR-27 | T-42, T-43 | | | | |

All 27 FR, 17 NFR, 26 AC are covered; none unmapped.

## Milestone completion rule
No milestone is marked done without a separate L4 VERIFY approval by the human.
