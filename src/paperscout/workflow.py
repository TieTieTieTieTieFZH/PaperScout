from __future__ import annotations

import hashlib
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph

from .graph_runtime import GraphRuntime, graph_config
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
    ReviewVerdict,
    RunStatus,
    WorkflowEvent,
)
from .prompts import INGEST_SYSTEM_PROMPT, build_ingest_repair_prompt, build_ingest_user_prompt
from .storage import FileSystemStore, read_json, sha256_file, write_json
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
    store.append_event(
        WorkflowEvent(
            event_id=uuid.uuid4().hex,
            sequence=state.event_sequence,
            run_id=state.run_id,
            thread_id=state.thread_id,
            agent=agent,
            event_type=event_type,
            node=node,
            message=message,
            data=data or {},
        )
    )
    state.event_sequence += 1


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


def build_ingest_graph(runtime: GraphRuntime, *, provider: Any, context_window: int):
    """Compile the real Ingest StateGraph against the owned SQLite checkpointer."""

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
            _event(
                store,
                state,
                EventKind.MODEL_STARTED,
                state.current_node,
                "Requesting Wiki candidate",
                agent=AgentKind.INGEST,
            )
            raw_output = ingest_agent(provider, _stored_context(state))
            _event(
                store,
                state,
                EventKind.MODEL_COMPLETED,
                state.current_node,
                "Received Wiki candidate",
                {"attempt": 1},
                agent=AgentKind.INGEST,
            )
            (store.run_dir(state.run_id) / "ingest-output-1.md").write_text(raw_output, encoding="utf-8")
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "attempt": 1,
                "candidate_markdown": raw_output,
                "candidate_sha256": hashlib.sha256(raw_output.encode("utf-8")).hexdigest(),
                "last_error": None,
            }
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
            _event(
                store,
                state,
                EventKind.MODEL_STARTED,
                state.current_node,
                "Requesting repaired Wiki candidate",
                {"attempt": state.attempt + 1},
                agent=AgentKind.INGEST,
            )
            repaired = repair_ingest_summary(provider, state.candidate_markdown, state.last_error, _stored_context(state))
            attempt = state.attempt + 1
            _event(
                store,
                state,
                EventKind.MODEL_COMPLETED,
                state.current_node,
                "Received repaired Wiki candidate",
                {"attempt": attempt},
                agent=AgentKind.INGEST,
            )
            (store.run_dir(state.run_id) / f"ingest-output-{attempt}.md").write_text(repaired, encoding="utf-8")
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "attempt": attempt,
                "candidate_markdown": repaired,
                "candidate_sha256": hashlib.sha256(repaired.encode("utf-8")).hexdigest(),
                "last_error": None,
            }
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
                "review": reviewed.model_dump(mode="json"),
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
            store.publish_staged_wiki(state.run_id)
            state.published = True
            state.status = RunStatus.COMPLETED
            result = {
                "status": "published",
                "paper_id": state.paper_id,
                "review": state.review.model_dump(mode="json") if state.review else None,
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

    def route_review(state: IngestGraphState) -> str:
        return "approve" if state.last_error is None and state.review is not None else "fail"

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
            return {"current_node": state.current_node, "last_error": None}
        except Exception as exc:
            return {"current_node": state.current_node, "last_error": str(exc)}

    builder = StateGraph(IngestGraphState)
    builder.add_node("prepare_ingest_context", prepare_node)
    builder.add_node("ingest_agent", generate_node)
    builder.add_node("validate_wiki", validate_node)
    builder.add_node("repair_ingest", repair_node)
    builder.add_node("verify_raw", verify_raw_node)
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
    builder.add_conditional_edges("verify_raw", route_error, {"continue": "render_wiki", "fail": "fail_run"})
    builder.add_conditional_edges("render_wiki", route_error, {"continue": "wiki_rules", "fail": "fail_run"})
    builder.add_conditional_edges(
        "wiki_rules",
        route_review,
        {"approve": "verify_publish", "fail": "fail_run"},
    )
    builder.add_conditional_edges("verify_publish", route_error, {"continue": "publish_wiki", "fail": "fail_run"})
    builder.add_conditional_edges("publish_wiki", route_error, {"continue": END, "fail": "fail_run"})
    builder.add_edge("fail_run", END)
    return runtime.compile(builder)


def _run_ingest_from_raw_store(store: FileSystemStore, paper_id: str, llm_mode: str) -> dict[str, Any]:
    if _existing_wiki_for_paper(store, paper_id):
        raise ValueError(f"Wiki already contains paper_id {paper_id}; refusing to overwrite published artifacts")
    provider = _provider(llm_mode)
    window = getattr(getattr(provider, "settings", None), "ingest_context_window", 128_000)
    state = _new_run(store, llm_mode, paper_id)
    try:
        with GraphRuntime.open(store.workspace) as runtime:
            graph = build_ingest_graph(runtime, provider=provider, context_window=window)
            final_state = graph.invoke(state.model_dump(mode="json"), graph_config(state.thread_id))
        result = final_state.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Ingest graph completed without a result")
        return result
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


def run_ingest_from_raw(workspace: Path, paper_id: str, llm_mode: str = "mock") -> dict[str, Any]:
    return _run_ingest_from_raw_store(FileSystemStore(workspace), paper_id, llm_mode)


def run_ingest(
    workspace: Path, mineru_path: Path | None = None, source_pdf: Path | None = None, paper_id: str | None = None,
    title: str | None = None, authors: list[str] | None = None, year: int | None = None, llm_mode: str = "mock", *, mineru_token: str | None = None,
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
    return _run_ingest_from_raw_store(store, imported.paper_id, llm_mode)
