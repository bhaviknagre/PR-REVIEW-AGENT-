"""Loader for the untrusted per-repo `.pr-review.yml` (FR-11); falls back to defaults."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

CONFIG_NAME = ".pr-review.yml"
MAX_BYTES = 64 * 1024
MAX_EVENTS = 5000
MAX_DEPTH = 16

Category = Literal["correctness", "security", "performance", "style"]
Severity = Literal["CRITICAL", "MAJOR", "MINOR", "INFO"]
SEVERITY_ORDER: tuple[Severity, ...] = ("CRITICAL", "MAJOR", "MINOR", "INFO")

_Arg = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=512, pattern=r"^[^\x00]*$")
]
_Glob = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=256, pattern=r"^[^\x00]*$")
]
_Name = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=64, pattern=r"^[^\x00]*$")
]


@dataclass(frozen=True)
class RepoLimits:
    """Operator ceilings the repo config may lower but never raise."""

    max_test_timeout_seconds: float


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    # project name -> argv (executed without a shell)
    test_commands: dict[_Name, Annotated[list[_Arg], Field(min_length=1, max_length=64)]] = Field(
        default_factory=dict, max_length=16
    )
    test_timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    ignore: list[_Glob] = Field(default_factory=list, max_length=200)
    categories: list[Category] = Field(
        default=["correctness", "security", "performance", "style"], max_length=4
    )
    min_severity: Severity = "INFO"
    tests_disabled: bool = False


def _read(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # ELOOP on symlink
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ValueError("not a regular file")
        data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("file too large")
    return data.decode("utf-8")


def _check_shape(text: str) -> None:
    """Reject aliases (bombs) and absurd size/depth before building any objects."""
    depth = events = 0
    for ev in yaml.parse(text, Loader=yaml.SafeLoader):
        events += 1
        if events > MAX_EVENTS:
            raise ValueError("too many nodes")
        if isinstance(ev, yaml.AliasEvent):
            raise ValueError("aliases not allowed")
        if isinstance(ev, yaml.CollectionStartEvent):
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError("too deeply nested")
        elif isinstance(ev, yaml.CollectionEndEvent):
            depth -= 1


def load_repo_config(workspace: Path, limits: RepoLimits) -> tuple[RepoConfig, str | None]:
    path = workspace / CONFIG_NAME
    try:
        text = _read(path)
        _check_shape(text)
        raw = yaml.safe_load(text)
        cfg = RepoConfig() if raw is None else RepoConfig.model_validate(raw)
    except FileNotFoundError:
        return RepoConfig(), None
    except ValidationError as exc:
        fields = ", ".join(sorted({".".join(map(str, e["loc"])) or "<root>" for e in exc.errors()}))
        return RepoConfig(), f"{CONFIG_NAME} invalid ({fields[:200]}); using defaults."
    except (OSError, ValueError, yaml.YAMLError) as exc:
        kind = "unreadable or a symlink" if isinstance(exc, OSError) else type(exc).__name__
        if isinstance(exc, ValueError) and not isinstance(exc, UnicodeDecodeError):
            kind = str(exc)
        return RepoConfig(), f"{CONFIG_NAME} ignored ({kind}); using defaults."
    t = cfg.test_timeout_seconds
    if t is not None and t > limits.max_test_timeout_seconds:
        cfg = cfg.model_copy(update={"test_timeout_seconds": limits.max_test_timeout_seconds})
    return cfg, None
