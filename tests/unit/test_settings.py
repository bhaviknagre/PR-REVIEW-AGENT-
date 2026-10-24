import pytest

from pr_review.core.settings import Settings, get_settings

REQUIRED = {
    "GITHUB_APP_ID": "1",
    "GITHUB_PRIVATE_KEY": "pk-secret",
    "GITHUB_WEBHOOK_SECRET": "whsec-secret",
    "DATABASE_URL": "postgresql://u:dbpass@h/db",
    "REDIS_URL": "redis://h:6379/0",
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir("/")  # no stray .env
    for k in [
        *REQUIRED,
        "LLM_API_KEY",
        "GITHUB_PRIVATE_KEY_PATH",
        "RETENTION_DAYS",
        "SANDBOX_MAX_REPO_BYTES",
        "RETENTION_HOUR_UTC",
        "GITHUB_MAX_DIFF_BYTES",
        "GITHUB_MAX_FILES",
    ]:
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = get_settings()
    assert s.retention_days == 30
    assert s.request_changes_on_critical is False
    assert s.llm_api_key is None
    assert (s.worker_max_jobs, s.worker_lease_seconds) == (4, 300)
    assert (s.job_deadline_seconds, s.stage_timeout_seconds, s.sweep_interval_seconds) == (
        3600,
        300.0,
        30,
    )
    assert s.llm_review_model == "ollama/qwen2.5-coder:7b"
    assert s.llm_triage_model == "ollama/qwen2.5-coder:1.5b"
    assert s.github_webhook_secret.get_secret_value() == "whsec-secret"


def test_secrets_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LLM_API_KEY", "sk-leak")
    text = repr(Settings()) + str(Settings())
    for secret in ("pk-secret", "whsec-secret", "dbpass", "sk-leak"):
        assert secret not in text


def test_missing_required_fails_fast() -> None:
    with pytest.raises(SystemExit) as e:
        get_settings()
    msg = str(e.value)
    assert "GITHUB_WEBHOOK_SECRET" in msg and "DATABASE_URL" in msg


def test_invalid_value_message_hides_input(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("RETENTION_DAYS", "0")
    with pytest.raises(SystemExit) as e:
        get_settings()
    assert "RETENTION_DAYS" in str(e.value)


@pytest.mark.parametrize(
    "name",
    ["WORKER_MAX_JOBS", "WORKER_LEASE_SECONDS", "JOB_DEADLINE_SECONDS", "STAGE_TIMEOUT_SECONDS"],
)
def test_worker_fields_reject_non_positive(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(name, "0")
    with pytest.raises(SystemExit) as e:
        get_settings()
    assert name in str(e.value)


def test_worker_fields_from_env_and_sweep_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("WORKER_MAX_JOBS", "9")
    assert get_settings().worker_max_jobs == 9
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "61")
    get_settings.cache_clear()
    with pytest.raises(SystemExit):
        get_settings()


def test_admin_token_optional_and_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    assert get_settings().admin_token is None
    get_settings.cache_clear()
    monkeypatch.setenv("ADMIN_TOKEN", "tok-123456")
    s = get_settings()
    assert s.admin_token is not None and "tok-123456" not in repr(s)


def test_llm_model_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LLM_REVIEW_MODEL", "ollama/a")
    monkeypatch.setenv("LLM_TRIAGE_MODEL", "ollama/b")
    s = Settings()
    assert (s.llm_review_model, s.llm_triage_model) == ("ollama/a", "ollama/b")


def test_pipeline_limits_defaults_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = get_settings()
    assert s.workspace_root is None
    assert s.workspace_max_bytes == s.sandbox_max_repo_bytes == 1 << 30  # SPEC: repos to 1 GB
    assert (s.sandbox_memory_bytes, s.sandbox_cpus) == (512 << 20, 1.0)
    assert (s.test_timeout_seconds, s.retention_hour_utc) == (300.0, 3)
    monkeypatch.setenv("SANDBOX_MAX_REPO_BYTES", "2048")
    monkeypatch.setenv("RETENTION_HOUR_UTC", "5")
    get_settings.cache_clear()
    s = get_settings()
    assert (s.sandbox_max_repo_bytes, s.retention_hour_utc) == (2048, 5)
    monkeypatch.setenv("RETENTION_HOUR_UTC", "24")
    get_settings.cache_clear()
    with pytest.raises(SystemExit):
        get_settings()


def _env(monkeypatch: pytest.MonkeyPatch, **over: str) -> None:
    for k, v in {**REQUIRED, **over}.items():
        monkeypatch.setenv(k, v)


def test_private_key_from_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: ANN001
    pem = tmp_path / "k.pem"
    pem.write_text("pem-from-file")
    _env(monkeypatch, GITHUB_PRIVATE_KEY_PATH=str(pem))
    monkeypatch.delenv("GITHUB_PRIVATE_KEY")
    assert get_settings().github_private_key.get_secret_value() == "pem-from-file"


def test_private_key_exactly_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, GITHUB_PRIVATE_KEY_PATH="/some/k.pem")  # both set
    with pytest.raises(SystemExit, match="exactly one of GITHUB_PRIVATE_KEY"):
        get_settings()
    monkeypatch.delenv("GITHUB_PRIVATE_KEY_PATH")
    monkeypatch.delenv("GITHUB_PRIVATE_KEY")  # neither set
    get_settings.cache_clear()
    with pytest.raises(SystemExit, match="GITHUB_PRIVATE_KEY"):
        get_settings()


def test_private_key_path_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, GITHUB_PRIVATE_KEY_PATH="/nonexistent/k.pem")
    monkeypatch.delenv("GITHUB_PRIVATE_KEY")
    with pytest.raises(SystemExit, match="cannot read GITHUB_PRIVATE_KEY_PATH"):
        get_settings()


def test_github_limits_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert (s.github_max_diff_bytes, s.github_max_files) == (2_000_000, 300)
    monkeypatch.setenv("GITHUB_MAX_DIFF_BYTES", "5000")
    monkeypatch.setenv("GITHUB_MAX_FILES", "7")
    s = Settings()
    assert (s.github_max_diff_bytes, s.github_max_files) == (5000, 7)


@pytest.mark.parametrize("name", ["GITHUB_MAX_DIFF_BYTES", "GITHUB_MAX_FILES"])
def test_github_limits_must_be_positive(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(name, "0")
    with pytest.raises(ValueError):
        Settings()


def test_analyse_timeout_default_env_and_stage_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from pr_review.core.settings import WorkerConfig
    from pr_review.pipeline.lease import stage_timeout

    monkeypatch.delenv("ANALYSE_TIMEOUT_SECONDS", raising=False)
    assert WorkerConfig().analyse_timeout_seconds == 1200.0
    monkeypatch.setenv("ANALYSE_TIMEOUT_SECONDS", "1800")
    cfg = WorkerConfig()
    assert stage_timeout("analyse", cfg, 10_000) == 1800.0
    assert stage_timeout("analyse", cfg, 50) == 50  # never beyond the job deadline
    monkeypatch.setenv("ANALYSE_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError):
        WorkerConfig()
