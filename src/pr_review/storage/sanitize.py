"""Persistence-boundary sanitizer for free text stored in the database (NFR-1, NFR-5, FR-24)."""

import re
from collections.abc import Iterable
from typing import Any

from pr_review.core.redaction import redact

MAX_TEXT = 500  # stored free text is metadata-sized, never a file
MAX_LINES = 3  # more lines than this looks like a pasted code excerpt
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ELIDED = "[excerpt elided]"


def sanitize_text(text: str, limit: int = MAX_TEXT) -> str:
    """Redact secrets, strip control chars, elide multi-line excerpts, cap length."""
    lines = _CONTROL.sub("", redact(text)).replace("\r", "").split("\n")
    if len(lines) > MAX_LINES:
        lines = [*lines[: MAX_LINES - 1], ELIDED]
    return "\n".join(lines)[:limit]


def sanitize_opt(text: str | None, limit: int = MAX_TEXT) -> str | None:
    return None if text is None else sanitize_text(text, limit)


def sanitize_obj(obj: Any) -> Any:
    """sanitize_text on every string in a JSON-like structure (stored event payloads)."""
    if isinstance(obj, str):
        return sanitize_text(obj)
    if isinstance(obj, dict):
        return {k: sanitize_obj(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [sanitize_obj(v) for v in obj]
    return obj


def leaks(blob: str, canaries: Iterable[str]) -> list[str]:
    """The canary strings present in a dump or log blob."""
    return [c for c in canaries if c and c in blob]
