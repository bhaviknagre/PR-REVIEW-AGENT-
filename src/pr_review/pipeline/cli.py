"""Operator CLI against the database directly (same queries as the admin API).

Exit codes: 0 ok, 1 not found / not in dead letter, 2 usage or configuration error.
"""

import argparse
import json
import sys
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from pr_review.api import admin
from pr_review.core.settings import get_settings


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pr-review")
    sub = p.add_subparsers(dest="cmd", required=True)
    jobs = sub.add_parser("jobs").add_subparsers(dest="sub", required=True)
    jobs.add_parser("list").add_argument("--status")
    jobs.add_parser("show").add_argument("job_id", type=uuid.UUID)
    dlq = sub.add_parser("dlq").add_subparsers(dest="sub", required=True)
    dlq.add_parser("list")
    for name in ("show", "requeue"):
        dlq.add_parser(name).add_argument("job_id", type=uuid.UUID)
    sub.add_parser("costs")
    return p


def _run(s: Session, a: argparse.Namespace) -> Any:
    match a.cmd, getattr(a, "sub", None):
        case "jobs", "list":
            return admin.list_jobs(s, a.status)
        case "jobs", "show":
            return admin.job_detail(s, a.job_id)
        case "dlq", "list":
            return admin.dlq_list(s)
        case "dlq", "show":
            d = admin.job_detail(s, a.job_id)
            return d if d and d["status"] == "dead_letter" else None
        case "dlq", "requeue":
            return {"id": str(a.job_id), "status": "queued"} if admin.requeue(s, a.job_id) else None
        case _:
            return admin.cost_summary(s)


def main(argv: Sequence[str] | None = None, database_url: str | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    try:
        url = database_url or get_settings().database_url.get_secret_value()
        engine = create_engine(url)
    except (SystemExit, Exception):  # invalid config; the message names fields, never values
        print("error: configuration invalid (DATABASE_URL)", file=sys.stderr)
        return 2
    try:
        with Session(engine) as s:
            out = _run(s, args)
    except Exception as exc:  # never echo driver text: it can contain the connection string
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()
    if out is None:
        print("error: not found", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
