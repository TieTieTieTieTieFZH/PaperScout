from __future__ import annotations

import hashlib
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph

from .graph_runtime import GraphRuntime, RetryableNodeError, graph_config
from .health import check_wiki_rules, write_health_report_for_wiki
from .importer import import_preparsed
from .llm import MockLLM, OpenAICompatibleResponsesLLM
from .mineru import parse_with_mineru_api
from .evidence import extract_section_evidence
from .models import (
    AgentKind,
    EventKind,
    EvidenceExtractionReport,
    IngestGraphState,
    ReviewDecision,
    ReviewVerdict,
    RunStatus,
    WorkflowEvent,
)
from .prompts import (
    INGEST_SYSTEM_PROMPT,
    WIKI_REVIEW_SYSTEM_PROMPT,
    build_ingest_repair_prompt,
    build_ingest_review_revision_prompt,
    build_ingest_user_prompt,
    build_wiki_review_prompt,
)
from .review import parse_review_response
from .storage import FileSystemStore, directory_hashes, read_json, sha256_file, write_json, write_text_atomic
from .wiki import (
    render_citable_sections, render_paper_wiki, validate_wiki_markdown,
    write_canonical_indexes, write_section_evidence,
)


class IngestCoverageError(ValueError):
    def __init__(
        self,
        message: str,
        coverage: EvidenceExtractionReport,
        input_evidence_ids: list[str],
    ) -> None:
        super().__init__(message)
        self.coverage = coverage
        self.input_evidence_ids = input_evidence_ids


def _provider(mode: str):
    if mode == "mock":
        return MockLLM()
    if mode == "real":
        return OpenAICompatibleResponsesLLM()
    raise ValueError("llm_mode must be 'mock' or 'real'")


def _review_provider(mode: str):
    if mode == "mock":
        return MockLLM()
    if mode == "real":
        return OpenAICompatibleResponsesLLM()
    raise ValueError("llm_mode must be 'mock' or 'real'")


def _event(
    store: FileSystemStore,
    state: IngestGraphState,
    event_type: EventKind,
    node: str,
    message: str,
    data: dict[str, Any] | None = None,
    *,
    agent: AgentKind = AgentKind.HOST,
) -> None:
    events_path = store.run_dir(state.run_id) / "events.jsonl"
    persisted_events = len(events_path.read_text(encoding="utf-8").splitlines()) if events_path.exists() else 0
    sequence = max(state.event_sequence, persisted_events)
    store.append_event(
        WorkflowEvent(
            event_id=uuid.uuid4().hex,
            sequence=sequence,
            run_id=state.run_id,
            thread_id=state.thread_id,
            agent=agent,
            event_type=event_type,
            node=node,
            message=message,
            data=data or {},
        )
    )
    state.event_sequence = sequence + 1


def _new_run(store: FileSystemStore, mode: str, paper_id: str) -> IngestGraphState:
    run_id = uuid.uuid4().hex
    state = IngestGraphState(
        run_id=run_id,
        thread_id=f"ingest:{run_id}",
        workspace=str(store.workspace),
        paper_id=paper_id,
        llm_mode=mode,
        status=RunStatus.RUNNING,
    )
    _event(store, state, EventKind.RUN_STARTED, "ingest", "Ingest run started")
    return state


def _raw_hashes(raw_dir: Path) -> dict[str, str]:
    return {
        str(path.relative_to(raw_dir)): sha256_file(path)
        for path in sorted(raw_dir.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _existing_wiki_for_paper(store: FileSystemStore, paper_id: str) -> bool:
    if (store.wiki / "papers" / f"{paper_id}.md").exists() or (store.wiki / "evidence" / paper_id).exists():
        return True
    sources_path = store.wiki / "indexes" / "sources.json"
    return sources_path.exists() and any(item.get("paper_id") == paper_id for item in read_json(sources_path))


def prepare_ingest_context(store: FileSystemStore, paper_id: str, context_window: int) -> dict[str, Any]:
    """Create one citable, paper-local view from the authoritative content_list."""
    raw_candidate = store.paper_raw_dir(paper_id)
    if raw_candidate.is_symlink():
        raise ValueError("raw paper directory must not be a symbolic link")
    raw_dir = raw_candidate.resolve()
    try:
        raw_dir.relative_to(store.raw.resolve())
    except ValueError as exc:
        raise ValueError("raw paper directory escapes the workspace raw root") from exc
    metadata_path = raw_dir / "metadata.json"
    content_path = raw_dir / "mineru" / "content_list.json"
    if not metadata_path.is_file() or not content_path.is_file():
        raise FileNotFoundError("raw paper requires metadata.json and mineru/content_list.json")
    paper = read_json(metadata_path)
    if paper.get("paper_id") != paper_id:
        raise ValueError("metadata.json paper_id does not match the requested paper_id")
    bundle = extract_section_evidence(raw_dir, paper_id)
    eligible = [item for item in bundle.evidence if item.eligible_for_ingest]
    if not eligible:
        raise ValueError("No usable evidence could be extracted from raw MinerU output")
    input_budget = max(1, context_window // 2)
    selected = []
    for item in eligible:
        proposed = render_citable_sections([*selected, item])
        if len(proposed) > input_budget:
            break
        selected.append(item)
    truncated = len(selected) < len(eligible)
    coverage = bundle.report.model_copy(
        update={
            "truncated": truncated,
            "last_included_content_index": selected[-1].end_content_index if selected else None,
        }
    )
    if truncated:
        raise IngestCoverageError(
            "Ingest input exceeds the simple prefix budget; refusing to publish an incomplete five-section Wiki "
            f"({len(selected)}/{len(eligible)} eligible sections, budget={input_budget} chars)",
            coverage,
            [item.evidence_id for item in selected],
        )
    citable_document = render_citable_sections(selected)
    return {
        "raw_dir": raw_dir,
        "paper": paper,
        "evidence": selected,
        "coverage": coverage,
        "citable_document": citable_document,
        "raw_hashes": _raw_hashes(raw_dir),
    }


def ingest_agent(provider: Any, context: dict[str, Any]) -> str:
    prompt = build_ingest_user_prompt(paper=context["paper"], citable_document=context["citable_document"])
    return provider.generate_raw_text(
        [{"role": "system", "content": INGEST_SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
    )


def repair_ingest_summary(provider: Any, raw_output: str, error: str, context: dict[str, Any]) -> str:
    prompt = build_ingest_repair_prompt(
        raw_output=raw_output, validation_error=error, citable_document=context["citable_document"]
    )
    return provider.generate_raw_text(
        [{"role": "system", "content": INGEST_SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
    )


def revise_ingest_summary(
    provider: Any,
    raw_output: str,
    verdict: ReviewVerdict,
    feedback: list[str],
    context: dict[str, Any],
) -> str:
    prompt = build_ingest_review_revision_prompt(
        raw_output=raw_output,
        verdict=verdict.value,
        feedback=feedback,
        citable_document=context["citable_document"],
    )
    return provider.generate_raw_text(
        [{"role": "system", "content": INGEST_SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
    )


def render_wiki(store: FileSystemStore, state: IngestGraphState, context: dict[str, Any], candidate: Any) -> Path:
    """Publish only deterministic artifacts derived from a validated Markdown body."""
    store.run_dir(state.run_id).mkdir(parents=True, exist_ok=True)
    paper_markdown = render_paper_wiki(context["paper"], candidate)
    (store.run_dir(state.run_id) / "ingest-summary.md").write_text(paper_markdown, encoding="utf-8")
    staging = store.prepare_staging_wiki(state.run_id)
    paper_id = state.paper_id
    write_section_evidence(staging, context["evidence"])
    paper_path = staging / "papers" / f"{paper_id}.md"
    paper_path.parent.mkdir(parents=True, exist_ok=True)
    paper_path.write_text(paper_markdown, encoding="utf-8")
    write_canonical_indexes(staging, context["paper"], candidate)
    return staging


def _stored_context(state: IngestGraphState) -> dict[str, Any]:
    if state.paper is None or not state.evidence or state.citable_document is None or not state.raw_hashes:
        raise RuntimeError("Ingest graph state does not contain a prepared evidence context")
    store = FileSystemStore(Path(state.workspace))
    return {
        "raw_dir": store.paper_raw_dir(state.paper_id).resolve(),
        "paper": state.paper,
        "evidence": state.evidence,
        "coverage": state.input_coverage,
        "citable_document": state.citable_document,
        "raw_hashes": state.raw_hashes,
    }


def build_ingest_graph(
    runtime: GraphRuntime,
    *,
    provider: Any,
    context_window: int,
    review_provider: Any | None = None,
    interrupt_before: list[str] | None = None,
):
    """Compile the real Ingest StateGraph against the owned SQLite checkpointer."""
    review_provider = review_provider or MockLLM()

    def prepare_node(state: IngestGraphState) -> dict[str, Any]:
        state.current_node = "prepare_ingest_context"
        try:
            store = FileSystemStore(Path(state.workspace))
            context = prepare_ingest_context(store, state.paper_id, context_window)
            return {
                "current_node": state.current_node,
                "paper": context["paper"],
                "evidence": [item.model_dump(mode="json") for item in context["evidence"]],
                "citable_document": context["citable_document"],
                "raw_hashes": context["raw_hashes"],
                "input_coverage": context["coverage"].model_dump(mode="json"),
                "input_evidence_ids": [item.evidence_id for item in context["evidence"]],
                "last_error": None,
            }
        except IngestCoverageError as exc:
            return {
                "current_node": state.current_node,
                "input_coverage": exc.coverage.model_dump(mode="json"),
                "input_evidence_ids": exc.input_evidence_ids,
                "last_error": str(exc),
            }
        except Exception as exc:
            return {"current_node": state.current_node, "last_error": str(exc)}

    def generate_node(state: IngestGraphState) -> dict[str, Any]:
        state.current_node = "ingest_agent"
        store = FileSystemStore(Path(state.workspace))
        try:
            output_path = store.run_dir(state.run_id) / "ingest-output-1.md"
            _event(
                store,
                state,
                EventKind.MODEL_STARTED,
                state.current_node,
                "Requesting Wiki candidate",
                agent=AgentKind.INGEST,
            )
            replayed = output_path.exists()
            try:
                if replayed:
                    raw_output = output_path.read_text(encoding="utf-8")
                else:
                    raw_output = ingest_agent(provider, _stored_context(state))
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    write_text_atomic(output_path, raw_output)
            except Exception as exc:
                raise RetryableNodeError(f"Ingest model call failed: {exc}") from exc
            _event(
                store,
                state,
                EventKind.MODEL_COMPLETED,
                state.current_node,
                "Received Wiki candidate",
                {"attempt": 1, "replayed": replayed},
                agent=AgentKind.INGEST,
            )
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "attempt": 1,
                "candidate_markdown": raw_output,
                "candidate_sha256": hashlib.sha256(raw_output.encode("utf-8")).hexdigest(),
                "last_error": None,
            }
        except RetryableNodeError:
            raise
        except Exception as exc:
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "last_error": str(exc),
            }

    def validate_node(state: IngestGraphState) -> dict[str, Any]:
        state.current_node = "validate_wiki"
        try:
            if state.candidate_markdown is None:
                raise ValueError("Ingest candidate Markdown is missing")
            context = _stored_context(state)
            validate_wiki_markdown(
                state.candidate_markdown,
                paper=context["paper"],
                evidence=context["evidence"],
            )
            return {"current_node": state.current_node, "last_error": None}
        except ValueError as exc:
            errors = [*state.rule_errors, str(exc)]
            store = FileSystemStore(Path(state.workspace))
            write_json(
                store.run_dir(state.run_id) / f"validation-errors-{state.attempt}.json",
                {"error": str(exc)},
            )
            return {"current_node": state.current_node, "rule_errors": errors, "last_error": str(exc)}

    def repair_node(state: IngestGraphState) -> dict[str, Any]:
        state.current_node = "repair_ingest"
        store = FileSystemStore(Path(state.workspace))
        try:
            if state.candidate_markdown is None or state.last_error is None:
                raise RuntimeError("Cannot repair without a candidate and validation error")
            attempt = state.attempt + 1
            output_path = store.run_dir(state.run_id) / f"ingest-output-{attempt}.md"
            _event(
                store,
                state,
                EventKind.MODEL_STARTED,
                state.current_node,
                "Requesting repaired Wiki candidate",
                {"attempt": attempt},
                agent=AgentKind.INGEST,
            )
            replayed = output_path.exists()
            try:
                if replayed:
                    repaired = output_path.read_text(encoding="utf-8")
                else:
                    repaired = repair_ingest_summary(
                        provider,
                        state.candidate_markdown,
                        state.last_error,
                        _stored_context(state),
                    )
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    write_text_atomic(output_path, repaired)
            except Exception as exc:
                raise RetryableNodeError(f"Ingest repair model call failed: {exc}") from exc
            _event(
                store,
                state,
                EventKind.MODEL_COMPLETED,
                state.current_node,
                "Received repaired Wiki candidate",
                {"attempt": attempt, "replayed": replayed},
                agent=AgentKind.INGEST,
            )
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "attempt": attempt,
                "candidate_markdown": repaired,
                "candidate_sha256": hashlib.sha256(repaired.encode("utf-8")).hexdigest(),
                "last_error": None,
            }
        except RetryableNodeError:
            raise
        except Exception as exc:
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "last_error": str(exc),
            }

    def wiki_review_node(state: IngestGraphState) -> dict[str, Any]:
        state.current_node = "wiki_review"
        store = FileSystemStore(Path(state.workspace))
        audit = store.run_dir(state.run_id) / "review" / "wiki" / str(state.attempt)
        try:
            context = _stored_context(state)
            if state.candidate_markdown is None:
                raise ValueError("Ingest candidate Markdown is missing")
            candidate = validate_wiki_markdown(
                state.candidate_markdown,
                paper=context["paper"],
                evidence=context["evidence"],
            )
            cited_ids = {
                evidence_id
                for section in candidate.sections
                for evidence_id in section.evidence_ids
            }
            cited_evidence = [item for item in context["evidence"] if item.evidence_id in cited_ids]
            request = build_wiki_review_prompt(
                paper=context["paper"],
                candidate_markdown=state.candidate_markdown,
                evidence_document=render_citable_sections(cited_evidence),
            )
            request_path = audit / "request.md"
            response_path = audit / "response.md"
            result_path = audit / "result.json"
            if audit.exists():
                if not audit.is_dir() or audit.is_symlink():
                    raise ValueError("Wiki review audit path must be a normal directory")
                if not request_path.is_file() or request_path.read_text(encoding="utf-8") != request:
                    raise RuntimeError("Wiki review replay request does not match its durable audit")
            else:
                audit.mkdir(parents=True)
                write_text_atomic(request_path, request)
            _event(
                store,
                state,
                EventKind.REVIEW_STARTED,
                state.current_node,
                "Requesting semantic Wiki review",
                {"attempt": state.attempt, "evidence_ids": sorted(cited_ids)},
                agent=AgentKind.WIKI_REVIEW,
            )
            replayed = False
            if result_path.is_file():
                decision = ReviewDecision.model_validate(read_json(result_path))
                replayed = True
            else:
                if response_path.is_file():
                    response = response_path.read_text(encoding="utf-8")
                    replayed = True
                else:
                    try:
                        response = review_provider.generate_raw_text(
                            [
                                {"role": "system", "content": WIKI_REVIEW_SYSTEM_PROMPT},
                                {"role": "user", "content": request},
                            ]
                        )
                        write_text_atomic(response_path, response)
                    except Exception as exc:
                        raise RetryableNodeError(f"Wiki review model call failed: {exc}") from exc
                decision = parse_review_response(response)
                write_json(result_path, decision.model_dump(mode="json"))
            _event(
                store,
                state,
                EventKind.REVIEW_COMPLETED,
                state.current_node,
                "Completed semantic Wiki review",
                {"attempt": state.attempt, "verdict": decision.verdict.value, "replayed": replayed},
                agent=AgentKind.WIKI_REVIEW,
            )
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "review": decision.model_dump(mode="json"),
                "last_error": None,
            }
        except RetryableNodeError:
            raise
        except Exception as exc:
            if audit.is_dir() and not (audit / "result.json").exists():
                write_json(audit / "result.json", {"status": "failed", "error": str(exc)})
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "last_error": str(exc),
            }

    def revise_node(state: IngestGraphState) -> dict[str, Any]:
        state.current_node = "revise_ingest"
        store = FileSystemStore(Path(state.workspace))
        try:
            if state.candidate_markdown is None or state.review is None:
                raise RuntimeError("Cannot revise without a candidate and semantic review")
            attempt = state.attempt + 1
            output_path = store.run_dir(state.run_id) / f"ingest-output-{attempt}.md"
            _event(
                store,
                state,
                EventKind.MODEL_STARTED,
                state.current_node,
                "Regenerating Wiki candidate from Review feedback",
                {"attempt": attempt, "verdict": state.review.verdict.value},
                agent=AgentKind.INGEST,
            )
            replayed = output_path.exists()
            try:
                if replayed:
                    revised = output_path.read_text(encoding="utf-8")
                else:
                    revised = revise_ingest_summary(
                        provider,
                        state.candidate_markdown,
                        state.review.verdict,
                        state.review.feedback,
                        _stored_context(state),
                    )
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    write_text_atomic(output_path, revised)
            except Exception as exc:
                raise RetryableNodeError(f"Ingest revision model call failed: {exc}") from exc
            _event(
                store,
                state,
                EventKind.MODEL_COMPLETED,
                state.current_node,
                "Received regenerated Wiki candidate",
                {"attempt": attempt, "replayed": replayed},
                agent=AgentKind.INGEST,
            )
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "attempt": attempt,
                "candidate_markdown": revised,
                "candidate_sha256": hashlib.sha256(revised.encode("utf-8")).hexdigest(),
                "review": None,
                "last_error": None,
            }
        except RetryableNodeError:
            raise
        except Exception as exc:
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "last_error": str(exc),
            }

    def verify_raw_node(state: IngestGraphState) -> dict[str, Any]:
        state.current_node = "verify_raw"
        try:
            context = _stored_context(state)
            if _raw_hashes(context["raw_dir"]) != context["raw_hashes"]:
                raise RuntimeError("Raw input changed during ingest; refusing to publish")
            return {"current_node": state.current_node, "last_error": None}
        except Exception as exc:
            return {"current_node": state.current_node, "last_error": str(exc)}

    def render_node(state: IngestGraphState) -> dict[str, Any]:
        state.current_node = "render_wiki"
        try:
            store = FileSystemStore(Path(state.workspace))
            store.discard_staging_wiki(state.run_id)
            context = _stored_context(state)
            if state.candidate_markdown is None:
                raise ValueError("Ingest candidate Markdown is missing")
            candidate = validate_wiki_markdown(
                state.candidate_markdown,
                paper=context["paper"],
                evidence=context["evidence"],
            )
            staging = render_wiki(store, state, context, candidate)
            write_health_report_for_wiki(staging)
            return {"current_node": state.current_node, "staging_path": str(staging), "last_error": None}
        except Exception as exc:
            return {"current_node": state.current_node, "last_error": str(exc)}

    def review_node(state: IngestGraphState) -> dict[str, Any]:
        state.current_node = "wiki_rules"
        store = FileSystemStore(Path(state.workspace))
        try:
            if state.staging_path is None:
                raise RuntimeError("Staging path is missing")
            _event(store, state, EventKind.REVIEW_STARTED, state.current_node, "Checking staged Wiki rules")
            reviewed = check_wiki_rules(Path(state.staging_path))
            _event(
                store,
                state,
                EventKind.REVIEW_COMPLETED,
                state.current_node,
                "Completed staged Wiki rule check",
                {"verdict": reviewed.verdict.value},
            )
            error = None if reviewed.verdict == ReviewVerdict.APPROVE else "Staged Wiki failed rule checks"
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "rule_review": reviewed.model_dump(mode="json"),
                "last_error": error,
            }
        except Exception as exc:
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "last_error": str(exc),
            }

    def publish_node(state: IngestGraphState) -> dict[str, Any]:
        state.current_node = "publish_wiki"
        store = FileSystemStore(Path(state.workspace))
        try:
            if not state.staging_hashes:
                raise RuntimeError("Reviewed staging manifest is missing before publish")
            store.publish_staged_wiki(state.run_id, expected_hashes=state.staging_hashes)
            state.published = True
            state.status = RunStatus.COMPLETED
            result = {
                "status": "published",
                "paper_id": state.paper_id,
                "review": state.review.model_dump(mode="json") if state.review else None,
                "rule_review": state.rule_review.model_dump(mode="json") if state.rule_review else None,
                "run_id": state.run_id,
            }
            _event(store, state, EventKind.RUN_COMPLETED, state.current_node, "Published reviewed Wiki")
            store.write_result(state.run_id, result)
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "published": True,
                "status": RunStatus.COMPLETED.value,
                "last_error": None,
                "result": result,
            }
        except Exception as exc:
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "last_error": str(exc),
            }

    def fail_node(state: IngestGraphState) -> dict[str, Any]:
        failed_node = state.current_node or "ingest"
        state.current_node = "fail_run"
        state.status = RunStatus.FAILED
        store = FileSystemStore(Path(state.workspace))
        message = state.last_error or "Ingest failed"
        result: dict[str, Any] = {
            "status": "failed",
            "paper_id": state.paper_id,
            "error": message,
            "run_id": state.run_id,
        }
        if state.review is not None:
            result["review"] = state.review.model_dump(mode="json")
        if state.rule_review is not None:
            result["rule_review"] = state.rule_review.model_dump(mode="json")
        if state.input_coverage is not None:
            result["coverage"] = state.input_coverage.model_dump(mode="json")
        _event(store, state, EventKind.RUN_FAILED, failed_node, message)
        store.write_result(state.run_id, result)
        return {
            "current_node": state.current_node,
            "event_sequence": state.event_sequence,
            "status": RunStatus.FAILED.value,
            "published": False,
            "last_error": message,
            "result": result,
        }

    def route_error(state: IngestGraphState) -> str:
        return "fail" if state.last_error else "continue"

    def route_validation(state: IngestGraphState) -> str:
        if state.last_error is None:
            return "valid"
        return "repair" if state.attempt < state.max_attempts else "fail"

    def route_semantic_review(state: IngestGraphState) -> str:
        if state.last_error is not None or state.review is None:
            return "fail"
        if state.review.verdict == ReviewVerdict.APPROVE:
            return "approve"
        return "revise" if state.attempt < state.max_attempts else "fail"

    def route_rule_review(state: IngestGraphState) -> str:
        return "approve" if state.last_error is None and state.rule_review is not None else "fail"

    def verify_publish_node(state: IngestGraphState) -> dict[str, Any]:
        state.current_node = "verify_publish"
        try:
            context = _stored_context(state)
            if _raw_hashes(context["raw_dir"]) != context["raw_hashes"]:
                raise RuntimeError("Raw input changed during ingest; refusing to publish")
            if state.candidate_markdown is None or state.candidate_sha256 is None:
                raise RuntimeError("Validated Ingest candidate is missing before publish")
            current_candidate_hash = hashlib.sha256(state.candidate_markdown.encode("utf-8")).hexdigest()
            if current_candidate_hash != state.candidate_sha256:
                raise RuntimeError("Ingest candidate changed after validation; refusing to publish")
            if state.staging_path is None or not Path(state.staging_path).is_dir():
                raise RuntimeError("Reviewed staging Wiki is missing before publish")
            staging_hashes = directory_hashes(Path(state.staging_path))
            if not staging_hashes:
                raise RuntimeError("Reviewed staging Wiki is empty before publish")
            return {
                "current_node": state.current_node,
                "staging_hashes": staging_hashes,
                "last_error": None,
            }
        except Exception as exc:
            return {"current_node": state.current_node, "last_error": str(exc)}

    builder = StateGraph(IngestGraphState)
    builder.add_node("prepare_ingest_context", prepare_node)
    builder.add_node("ingest_agent", generate_node)
    builder.add_node("validate_wiki", validate_node)
    builder.add_node("repair_ingest", repair_node)
    builder.add_node("verify_raw", verify_raw_node)
    builder.add_node("wiki_review", wiki_review_node)
    builder.add_node("revise_ingest", revise_node)
    builder.add_node("render_wiki", render_node)
    builder.add_node("wiki_rules", review_node)
    builder.add_node("verify_publish", verify_publish_node)
    builder.add_node("publish_wiki", publish_node)
    builder.add_node("fail_run", fail_node)
    builder.add_edge(START, "prepare_ingest_context")
    builder.add_conditional_edges(
        "prepare_ingest_context",
        route_error,
        {"continue": "ingest_agent", "fail": "fail_run"},
    )
    builder.add_conditional_edges("ingest_agent", route_error, {"continue": "validate_wiki", "fail": "fail_run"})
    builder.add_conditional_edges(
        "validate_wiki",
        route_validation,
        {"valid": "verify_raw", "repair": "repair_ingest", "fail": "fail_run"},
    )
    builder.add_conditional_edges(
        "repair_ingest",
        route_error,
        {"continue": "validate_wiki", "fail": "fail_run"},
    )
    builder.add_conditional_edges("verify_raw", route_error, {"continue": "wiki_review", "fail": "fail_run"})
    builder.add_conditional_edges(
        "wiki_review",
        route_semantic_review,
        {"approve": "render_wiki", "revise": "revise_ingest", "fail": "fail_run"},
    )
    builder.add_conditional_edges("revise_ingest", route_error, {"continue": "validate_wiki", "fail": "fail_run"})
    builder.add_conditional_edges("render_wiki", route_error, {"continue": "wiki_rules", "fail": "fail_run"})
    builder.add_conditional_edges(
        "wiki_rules",
        route_rule_review,
        {"approve": "verify_publish", "fail": "fail_run"},
    )
    builder.add_conditional_edges("verify_publish", route_error, {"continue": "publish_wiki", "fail": "fail_run"})
    builder.add_conditional_edges("publish_wiki", route_error, {"continue": END, "fail": "fail_run"})
    builder.add_edge("fail_run", END)
    return runtime.compile(builder, interrupt_before=interrupt_before)


def _interrupted_ingest_outcome(
    store: FileSystemStore,
    snapshot: Any,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    if not snapshot.next:
        raise RuntimeError("Ingest failure did not leave a resumable checkpoint")
    paused = IngestGraphState.model_validate(snapshot.values)
    next_nodes = list(snapshot.next)
    _event(
        store,
        paused,
        EventKind.RUN_INTERRUPTED,
        paused.current_node or "ingest",
        "Ingest run interrupted at a checkpoint boundary",
        {"next_nodes": next_nodes, "error": error},
    )
    result = {
        "status": "interrupted",
        "paper_id": paused.paper_id,
        "run_id": paused.run_id,
        "thread_id": paused.thread_id,
        "next_nodes": next_nodes,
        "retryable": True,
    }
    if error is not None:
        result["error"] = error
    store.write_result(paused.run_id, result)
    return result


def _ingest_graph_outcome(
    store: FileSystemStore,
    graph: Any,
    config: dict[str, dict[str, str]],
    output: dict[str, Any],
) -> dict[str, Any]:
    snapshot = graph.get_state(config)
    if snapshot.next:
        return _interrupted_ingest_outcome(store, snapshot)
    result = output.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Ingest graph completed without a result")
    return result


def _run_ingest_from_raw_store(
    store: FileSystemStore,
    paper_id: str,
    llm_mode: str,
    interrupt_before: list[str] | None = None,
) -> dict[str, Any]:
    if _existing_wiki_for_paper(store, paper_id):
        raise ValueError(f"Wiki already contains paper_id {paper_id}; refusing to overwrite published artifacts")
    provider = _provider(llm_mode)
    review_provider = _review_provider(llm_mode)
    window = getattr(getattr(provider, "settings", None), "ingest_context_window", 128_000)
    state = _new_run(store, llm_mode, paper_id)
    try:
        with GraphRuntime.open(store.workspace) as runtime:
            graph = build_ingest_graph(
                runtime,
                provider=provider,
                review_provider=review_provider,
                context_window=window,
                interrupt_before=interrupt_before,
            )
            config = graph_config(state.thread_id)
            try:
                final_state = graph.invoke(state.model_dump(mode="json"), config)
            except RetryableNodeError as exc:
                return _interrupted_ingest_outcome(store, graph.get_state(config), error=str(exc))
            return _ingest_graph_outcome(store, graph, config, final_state)
    except Exception as exc:
        state.status = RunStatus.FAILED
        state.last_error = str(exc)
        events_path = store.run_dir(state.run_id) / "events.jsonl"
        if events_path.exists():
            state.event_sequence = len(events_path.read_text(encoding="utf-8").splitlines())
        _event(store, state, EventKind.RUN_FAILED, state.current_node or "ingest", str(exc))
        result = {"status": "failed", "paper_id": paper_id, "error": str(exc), "run_id": state.run_id}
        store.write_result(state.run_id, result)
        return result


def run_ingest_from_raw(
    workspace: Path,
    paper_id: str,
    llm_mode: str = "mock",
    *,
    interrupt_before: list[str] | None = None,
) -> dict[str, Any]:
    return _run_ingest_from_raw_store(FileSystemStore(workspace), paper_id, llm_mode, interrupt_before)


def resume_ingest(workspace: Path, thread_id: str, llm_mode: str = "mock") -> dict[str, Any]:
    store = FileSystemStore(workspace)
    config = graph_config(thread_id)
    with GraphRuntime.open(store.workspace) as runtime:
        inspection_graph = build_ingest_graph(
            runtime,
            provider=MockLLM(),
            review_provider=MockLLM(),
            context_window=128_000,
        )
        snapshot = inspection_graph.get_state(config)
        if not snapshot.values:
            raise ValueError(f"No Ingest checkpoint exists for thread_id {thread_id}")
        state = IngestGraphState.model_validate(snapshot.values)
        if state.thread_id != thread_id or Path(state.workspace).resolve() != store.workspace.resolve():
            raise ValueError("Ingest checkpoint does not belong to this workspace or thread")
        if not snapshot.next:
            if isinstance(state.result, dict):
                return state.result
            raise RuntimeError("Ingest checkpoint is terminal without a result")
        if state.llm_mode != llm_mode:
            raise ValueError(f"Ingest checkpoint requires llm_mode={state.llm_mode}")
        provider = _provider(llm_mode)
        review_provider = _review_provider(llm_mode)
        window = getattr(getattr(provider, "settings", None), "ingest_context_window", 128_000)
        graph = build_ingest_graph(
            runtime,
            provider=provider,
            review_provider=review_provider,
            context_window=window,
        )
        _event(
            store,
            state,
            EventKind.RUN_RESUMED,
            state.current_node or "ingest",
            "Resuming Ingest from checkpoint",
            {"next_nodes": list(snapshot.next)},
        )
        try:
            final_state = graph.invoke(None, config)
        except RetryableNodeError as exc:
            return _interrupted_ingest_outcome(store, graph.get_state(config), error=str(exc))
        return _ingest_graph_outcome(store, graph, config, final_state)


def run_ingest(
    workspace: Path, mineru_path: Path | None = None, source_pdf: Path | None = None, paper_id: str | None = None,
    title: str | None = None, authors: list[str] | None = None, year: int | None = None, llm_mode: str = "mock", *, mineru_token: str | None = None,
    interrupt_before: list[str] | None = None,
) -> dict[str, Any]:
    """Import raw MinerU output if needed, then run the same raw-to-Wiki pipeline."""
    store = FileSystemStore(workspace)
    temporary_mineru_path: Path | None = None
    if mineru_path is None:
        if source_pdf is None:
            raise ValueError("source_pdf is required when mineru_path is not provided")
        temporary_mineru_path = Path(tempfile.mkdtemp(prefix="paperscout-mineru-"))
        try:
            parsed = parse_with_mineru_api(source_pdf, temporary_mineru_path, token=mineru_token)
            imported = import_preparsed(workspace, parsed.output_dir, source_pdf, paper_id, title, authors, year, task_metadata=parsed.task_metadata)
        finally:
            shutil.rmtree(temporary_mineru_path, ignore_errors=True)
    else:
        imported = import_preparsed(workspace, mineru_path, source_pdf, paper_id, title, authors, year)
    return _run_ingest_from_raw_store(store, imported.paper_id, llm_mode, interrupt_before)
