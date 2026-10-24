"""initial schema

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("dead_letter_reason", sa.Text(), nullable=True),
        sa.Column("leased_by", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint(
            "repo", "pr_number", "head_sha", name=op.f("uq_jobs_repo_pr_number_head_sha")
        ),
    )
    op.create_index("ix_jobs_created_at", "jobs", ["created_at"], unique=False)
    op.create_index(op.f("ix_jobs_status"), "jobs", ["status"], unique=False)
    op.create_index(
        "ix_jobs_status_lease_expires_at", "jobs", ["status", "lease_expires_at"], unique=False
    )
    op.create_table(
        "delivery_keys",
        sa.Column("delivery_id", sa.String(length=128), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_delivery_keys_job_id_jobs"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("delivery_id", name=op.f("pk_delivery_keys")),
    )
    op.create_index("ix_delivery_keys_created_at", "delivery_keys", ["created_at"], unique=False)
    op.create_index(op.f("ix_delivery_keys_job_id"), "delivery_keys", ["job_id"], unique=False)
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_events_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
    )
    op.create_index("ix_events_created_at", "events", ["created_at"], unique=False)
    op.create_index(op.f("ix_events_job_id"), "events", ["job_id"], unique=False)
    op.create_table(
        "findings",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("agent_type", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("line_start", sa.Integer(), nullable=False),
        sa.Column("line_end", sa.Integer(), nullable=False),
        sa.Column("suggestion", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_findings_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_findings")),
    )
    op.create_index("ix_findings_created_at", "findings", ["created_at"], unique=False)
    op.create_index(op.f("ix_findings_job_id"), "findings", ["job_id"], unique=False)
    op.create_table(
        "stage_attempts",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_stage_attempts_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stage_attempts")),
        sa.UniqueConstraint(
            "job_id", "stage", "attempt_no", name=op.f("uq_stage_attempts_job_id_stage_attempt_no")
        ),
    )
    op.create_index("ix_stage_attempts_created_at", "stage_attempts", ["created_at"], unique=False)
    op.create_index(op.f("ix_stage_attempts_job_id"), "stage_attempts", ["job_id"], unique=False)
    op.create_table(
        "test_runs",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("framework", sa.String(length=64), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("passed", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("timed_out", sa.Boolean(), nullable=False),
        sa.Column(
            "summary", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_test_runs_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_runs")),
    )
    op.create_index("ix_test_runs_created_at", "test_runs", ["created_at"], unique=False)
    op.create_index(op.f("ix_test_runs_job_id"), "test_runs", ["job_id"], unique=False)
    op.create_table(
        "comments",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column(
            "finding_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True
        ),
        sa.Column("github_comment_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["findings.id"],
            name=op.f("fk_comments_finding_id_findings"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_comments_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_comments")),
        sa.UniqueConstraint("github_comment_id", name=op.f("uq_comments_github_comment_id")),
    )
    op.create_index("ix_comments_created_at", "comments", ["created_at"], unique=False)
    op.create_index(op.f("ix_comments_finding_id"), "comments", ["finding_id"], unique=False)
    op.create_index(op.f("ix_comments_job_id"), "comments", ["job_id"], unique=False)
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stage_attempt_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True
        ),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_llm_calls_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["stage_attempt_id"],
            ["stage_attempts.id"],
            name=op.f("fk_llm_calls_stage_attempt_id_stage_attempts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"], unique=False)
    op.create_index(op.f("ix_llm_calls_job_id"), "llm_calls", ["job_id"], unique=False)
    op.create_index(
        op.f("ix_llm_calls_stage_attempt_id"), "llm_calls", ["stage_attempt_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_llm_calls_stage_attempt_id"), table_name="llm_calls")
    op.drop_index(op.f("ix_llm_calls_job_id"), table_name="llm_calls")
    op.drop_index("ix_llm_calls_created_at", table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_index(op.f("ix_comments_job_id"), table_name="comments")
    op.drop_index(op.f("ix_comments_finding_id"), table_name="comments")
    op.drop_index("ix_comments_created_at", table_name="comments")
    op.drop_table("comments")
    op.drop_index(op.f("ix_test_runs_job_id"), table_name="test_runs")
    op.drop_index("ix_test_runs_created_at", table_name="test_runs")
    op.drop_table("test_runs")
    op.drop_index(op.f("ix_stage_attempts_job_id"), table_name="stage_attempts")
    op.drop_index("ix_stage_attempts_created_at", table_name="stage_attempts")
    op.drop_table("stage_attempts")
    op.drop_index(op.f("ix_findings_job_id"), table_name="findings")
    op.drop_index("ix_findings_created_at", table_name="findings")
    op.drop_table("findings")
    op.drop_index(op.f("ix_events_job_id"), table_name="events")
    op.drop_index("ix_events_created_at", table_name="events")
    op.drop_table("events")
    op.drop_index(op.f("ix_delivery_keys_job_id"), table_name="delivery_keys")
    op.drop_index("ix_delivery_keys_created_at", table_name="delivery_keys")
    op.drop_table("delivery_keys")
    op.drop_index("ix_jobs_status_lease_expires_at", table_name="jobs")
    op.drop_index(op.f("ix_jobs_status"), table_name="jobs")
    op.drop_index("ix_jobs_created_at", table_name="jobs")
    op.drop_table("jobs")
