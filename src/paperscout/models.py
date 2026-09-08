from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

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


class AnswerReviewEvidence(BaseModel):
    """One current section-evidence document supplied to Answer Review."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1)
    paper_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str = Field(min_length=1)


class ReadProjectFileArguments(BaseModel):
    """Canonical arguments for the QA agent's only read-only tool."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=500)
    offset_chars: int = Field(default=0, ge=0)
    max_chars: int = Field(default=20_000, ge=1, le=50_000)


class ProjectFileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    kind: Literal["directory", "text", "pdf", "image", "unsupported"]


class ReadProjectFileResult(BaseModel):
    """JSON-safe result returned to the QA Agent for every read attempt."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "error"]
    path: str
    kind: Literal["directory", "text", "pdf", "image"] | None = None
    content: str | None = None
    entries: list[ProjectFileEntry] = Field(default_factory=list)
    offset_chars: int = Field(default=0, ge=0)
    next_offset_chars: int | None = Field(default=None, ge=0)
    returned_chars: int = Field(default=0, ge=0)
    truncated: bool = False
    sha256: str | None = None
    media_type: str | None = None
    error_code: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_result_shape(self) -> "ReadProjectFileResult":
        if self.status == "success":
            if self.kind is None or self.sha256 is None or self.error_code is not None or self.error is not None:
                raise ValueError("successful reads require kind and sha256 without error fields")
            if self.kind in {"text", "directory"} and self.content is None:
                raise ValueError("text and directory reads require content")
            if self.kind in {"pdf", "image"} and self.content is not None:
                raise ValueError("binary resources must not expose content")
        elif self.error_code is None or self.error is None:
            raise ValueError("failed reads require error_code and error")
        return self


class ReadProjectFileOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: ReadProjectFileResult
    budget: "ReadBudget"
    record: "ReadResourceRecord | None" = None


class WikiSectionKind(str, Enum):
    RESEARCH_QUESTION = "research_question"
    CORE_IDEA = "core_idea"
    METHOD = "method"
    EXPERIMENT_OVERVIEW = "experiment_overview"
    CONCLUSION_AND_LIMITATIONS = "conclusion_and_limitations"


WIKI_SECTION_ORDER = (
    WikiSectionKind.RESEARCH_QUESTION,
    WikiSectionKind.CORE_IDEA,
    WikiSectionKind.METHOD,
    WikiSectionKind.EXPERIMENT_OVERVIEW,
    WikiSectionKind.CONCLUSION_AND_LIMITATIONS,
)


class WikiSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: WikiSectionKind
    content: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def reject_duplicate_evidence(self) -> "WikiSection":
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("Wiki section evidence IDs must be unique")
        return self


class WikiCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    sections: list[WikiSection] = Field(min_length=5, max_length=5)
    input_evidence_ids: list[str] = Field(min_length=1)
    source_sha256: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_wiki_evidence(self) -> "WikiCandidate":
        if tuple(section.kind for section in self.sections) != WIKI_SECTION_ORDER:
            raise ValueError("Wiki sections must use the canonical five-section order")
        allowed = set(self.input_evidence_ids)
        if len(allowed) != len(self.input_evidence_ids):
            raise ValueError("input_evidence_ids must be unique")
        for section in self.sections:
            unknown = set(section.evidence_ids) - allowed
            if unknown:
                raise ValueError(f"Wiki section cites evidence outside the Ingest input: {sorted(unknown)}")
            if any(not evidence_id.startswith(f"{self.paper_id}:s") for evidence_id in section.evidence_ids):
                raise ValueError("Wiki section evidence must belong to the current paper")
        return self


class SessionMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1)
    role: Literal["system", "user", "assistant", "tool"]
    content: str | dict[str, Any]
    tool_call_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ReadResourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    offset_chars: int = Field(ge=0)
    returned_chars: int = Field(ge=0)
    sha256: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class ReadBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_calls: int = Field(default=24, ge=1)
    max_chars_per_call: int = Field(default=50_000, ge=1)
    max_chars_total: int = Field(default=200_000, ge=1)
    calls_used: int = Field(default=0, ge=0)
    chars_used: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_usage(self) -> "ReadBudget":
        if self.calls_used > self.max_calls or self.chars_used > self.max_chars_total:
            raise ValueError("read budget usage exceeds its configured limit")
        return self


class UserProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_directions: list[str] = Field(default_factory=list)
    answer_style_preferences: list[str] = Field(default_factory=list)
    citation_preferences: list[str] = Field(default_factory=list)


class ProjectMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_goal: str | None = None
    paper_aliases: dict[str, str] = Field(default_factory=dict)
    confirmed_decisions: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    research_hypotheses: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ProjectState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    memory: ProjectMemory = Field(default_factory=ProjectMemory)
    updated_at: datetime = Field(default_factory=utc_now)


class QAClaimType(str, Enum):
    PAPER_FACT = "paper_fact"
    CROSS_PAPER_SYNTHESIS = "cross_paper_synthesis"
    HYPOTHESIS = "hypothesis"


class QAAnswerStatus(str, Enum):
    ANSWERED = "answered"
    PARTIALLY_ANSWERED = "partially_answered"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class QAClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    type: QAClaimType
    paper_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_evidence_ownership(self) -> "QAClaim":
        if len(self.paper_ids) != len(set(self.paper_ids)):
            raise ValueError("claim paper_ids must be unique")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("claim evidence_ids must be unique")
        if self.type == QAClaimType.PAPER_FACT and (not self.paper_ids or not self.evidence_ids):
            raise ValueError("paper facts require paper_ids and evidence_ids")
        if self.type == QAClaimType.CROSS_PAPER_SYNTHESIS and len(self.paper_ids) < 2:
            raise ValueError("cross-paper synthesis requires at least two papers")
        for evidence_id in self.evidence_ids:
            if not any(evidence_id.startswith(f"{paper_id}:s") for paper_id in self.paper_ids):
                raise ValueError(f"evidence {evidence_id} does not belong to a claim paper")
        if self.type in {QAClaimType.PAPER_FACT, QAClaimType.CROSS_PAPER_SYNTHESIS}:
            missing = [
                paper_id
                for paper_id in self.paper_ids
                if not any(evidence_id.startswith(f"{paper_id}:s") for evidence_id in self.evidence_ids)
            ]
            if missing:
                raise ValueError(f"claim papers lack evidence: {missing}")
        return self


class QAAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1)
    claims: list[QAClaim] = Field(default_factory=list)
    cited_evidence_ids: list[str] = Field(default_factory=list)
    status: QAAnswerStatus
    memory_patch: ProjectMemory = Field(default_factory=ProjectMemory)

    @model_validator(mode="after")
    def validate_citation_index(self) -> "QAAnswer":
        if len(self.cited_evidence_ids) != len(set(self.cited_evidence_ids)):
            raise ValueError("cited_evidence_ids must be unique")
        claimed = {evidence_id for claim in self.claims for evidence_id in claim.evidence_ids}
        if set(self.cited_evidence_ids) != claimed:
            raise ValueError("cited_evidence_ids must exactly index claim evidence")
        if self.status == QAAnswerStatus.INSUFFICIENT_EVIDENCE and self.claims:
            raise ValueError("insufficient_evidence answers must not contain factual claims")
        return self


class QAToolCallEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_call"]
    id: str = Field(min_length=1)
    name: Literal["read_project_file"]
    arguments: dict[str, Any]


class QAFinalEnvelope(QAAnswer):
    type: Literal["final"]


class SessionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    project_id: str = Field(default="default", min_length=1)
    messages: list[SessionMessage] = Field(default_factory=list)
    summary: str = ""
    compacted_turns: int = Field(default=0, ge=0)
    memory: ProjectMemory = Field(default_factory=ProjectMemory)
    read_resources: list[ReadResourceRecord] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class IngestGraphState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    paper_id: str = Field(min_length=1)
    llm_mode: Literal["mock", "real"]
    status: RunStatus = RunStatus.PENDING
    current_node: str | None = None
    event_sequence: int = Field(default=0, ge=0)
    paper: dict[str, Any] | None = None
    evidence: list[SectionEvidence] = Field(default_factory=list)
    citable_document: str | None = None
    raw_hashes: dict[str, str] = Field(default_factory=dict)
    input_coverage: EvidenceExtractionReport | None = None
    input_evidence_ids: list[str] = Field(default_factory=list)
    candidate_markdown: str | None = None
    candidate_sha256: str | None = None
    rule_errors: list[str] = Field(default_factory=list)
    review: ReviewDecision | None = None
    rule_review: ReviewDecision | None = None
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=2, ge=1)
    staging_path: str | None = None
    staging_hashes: dict[str, str] = Field(default_factory=dict)
    published: bool = False
    last_error: str | None = None
    result: dict[str, Any] | None = None


class AgentToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class QAGraphState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    project_id: str = Field(default="default", min_length=1)
    workspace: str = Field(min_length=1)
    question: str = Field(min_length=1)
    llm_mode: Literal["mock", "real"] = "mock"
    status: RunStatus = RunStatus.PENDING
    current_node: str | None = None
    event_sequence: int = Field(default=0, ge=0)
    messages: list[SessionMessage] = Field(default_factory=list)
    profile: UserProfile = Field(default_factory=UserProfile)
    history_summary: str = ""
    memory: ProjectMemory = Field(default_factory=ProjectMemory)
    session_read_resources: list[ReadResourceRecord] = Field(default_factory=list)
    invalidated_resources: list[str] = Field(default_factory=list)
    invalidated_evidence_ids: list[str] = Field(default_factory=list)
    session_message_count: int = Field(default=0, ge=0)
    context_compacted_turns: int = Field(default=0, ge=0)
    retained_history_turns: int = Field(default=0, ge=0)
    qa_context_window: int = Field(default=128_000, ge=1)
    context_threshold_tokens: int = Field(default=76_800, ge=1)
    estimated_input_tokens: int = Field(default=0, ge=0)
    read_resources: list[ReadResourceRecord] = Field(default_factory=list)
    read_budget: ReadBudget = Field(default_factory=ReadBudget)
    turn_start_message_index: int = Field(default=0, ge=0)
    current_tool_calls: list[AgentToolCall] = Field(default_factory=list)
    candidate_answer: QAAnswer | None = None
    rule_review: ReviewDecision | None = None
    answer_evidence_hashes: dict[str, str] = Field(default_factory=dict)
    review: ReviewDecision | None = None
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=2, ge=1)
    model_steps: int = Field(default=0, ge=0)
    max_model_steps: int = Field(default=32, ge=1)
    last_error: str | None = None
    result: dict[str, Any] | None = None


class AgentKind(str, Enum):
    HOST = "host"
    INGEST = "ingest"
    QA = "qa"
    WIKI_REVIEW = "wiki_review"
    ANSWER_REVIEW = "answer_review"


class EventKind(str, Enum):
    RUN_STARTED = "run.started"
    RUN_INTERRUPTED = "run.interrupted"
    RUN_RESUMED = "run.resumed"
    MODEL_STARTED = "model.started"
    MODEL_COMPLETED = "model.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    REVIEW_STARTED = "review.started"
    REVIEW_COMPLETED = "review.completed"
    CONTEXT_COMPACTED = "context.compacted"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"


class WorkflowEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    run_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    session_id: str | None = None
    agent: AgentKind
    event_type: EventKind
    timestamp: datetime = Field(default_factory=utc_now)
    node: str | None = None
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
