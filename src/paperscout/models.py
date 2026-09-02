from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PaperMetadata(BaseModel):
    paper_id: str
    title: str = "Unknown title"
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    source_url: str | None = None
    source_pdf: str
    source_sha256: str
    imported_at: datetime = Field(default_factory=utc_now)


class Evidence(BaseModel):
    evidence_id: str
    paper_id: str
    page: int
    page_idx: int
    content_index: int
    block_id: str | None = None
    section: str | None = None
    content_type: str
    quote: str
    bbox: list[float] | None = None
    raw_file: str
    mineru_file: str


class Citation(BaseModel):
    evidence_id: str
    page: int
    quote: str


class Claim(BaseModel):
    claim: str
    citations: list[Citation] = Field(default_factory=list)


class QAResult(BaseModel):
    answer: str
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)


class ReviewResult(BaseModel):
    status: Literal["supported", "partially_supported", "unsupported"]
    feedback: list[str] = Field(default_factory=list)
    checked_evidence: int = 0


class Artifact(BaseModel):
    artifact_type: str
    path: str
    sha256: str
    created_at: datetime = Field(default_factory=utc_now)


class StepResult(BaseModel):
    node: str
    status: Literal["completed", "failed"]
    message: str = ""
    artifacts: list[Artifact] = Field(default_factory=list)


class AgentTask(BaseModel):
    task_id: str
    task_type: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)


class RunContext(BaseModel):
    run_id: str
    workspace: str
    current_node: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)


class RunEvent(BaseModel):
    timestamp: datetime = Field(default_factory=utc_now)
    event_type: str
    node: str | None = None
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class Hook(Protocol):
    def __call__(self, event: RunEvent) -> None: ...
