from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class EvidenceBlock(BaseModel):
    """One source block contained by a section-level evidence record."""

    model_config = ConfigDict(extra="forbid")

    content_index: int = Field(ge=0)
    page: int = Field(ge=1)
    page_idx: int = Field(ge=0)
    content_type: str = Field(min_length=1)
    text: str = Field(min_length=1)
    text_level: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None
    block_id: str | None = None


class SectionEvidence(BaseModel):
    """Canonical, source-addressable evidence grouped by a MinerU level-2 section."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1)
    paper_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    start_content_index: int = Field(ge=0)
    end_content_index: int = Field(ge=0)
    content_indices: list[int] = Field(min_length=1)
    pages: list[int] = Field(min_length=1)
    blocks: list[EvidenceBlock] = Field(min_length=1)
    text: str = Field(min_length=1)
    raw_file: str = Field(min_length=1)
    mineru_file: str = Field(min_length=1)
    source_sha256: str | None = None
    parser_name: str = "mineru"
    parser_version: str | None = None
    eligible_for_ingest: bool = True

    @model_validator(mode="after")
    def validate_source_coordinates(self) -> "SectionEvidence":
        expected_id = f"{self.paper_id}:s{self.start_content_index:04d}"
        if self.evidence_id != expected_id:
            raise ValueError(f"evidence_id must be {expected_id}")
        indices = [block.content_index for block in self.blocks]
        if indices != sorted(set(indices)):
            raise ValueError("evidence blocks must use unique ascending content indices")
        if self.content_indices != indices:
            raise ValueError("content_indices must exactly match the contained blocks")
        if self.start_content_index != indices[0] or self.end_content_index != indices[-1]:
            raise ValueError("section boundaries must match the first and last contained blocks")
        pages = sorted({block.page for block in self.blocks})
        if self.pages != pages:
            raise ValueError("pages must be the sorted unique pages from the contained blocks")
        return self


class EvidenceExtractionReport(BaseModel):
    """Deterministic extraction coverage and parser-quality diagnostics."""

    model_config = ConfigDict(extra="forbid")

    total_blocks: int = Field(ge=0)
    section_count: int = Field(ge=0)
    eligible_section_count: int = Field(ge=0)
    skipped_before_first_section: int = Field(ge=0)
    skipped_noise_blocks: int = Field(ge=0)
    skipped_empty_blocks: int = Field(ge=0)
    truncated: bool = False
    last_included_content_index: int | None = Field(default=None, ge=0)


class SectionEvidenceBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence: list[SectionEvidence]
    report: EvidenceExtractionReport


class ReviewVerdict(str, Enum):
    APPROVE = "APPROVE"
    REVISE = "REVISE"
    REJECT = "REJECT"


class ReviewDecision(BaseModel):
    """Canonical fail-closed output shared by Wiki and Answer review clients."""

    model_config = ConfigDict(extra="forbid")

    verdict: ReviewVerdict
    feedback: list[str] = Field(default_factory=list)


class ReadProjectFileArguments(BaseModel):
    """Canonical arguments for the QA agent's only read-only tool."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=500)
    offset_chars: int = Field(default=0, ge=0)
    max_chars: int = Field(default=20_000, ge=1, le=50_000)


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


class ReadRawToolArguments(BaseModel):
    """Bounded arguments for the read-only raw tool."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    offset_chars: int = Field(default=0, ge=0)
    max_chars: int = Field(default=0, ge=0)


class AgentToolCall(BaseModel):
    """A tool request emitted only inside an assistant message."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class AssistantAgentMessage(BaseModel):
    """One agent turn: either tool calls or the final draft envelope."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["assistant"]
    content: dict[str, Any] | None = None
    tool_calls: list[AgentToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_one_response_mode(self) -> "AssistantAgentMessage":
        if bool(self.content) == bool(self.tool_calls):
            raise ValueError("assistant 消息必须恰好包含 content 或 tool_calls")
        return self


class ToolAgentMessage(BaseModel):
    """A host-created tool result bound to a prior assistant tool call."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["tool"]
    tool_call_id: str = Field(min_length=1)
    content: dict[str, Any]


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
