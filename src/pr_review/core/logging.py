"""JSON logging with redaction and delivery/job/stage correlation IDs."""

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime

from pr_review.core.redaction import redact, redact_obj

_CORRELATION = ("delivery_id", "job_id", "stage")
_ctx: dict[str, ContextVar[str | None]] = {k: ContextVar(k, default=None) for k in _CORRELATION}
_STD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def bind(**ids: str | None) -> None:
    """Set correlation IDs (delivery_id, job_id, stage) for the current context."""
    for k, v in ids.items():
        _ctx[k].set(v)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        out.update({k: c.get() for k, c in _ctx.items()})
        out.update({k: redact_obj(v) for k, v in record.__dict__.items() if k not in _STD})
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            out["stack"] = redact(self.formatStack(record.stack_info))
        return json.dumps(out)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
