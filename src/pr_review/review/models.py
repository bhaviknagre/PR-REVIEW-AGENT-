"""Versioned LLM output schema (FR-15, NFR-11): Finding, FindingSet, FailedChunk."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from pr_review.core.repo_config import Category, Severity

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: Severity
    category: Category
    file_path: _Text = Field(max_length=512)
    line_start: int = Field(ge=1, le=10_000_000)
    line_end: int = Field(ge=1, le=10_000_000)
    summary: _Text = Field(max_length=300)
    rationale: _Text = Field(max_length=2000)
    suggestion: str | None = Field(default=None, max_length=4000)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    confidence_defaulted: bool = False  # set by the analyzer when the model omitted confidence

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        return self


class FailedCategory(BaseModel):
    """One category that never produced valid output; the chunk's other categories still count."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: Category
    reason: str = Field(max_length=500)
    attempts: int = Field(ge=1)


class FindingSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    prompt_version: str = "v1"
    chunk_id: str
    findings: tuple[Finding, ...] = ()
    failed_categories: tuple[FailedCategory, ...] = ()
    rejected: int = 0  # findings dropped for not anchoring to a changed line (counted, not silent)


class FailedChunk(BaseModel):
    """A chunk that could not be analysed; persisted so it is never silently dropped."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    chunk_id: str
    reason: str = Field(max_length=500)
    attempts: int = Field(ge=1)
