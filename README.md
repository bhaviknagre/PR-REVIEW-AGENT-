# pr-review

**📘 [PR Review Agent - Setup & Deployment Guide](https://bhaviknagre.github.io/pr-review-agent/setup-and-deployment-guide.html)** · **📖 [PR Review Documentation](https://bhaviknagre.github.io/pr-review-agent/technical-documentation.html)**

A self-hosted AI pull-request review agent for GitHub. It receives GitHub App webhooks, checks out the PR, runs linters and the repository's tests inside a locked-down sandbox, asks an LLM to review the diff, and posts a single review back to the pull request.

The LLM layer is [LiteLLM](https://github.com/BerriAI/litellm). The default is a free local [Ollama](https://ollama.com) model; you can switch to any LiteLLM-supported hosted provider by bringing your own key.

## Table of contents

1. [Architecture](#architecture)
2. [Install](#install)
3. [GitHub App setup](#github-app-setup)
4. [Environment variables](#environment-variables)
5. [Ollama and LiteLLM switching](#ollama-and-litellm-switching)
6. [Per-repository config](#per-repository-config)
7. [Local development and testing](#local-development-and-testing)
8. [Deployment](#deployment)
9. [Operator CLI and admin API](#operator-cli-and-admin-api)
10. [Host hardening](#host-hardening)
11. [Network egress and telemetry](#network-egress-and-telemetry)
12. [Troubleshooting](#troubleshooting)

## Architecture

One container image runs three roles, selected by the container command (`Dockerfile`, entrypoint `entry`):

| Role | Command | What it does |
|---|---|---|
| `api` | `api` (default) | FastAPI app on port 8000: webhook, health, metrics, admin API |
| `worker` | `worker` | arq worker: runs the review pipeline and starts sandbox containers through the host Docker daemon |
| `migrate` | `migrate` | One-shot `alembic upgrade head` |

`docker-compose.yml` adds Postgres 16 and Redis 7 (append-only persistence). Sandbox containers are separate images (`pr-review-sandbox-*`) that the worker launches with no network, no capabilities, a read-only root filesystem and resource caps.

## Install

Prerequisites:

- Docker with the Compose plugin
- A publicly reachable HTTPS URL for the webhook (a reverse proxy in production; a tunnel such as smee.io or ngrok for local trials)
- Ollama on the host for the default LLM setup (or a hosted provider key, see below)
- For development only: [uv](https://docs.astral.sh/uv/) and Python 3.12

Steps:

```sh
git clone <this repository> && cd pr-review
cp .env.example .env            # required, then edit the values
./docker/sandbox/build.sh       # build the sandbox images
ollama pull qwen2.5-coder:7b    # default review model
ollama pull qwen2.5-coder:1.5b  # default triage model
docker compose up -d --build
curl -fsS http://localhost:8000/healthz   # {"status":"ok"}
```

`docker compose up` requires `.env`. Without it `POSTGRES_PASSWORD` is empty and Postgres crash-loops (see [Troubleshooting](#troubleshooting)). Also set `DOCKER_GID` as described in [Deployment](#deployment).

Sandbox images built by `docker/sandbox/build.sh`: `pr-review-sandbox-python:3.12`, `pr-review-sandbox-node:20`, `pr-review-sandbox-go:1.22`, `pr-review-sandbox-rust:1.83`. The sandbox only runs images on a fixed allowlist (these four plus `python:3.12-slim` and `alpine:3.20`).

## GitHub App setup

Create the App under your account or organization (Settings, Developer settings, GitHub Apps, New GitHub App).

1. **Webhook URL:** `https://<your-host>/webhook/github`
2. **Webhook secret:** a long random string. Put the same value in `GITHUB_WEBHOOK_SECRET`.
3. **Repository permissions** (least privilege, derived from what the code does: reads PRs and clones the repo, posts a review):
   - Pull requests: Read and write
   - Contents: Read-only
   - Metadata: Read-only (always granted)
4. **Subscribe to events:** Pull request. The service acts on the actions `opened`, `synchronize`, `reopened` and `ready_for_review`; other events and actions get `202 ignored`, and `ping` gets `200 pong`.
5. Create the App, note its **App ID** (`GITHUB_APP_ID`), and generate a **private key** (PEM).
6. Provide the key through exactly one of:
   - `GITHUB_PRIVATE_KEY`: the inline PEM, newlines written as `\n` inside double quotes (simplest with Compose)
   - `GITHUB_PRIVATE_KEY_PATH`: a path readable by the process (in Compose you must mount it into the container)

   Setting both, or neither, is a startup error.
7. Install the App on the repositories you want reviewed.

The service authenticates as the App and uses short-lived installation tokens. Webhook signatures are verified with `GITHUB_WEBHOOK_SECRET`; requests are limited to 5 MiB, and rate-limited per client IP and per installation.

## Environment variables

Copy `.env.example` to `.env`. Variables are read by `src/pr_review/core/settings.py` (names are case-insensitive). Invalid or missing required values stop startup with a message naming the fields, never the values.

### Required

| Variable | Description |
|---|---|
| `GITHUB_APP_ID` | Integer App ID |
| `GITHUB_PRIVATE_KEY` or `GITHUB_PRIVATE_KEY_PATH` | App private key (exactly one) |
| `GITHUB_WEBHOOK_SECRET` | Webhook HMAC secret |
| `DATABASE_URL` | SQLAlchemy URL, e.g. `postgresql+psycopg://user:pass@host:5432/pr_review` |
| `REDIS_URL` | e.g. `redis://localhost:6379` |

### Compose-only

| Variable | Description |
|---|---|
| `POSTGRES_PASSWORD` | Password for the bundled Postgres; Compose also uses it to build the container `DATABASE_URL` |
| `DOCKER_GID` | Group id granted to the worker for the Docker socket (default `65534`, which gives no access) |

In Compose, `DATABASE_URL` and `REDIS_URL` are overridden to point at the `postgres` and `redis` services; the values in `.env` are for running outside Compose.

### LLM

| Variable | Default | Description |
|---|---|---|
| `LLM_REVIEW_MODEL` | `ollama/qwen2.5-coder:7b` | Model for the review step |
| `LLM_TRIAGE_MODEL` | `ollama/qwen2.5-coder:1.5b` | Model for triage |
| `LLM_FALLBACK_MODELS` | `[]` | JSON list tried after the primary, e.g. `["ollama/qwen2.5-coder:0.5b"]` |
| `LLM_API_KEY` | unset | Key for hosted providers; not needed for Ollama |
| `OLLAMA_API_BASE` | `http://localhost:11434` in code; `.env.example` sets `http://host.docker.internal:11434` | Ollama endpoint |
| `LLM_JOB_BUDGET_USD` | `1.0` | Per-job spend cap |
| `LLM_DAILY_BUDGET_USD` | `20.0` | Daily spend cap |

### Behaviour

| Variable | Default | Description |
|---|---|---|
| `ADMIN_TOKEN` | unset | Bearer token for `/admin/*`; unset disables the admin API |
| `RETENTION_DAYS` | `30` | Days to keep job data |
| `RETENTION_HOUR_UTC` | `3` | Hour (0-23 UTC) of the daily retention purge |
| `REQUEST_CHANGES_ON_CRITICAL` | `false` | Submit the review as REQUEST_CHANGES when a critical finding exists; otherwise the review event is COMMENT |

### Worker and sandbox

| Variable | Default | Description |
|---|---|---|
| `WORKER_MAX_JOBS` | `4` | Concurrent jobs per worker process |
| `WORKER_LEASE_SECONDS` | `300` | Job lease TTL (crash recovery window) |
| `JOB_DEADLINE_SECONDS` | `3600` | Time from job creation before it is dead-lettered |
| `STAGE_TIMEOUT_SECONDS` | `300` | Default per-stage timeout |
| `ANALYSE_TIMEOUT_SECONDS` | `1200` | LLM analysis stage timeout (raise for slow local models) |
| `SWEEP_INTERVAL_SECONDS` | `30` | Sweeper/recovery period (1-60) |
| `WORKSPACE_ROOT` | system temp | Parent directory of ephemeral checkouts |
| `WORKSPACE_MAX_BYTES` | `1073741824` | Checkout size cap |
| `SANDBOX_MAX_REPO_BYTES` | `1073741824` | Repo copy cap per sandbox run |
| `SANDBOX_MEMORY_BYTES` | `536870912` | Memory limit per sandbox container |
| `SANDBOX_CPUS` | `1.0` | CPU limit per sandbox container |
| `SANDBOX_PIP_INDEX_URL` | unset | Optional pip index override (`http(s)://host[:port]/path`, e.g. a caching mirror). Its host:port is the only extra egress destination; unset uses real PyPI. Applies to pip only |
| `TEST_TIMEOUT_SECONDS` | `300` | Test budget; a repo config may only lower it |

## Ollama and LiteLLM switching

Models are named in LiteLLM format, `<provider>/<model>`. Models whose name starts with `ollama/` or `ollama_chat/` are sent to `OLLAMA_API_BASE`; every other model uses `LLM_API_KEY`.

**Local Ollama (default):**

```sh
ollama pull qwen2.5-coder:7b && ollama pull qwen2.5-coder:1.5b
# .env
LLM_REVIEW_MODEL=ollama/qwen2.5-coder:7b
LLM_TRIAGE_MODEL=ollama/qwen2.5-coder:1.5b
OLLAMA_API_BASE=http://host.docker.internal:11434
```

Compose maps `host.docker.internal` to the host gateway so containers on Linux can reach Ollama on the host. Ollama must listen on an address reachable from the containers (not only loopback) when the app runs in Docker.

**Hosted provider (bring your own key),** any provider LiteLLM supports, for example:

```sh
# .env
LLM_REVIEW_MODEL=anthropic/<model-name>      # or openai/<model-name>, etc.
LLM_TRIAGE_MODEL=anthropic/<smaller-model>
LLM_API_KEY=<your provider key>
```

Check the LiteLLM provider documentation for the exact model string. A single `LLM_API_KEY` is used for all non-Ollama models, so use one provider at a time or rely on an Ollama fallback. `LLM_FALLBACK_MODELS` lists models tried after the primary fails. Use `LLM_JOB_BUDGET_USD` and `LLM_DAILY_BUDGET_USD` to cap spend on paid providers.

Restart the services after changing `.env` (`docker compose up -d`).

## Per-repository config

A repository may add `.pr-review.yml` (read from the PR checkout, treated as untrusted, max 64 KiB, unknown keys rejected). Supported keys:

| Key | Meaning |
|---|---|
| `test_commands` | Map of project name to argv list (run without a shell) |
| `test_timeout_seconds` | Test timeout; can only lower the operator limit |
| `ignore` | List of path globs to skip |
| `categories` | Subset of `correctness`, `security`, `performance`, `style` |
| `min_severity` | `CRITICAL`, `MAJOR`, `MINOR` or `INFO` (default `INFO`) |
| `tests_disabled` | `true` to skip running tests |

A missing or invalid file falls back to defaults and adds a notice to the review summary.

## Local development and testing

```sh
uv sync --locked
uv run ruff check src tests
uv run mypy src
uv run pytest -q
```

Tests use real services and no mocks: Postgres, Redis, a Docker daemon (tests marked `docker`), a real Ollama server (tests marked `llm`), local HTTP fault-injection servers, and the real GitHub API with a dedicated sandbox App (tests marked `github`). Plan for these before running the full suite:

- Ollama running locally with `qwen2.5-coder:7b` (review) and `qwen2.5-coder:1.5b` (triage) pulled. CI uses the smaller `qwen2.5-coder:0.5b`, selected with `PR_REVIEW_TEST_LLM_MODEL=ollama/qwen2.5-coder:0.5b`. `PR_REVIEW_TEST_OLLAMA` overrides the Ollama URL (default `http://localhost:11434`).
- Sandbox images built with `./docker/sandbox/build.sh` for sandbox tests.
- Package cache: tests that run the sandbox install start a real caching PyPI mirror (`epicwink/proxpi:1.3.1`) via the session fixture `pkg_index` in `tests/conftest.py` and pass it as `SANDBOX_PIP_INDEX_URL`. Wheels are cached in the named Docker volume `pr-review-test-pkgcache`, which is intentionally kept across runs (the mirror container itself is removed after each session), so only the first run downloads. Remove it with `docker volume rm pr-review-test-pkgcache`; set `PR_REVIEW_TEST_NO_PKGCACHE=1` to test against real PyPI.
- npm, Go and Rust install tests (`tests/integration/test_install_langs.py`) need the matching sandbox images and skip with a reason if they are not built.
- Markers are declared in `pyproject.toml`: `github`, `docker`, `llm`. Select or deselect with `-m`, e.g. `uv run pytest -m "not github"`.

CI (`.github/workflows/ci.yml`) runs `ruff`, `mypy`, the test suite with `--cov-fail-under=85` against a local Ollama, `pip-audit`, and `scripts/check_no_todos.sh`.

To run the services from source instead of Compose, start Postgres and Redis, set the required variables, then:

```sh
uv run alembic upgrade head
uv run uvicorn pr_review.api.app:create_app --factory --port 8000
uv run arq pr_review.pipeline.queue.WorkerSettings
```

## Deployment

1. Provision a Linux host with Docker (preferably rootless, see [Host hardening](#host-hardening)).
2. `cp .env.example .env` and set strong values for `GITHUB_WEBHOOK_SECRET`, `POSTGRES_PASSWORD`, `ADMIN_TOKEN`, plus the GitHub App and LLM settings. Keep `DATABASE_URL` in `.env` consistent with `POSTGRES_PASSWORD`.
3. Set `DOCKER_GID` in `.env` to the group that owns the Docker socket:
   - Linux: `stat -c %g /var/run/docker.sock`
   - Docker Desktop on macOS: `DOCKER_GID=0`
4. Build sandbox images: `./docker/sandbox/build.sh`.
5. Start: `docker compose up -d --build`. Order is enforced: Postgres healthy, then `migrate` completes, then `api` and `worker` start (Redis must also be healthy).
6. Put a TLS-terminating reverse proxy in front of port 8000 and point the GitHub App webhook at `https://<host>/webhook/github`. Only `/webhook/github` needs to be reachable from GitHub; do not expose `/metrics` or `/admin` publicly.
7. Verify: `curl -fsS https://<host>/healthz`, then open a test PR in an installed repository.

Endpoints on the api service (port 8000):

| Endpoint | Purpose |
|---|---|
| `POST /webhook/github` | GitHub webhook receiver |
| `GET /healthz` | Liveness, returns `{"status":"ok"}`; used by the image `HEALTHCHECK` |
| `GET /readyz` | Readiness; `503` if the database or Redis check fails |
| `GET /metrics` | Prometheus metrics |
| `/admin/*` | Admin API, bearer-token protected |

Compose services have resource limits (api 1 CPU/512 MB, worker 2 CPU/2 GB, Postgres 1 CPU/1 GB, Redis 0.5 CPU/512 MB). Data persists in the `pgdata` and `redisdata` volumes. The worker healthcheck pings the Docker daemon, so an unhealthy worker usually means the socket is not accessible.

## Operator CLI and admin API

The module `pr_review.pipeline.cli` (argparse program name `pr-review`) queries the database directly and prints JSON. `pyproject.toml` does not currently define a `[project.scripts]` entry, so no `pr-review` executable is installed; invoke the module:

```sh
uv run python -m pr_review.pipeline.cli jobs list [--status STATUS]
uv run python -m pr_review.pipeline.cli jobs show <job-uuid>
uv run python -m pr_review.pipeline.cli dlq list
uv run python -m pr_review.pipeline.cli dlq show <job-uuid>
uv run python -m pr_review.pipeline.cli dlq requeue <job-uuid>
uv run python -m pr_review.pipeline.cli costs
```

In Compose, run it in the api container (the entrypoint passes unknown commands through):

```sh
docker compose run --rm api python -m pr_review.pipeline.cli dlq list
```

Exit codes: `0` ok, `1` not found (or not in the dead-letter queue), `2` usage or configuration error.

The same operations are available over HTTP with `Authorization: Bearer $ADMIN_TOKEN`:

| Endpoint | Purpose |
|---|---|
| `GET /admin/jobs` | List jobs |
| `GET /admin/jobs/{job_id}` | Job detail |
| `GET /admin/jobs/{job_id}/timeline` | Event timeline |
| `GET /admin/dlq`, `GET /admin/dlq/{job_id}` | Dead-letter queue |
| `POST /admin/dlq/{job_id}/requeue` | Requeue a dead-lettered job |
| `GET /admin/costs` | LLM cost summary |

With `ADMIN_TOKEN` unset these return `503`; a wrong token returns `401`.

## Host hardening

The worker launches sandboxes through the Docker daemon, so **access to the Docker socket is root-equivalent on the daemon's host**. The socket is mounted only into `worker`, never into `api`. Treat the worker host as a trust boundary:

- **Rootless Docker (recommended).** Run the daemon rootless (`dockerd-rootless-setuptool.sh install`) so a sandbox escape lands in an unprivileged user, not root. Mount the rootless socket (typically `$XDG_RUNTIME_DIR/docker.sock`) in place of `/var/run/docker.sock` in the worker's `volumes` in `docker-compose.yml`, and set `DOCKER_GID` to the socket's group as seen on the host. Docker Desktop is not rootless; the code only reports this, it does not enforce it.
- **gVisor (`runsc`).** Install gVisor, register the runtime in `/etc/docker/daemon.json` (`"runtimes": {"runsc": {"path": "/usr/local/bin/runsc"}}`), and set `"default-runtime": "runsc"` so sandbox containers run in a user-space kernel. The sandbox does not select a runtime itself, so making `runsc` the daemon default is how it is applied. Combine it with rootless Docker where possible.
- **Dedicated host.** Run the worker on a machine that holds no other secrets or workloads.
- **Sandbox properties already enforced** (`src/pr_review/runner/sandbox.py`): non-root user, all capabilities dropped, default seccomp profile, `no-new-privileges`, read-only root filesystem, tmpfs `/tmp`, no network, no swap, process/CPU/memory/ulimit caps, empty environment, wall-clock kill, repo mounted read-only, only allowlisted images. Repository code never runs outside the sandbox.
- **Dependency install** is the only sandbox step with network access: it runs on a per-run internal Docker network behind an egress proxy that permits only `CONNECT <allowlisted host>:443`, with `pip --only-binary=:all:` by default so no source builds run.
- **Secrets:** keep `.env` out of version control (it is git-ignored) and with mode `600`; secrets are never passed into sandboxes, prompts, logs or the database.
- **Network:** expose only the reverse proxy; keep Postgres and Redis off public interfaces (the compose file publishes only port 8000). Restrict `/admin` and `/metrics` at the proxy.
- **Data:** job data is purged after `RETENTION_DAYS`.

## Network egress and telemetry

The product makes only these outbound connections:

| Destination | Purpose | When |
|---|---|---|
| GitHub (`api.github.com` for the API; `https` clone of the repository from GitHub) | Installation tokens, PR data, checkout, posting the review | Always |
| The configured LLM endpoint | Review and triage prompts, which contain code from the PR | Local Ollama at `OLLAMA_API_BASE` by default (no third party); a hosted provider only if you configure one |
| Allowlisted package registries: `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`, `proxy.golang.org`, `index.crates.io`, `static.crates.io` | Dependency install inside the sandbox, via the allowlist egress proxy | During the install step (the installer currently uses pip only) |

With the default local Ollama, repository code is sent to no third party. **No telemetry or usage data is sent to the vendor or any analytics service.** LiteLLM callbacks and logging hooks are disabled in `src/pr_review/llm/provider.py`, and its telemetry flag is set off where it exists. Test execution inside the sandbox has no network at all. If you use a hosted LLM, code from reviewed PRs is sent to that provider under its terms.

## Known limitations

- GitHub retry loops are not yet consolidated: `client.py`, `auth.py` and `posting.py` each carry their own.
- Findings carry their source as a summary prefix rather than as a dedicated field.
- Suggestion blocks are single-line only.
- The webhook rate limiter fails open when Redis is down. This is deliberate: GitHub redelivery and the signature check still apply.

## Troubleshooting

**Postgres crash-loops on first start.** `.env` is missing, so `POSTGRES_PASSWORD` is empty and Postgres refuses to initialize. Run `cp .env.example .env`, set a password, then `docker compose down -v && docker compose up -d` (the `-v` discards the half-initialized volume; only do this on a fresh install).

**Worker is `unhealthy` or cannot start sandboxes.** `DOCKER_GID` does not match the group of the Docker socket. The default `65534` grants no access. On Linux use `stat -c %g /var/run/docker.sock`; on Docker Desktop for macOS use `DOCKER_GID=0`. Then `docker compose up -d worker`.

**`Invalid configuration: ...` at startup.** A required variable is missing or malformed. The message lists the field names. Common causes: both or neither of `GITHUB_PRIVATE_KEY` / `GITHUB_PRIVATE_KEY_PATH`, a non-integer `GITHUB_APP_ID`, an unreadable key path, or `LLM_FALLBACK_MODELS` that is not a JSON list.

**Webhook returns 401.** `GITHUB_WEBHOOK_SECRET` differs from the App's webhook secret. **400** means a malformed payload; **413** a body over 5 MiB; **429/503** rate limiting or an unavailable limiter or queue. Check recent deliveries in the App's Advanced tab. **202 `ignored`** is normal for events and actions that are not reviewed.

**No review appears.** Check `docker compose logs worker`, `GET /readyz`, and `jobs list` / `dlq list` from the [CLI](#operator-cli-and-admin-api). Confirm the App is installed on the repository and has Pull requests (write) and Contents (read) permissions. Dead-lettered jobs can be requeued with `dlq requeue`.

**LLM errors or timeouts.** Confirm the model is pulled (`ollama list`) and that `OLLAMA_API_BASE` is reachable from inside the container (`docker compose exec worker python -c "import urllib.request as u; print(u.urlopen('http://host.docker.internal:11434').read())"`). The 7b model on CPU-only hosts is slow; consider a smaller model or a hosted provider. A job that hits `LLM_JOB_BUDGET_USD` or the daily budget stops calling the LLM.

**Sandbox dependency install fails with `ReadTimeout`.** The install step downloads from the real PyPI through the allowlist proxy. On slow or restricted networks pip can time out. Retry by pushing a new commit or requeueing, and check that the host can reach `pypi.org` and `files.pythonhosted.org`. A denied host appears as a `403` from the proxy and means the host is not on the allowlist.

**Sandbox image not found or not allowed.** Run `./docker/sandbox/build.sh`; image tags must match the allowlist exactly (`pr-review-sandbox-python:3.12`, `-node:20`, `-go:1.22`, `-rust:1.83`).

**Tests fail locally.** Real services are required: Docker daemon, Postgres, Redis and Ollama with the expected models (`qwen2.5-coder:7b` and `:1.5b` locally, `:0.5b` in CI via `PR_REVIEW_TEST_LLM_MODEL`). Deselect groups with `-m "not docker"`, `-m "not llm"` or `-m "not github"`.
