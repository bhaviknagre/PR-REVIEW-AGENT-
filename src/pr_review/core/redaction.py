"""Redaction of secrets from text: known token formats, registered values, high-entropy strings."""

import math
import os
import re
from collections import Counter
from typing import Any

from pydantic import BaseModel, SecretStr

MASK = "[REDACTED]"
_MIN_SECRET_LEN = 6  # shorter values would shred normal text
_SENSITIVE_ENV = re.compile(r"(SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|URL)", re.I)

_PATTERNS = [
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----|[\s\S]*)"
    ),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\b(bearer\s+)[\w.~+/=-]{8,}"),
    re.compile(r"(?<=://)([^/\s:@]+:)[^@\s/]+(?=@)"),
]
_PATTERN_REPL = [MASK, MASK, MASK, MASK, rf"\1{MASK}", rf"\1{MASK}"]
_CANDIDATE = re.compile(r"[A-Za-z0-9_+=-]{24,}")
_HEX = re.compile(r"[0-9a-fA-F]+")
_ENTROPY_BITS = 4.0

_values: set[str] = set()


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _is_secret_like(tok: str) -> bool:
    return (
        not _HEX.fullmatch(tok)  # SHAs / digests are public identifiers
        and any(c.isdigit() for c in tok)
        and any(c.isalpha() for c in tok)
        and _entropy(tok) >= _ENTROPY_BITS
    )


def register_secrets(*values: str) -> None:
    """Add exact values (e.g. configured secrets) that must never appear in output."""
    _values.update(v for v in values if len(v) >= _MIN_SECRET_LEN)


def register_settings(settings: BaseModel) -> None:
    """Register every SecretStr field of a settings object, plus sensitive process env values."""
    for name in type(settings).model_fields:
        v = getattr(settings, name)
        if isinstance(v, SecretStr):
            register_secrets(v.get_secret_value())
    register_secrets(*(v for k, v in os.environ.items() if _SENSITIVE_ENV.search(k)))


def redact(text: str) -> str:
    for pat, repl in zip(_PATTERNS, _PATTERN_REPL, strict=True):
        text = pat.sub(repl, text)
    for v in sorted(_values, key=len, reverse=True):
        text = text.replace(v, MASK)
    return _CANDIDATE.sub(lambda m: MASK if _is_secret_like(m.group()) else m.group(), text)


def redact_obj(obj: Any) -> Any:
    """Redact strings recursively; non-JSON scalars are stringified then redacted."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {redact(str(k)): redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set):
        return [redact_obj(v) for v in obj]
    if obj is None or isinstance(obj, bool | int | float):
        return obj
    return redact(str(obj))
