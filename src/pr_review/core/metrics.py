"""Prometheus metrics on a dedicated registry. Labels are bounded: no repo/PR/job labels."""

from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

QUEUE_DEPTH = Gauge("pr_review_queue_depth", "Jobs waiting in the queue", registry=REGISTRY)
DLQ_SIZE = Gauge("pr_review_dead_letter_jobs", "Jobs currently dead-lettered", registry=REGISTRY)
STAGE_LATENCY = Histogram(
    "pr_review_stage_latency_seconds", "Pipeline stage duration", ["stage"], registry=REGISTRY
)
LLM_COST = Counter("pr_review_llm_cost_usd_total", "LLM spend in USD", ["model"], registry=REGISTRY)
LLM_TOKENS = Counter(
    "pr_review_llm_tokens_total", "LLM tokens", ["model", "kind"], registry=REGISTRY
)
JOBS = Counter("pr_review_jobs_total", "Jobs by terminal status", ["status"], registry=REGISTRY)


def observe_stage(stage: str, seconds: float) -> None:
    STAGE_LATENCY.labels(stage).observe(seconds)


@contextmanager
def time_stage(stage: str) -> Iterator[None]:
    start = perf_counter()
    try:
        yield
    finally:
        observe_stage(stage, perf_counter() - start)


def record_llm_call(
    model: str, prompt_tokens: int, completion_tokens: int, cost_usd: float
) -> None:
    LLM_COST.labels(model).inc(cost_usd)
    LLM_TOKENS.labels(model, "prompt").inc(prompt_tokens)
    LLM_TOKENS.labels(model, "completion").inc(completion_tokens)


def record_job_finished(status: str) -> None:
    JOBS.labels(status).inc()


def set_queue_gauges(queue_depth: int, dead_letter: int) -> None:
    QUEUE_DEPTH.set(queue_depth)
    DLQ_SIZE.set(dead_letter)
