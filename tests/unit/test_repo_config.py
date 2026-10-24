import time
from pathlib import Path

import pytest

from pr_review.core.repo_config import MAX_BYTES, RepoConfig, RepoLimits, load_repo_config

LIM = RepoLimits(max_test_timeout_seconds=300)


def load(tmp_path: Path, text: str) -> tuple[RepoConfig, str | None]:
    (tmp_path / ".pr-review.yml").write_text(text)
    return load_repo_config(tmp_path, LIM)


def test_missing_gives_defaults_no_notice(tmp_path: Path) -> None:
    cfg, notice = load_repo_config(tmp_path, LIM)
    assert (cfg, notice) == (RepoConfig(), None)
    assert (
        cfg.min_severity == "INFO" and not cfg.tests_disabled and cfg.test_timeout_seconds is None
    )
    assert cfg.categories == ["correctness", "security", "performance", "style"]


def test_empty_file_is_defaults(tmp_path: Path) -> None:
    assert load(tmp_path, "") == (RepoConfig(), None)


def test_full_config(tmp_path: Path) -> None:
    cfg, notice = load(
        tmp_path,
        """
test_commands:
  api: [pytest, -q, "-k", "not slow"]
test_timeout_seconds: 60
ignore: ["vendor/**", "*.min.js"]
categories: [security, correctness]
min_severity: MAJOR
tests_disabled: true
""",
    )
    assert notice is None
    assert cfg.test_commands == {"api": ["pytest", "-q", "-k", "not slow"]}
    assert cfg.test_timeout_seconds == 60
    assert cfg.ignore == ["vendor/**", "*.min.js"]
    assert cfg.categories == ["security", "correctness"]
    assert cfg.min_severity == "MAJOR" and cfg.tests_disabled


def test_timeout_clamped_to_operator_max(tmp_path: Path) -> None:
    cfg, notice = load(tmp_path, "test_timeout_seconds: 99999")
    assert notice is None and cfg.test_timeout_seconds == 300


@pytest.mark.parametrize(
    "text",
    [
        "bogus: 1",
        "tests_disabled: 'yes'",
        "min_severity: HIGH",
        "categories: [nope]",
        "test_timeout_seconds: -1",
        "test_commands: {api: 'pytest -q; rm -rf /'}",  # shell string rejected
        "test_commands: {api: []}",
        'test_commands: {api: ["a\\0b"]}',
        "ignore: [" + ",".join(["x"] * 201) + "]",
        "- just\n- a list",
    ],
)
def test_invalid_gives_defaults_and_notice(tmp_path: Path, text: str) -> None:
    cfg, notice = load(tmp_path, text)
    assert cfg == RepoConfig() and notice is not None and len(notice) < 300


def test_notice_does_not_echo_values(tmp_path: Path) -> None:
    _, notice = load(tmp_path, "min_severity: SECRETVALUE")
    assert notice is not None and "SECRETVALUE" not in notice and "min_severity" in notice


def test_bad_yaml(tmp_path: Path) -> None:
    cfg, notice = load(tmp_path, "a: [unclosed")
    assert cfg == RepoConfig() and notice is not None


def test_oversize(tmp_path: Path) -> None:
    cfg, notice = load(tmp_path, "# " + "x" * MAX_BYTES)
    assert cfg == RepoConfig() and notice is not None and "large" in notice


def test_alias_bomb_fast(tmp_path: Path) -> None:
    lines = ["a: &a [x,x,x,x,x,x,x,x,x]"]
    for i in range(9):
        p = chr(ord("a") + i)
        lines.append(f"{chr(ord(p) + 1)}: &{chr(ord(p) + 1)} [{','.join(['*' + p] * 9)}]")
    start = time.monotonic()
    cfg, notice = load(tmp_path, "\n".join(lines))
    assert time.monotonic() - start < 1
    assert cfg == RepoConfig() and notice is not None


def test_deep_nesting_rejected(tmp_path: Path) -> None:
    _, notice = load(tmp_path, "[" * 100 + "]" * 100)
    assert notice is not None


def test_symlink_ignored(tmp_path: Path) -> None:
    target = tmp_path / "real.yml"
    target.write_text("tests_disabled: true")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".pr-review.yml").symlink_to(target)
    cfg, notice = load_repo_config(ws, LIM)
    assert not cfg.tests_disabled and notice is not None


def test_directory_not_followed(tmp_path: Path) -> None:
    (tmp_path / ".pr-review.yml").mkdir()
    cfg, notice = load_repo_config(tmp_path, LIM)
    assert cfg == RepoConfig() and notice is not None
