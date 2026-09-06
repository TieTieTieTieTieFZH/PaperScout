from __future__ import annotations

from datetime import datetime, timezone
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
