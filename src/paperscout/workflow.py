from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .health import write_health_report
from .importer import import_preparsed
from .llm import MockLLM, OpenAICompatibleResponsesLLM
from .mineru import parse_with_mineru_api
from .models import QAResult, ReviewResult, RunEvent
from .storage import FileSystemStore, read_json, write_json
from .wiki import extract_evidence, render_concept, render_summary, retrieve, validate_qa, write_evidence, write_indexes, read_evidence


class PipelineState(TypedDict, total=False):
    run_id: str
    workspace: str
    paper_id: str
    question: str
    mode: str
    current_node: str
    ingest_attempts: int
    qa_attempts: int
    review_target: str
    feedback: list[str]
    review: dict[str, Any]
    qa_result: dict[str, Any]
    result: dict[str, Any]


def _provider(mode: str):
    if mode == "mock":
        return MockLLM()
    if mode == "real":
        return OpenAICompatibleResponsesLLM()
    raise ValueError("llm_mode must be 'mock' or 'real'")


def _event(store: FileSystemStore, state: PipelineState, event_type: str, node: str, message: str, data: dict[str, Any] | None = None) -> None:
    store.append_event(state["run_id"], RunEvent(event_type=event_type, node=node, message=message, data=data or {}))
    store.checkpoint(state["run_id"], dict(state))


def _build_ingest_graph(store: FileSystemStore, provider: Any):
    def wiki_ingest(state: PipelineState) -> dict[str, Any]:
        node = "wiki_ingest"
        state["current_node"] = node
        _event(store, state, "node_started", node, "Compiling MinerU output into staged Wiki artifacts")
        raw_dir = store.paper_raw_dir(state["paper_id"])
        metadata = read_json(raw_dir / "metadata.json")
        evidence = extract_evidence(raw_dir, state["paper_id"])
        if isinstance(provider, MockLLM):
            generated = provider.generate_ingest(metadata, [item.model_dump() for item in evidence])
        else:
            generated = provider.generate_json(
                "Compile a paper Wiki candidate as JSON with summary_sections, claims, and concepts. "
                "Each claim citation must use an evidence_id from the supplied evidence.",
                {"paper": metadata, "evidence": [item.model_dump() for item in evidence]},
            )
        staging = store.staging_wiki_dir(state["run_id"])
        if staging.exists():
            shutil.rmtree(staging)
        evidence_path = staging / "evidence" / f"{state['paper_id']}.jsonl"
        write_evidence(evidence_path, evidence)
        summary_path = staging / "summaries" / f"{state['paper_id']}.md"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(render_summary(metadata, generated, evidence), encoding="utf-8")
        concept_paths: list[tuple[str, Path]] = []
        for concept in generated.get("concepts", []):
            concept_id = concept.lower().replace(" ", "-")
            path = staging / "concepts" / f"{concept_id}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_concept(concept, state["paper_id"], evidence), encoding="utf-8")
            concept_paths.append((concept, path))
        write_indexes(staging, metadata, summary_path, concept_paths, evidence)
        state["review_target"] = "ingest"
        state["ingest_attempts"] = state.get("ingest_attempts", 0) + 1
        _event(store, state, "node_completed", node, "Staged Wiki artifacts", {"evidence_count": len(evidence)})
        return state

    def review(state: PipelineState) -> dict[str, Any]:
        node = "review"
        state["current_node"] = node
        _event(store, state, "node_started", node, f"Reviewing {state.get('review_target', 'unknown')} output")
        if state.get("review_target") == "ingest":
            staged_evidence = store.staging_wiki_dir(state["run_id"]) / "evidence" / f"{state['paper_id']}.jsonl"
            evidence = read_evidence(staged_evidence)
            review = ReviewResult(status="supported" if evidence else "unsupported", checked_evidence=len(evidence), feedback=[] if evidence else ["No evidence records generated"])
        else:
            evidence_path = store.wiki / "evidence" / f"{state['paper_id']}.jsonl"
            qa = QAResult.model_validate(state["qa_result"])
            status, feedback = validate_qa(qa, evidence_path)
            review = ReviewResult(status=status, feedback=feedback, checked_evidence=len(qa.citations))
        state["review"] = review.model_dump()
        _event(store, state, "review_completed", node, review.status, {"feedback": review.feedback})
        return state

    def route_after_review(state: PipelineState) -> str:
        review = state.get("review", {})
        if review.get("status") == "supported":
            return "end"
        if state.get("review_target") == "ingest" and state.get("ingest_attempts", 0) <= 1:
            return "wiki_ingest"
        return "end"

    graph = StateGraph(PipelineState)
    graph.add_node("wiki_ingest", wiki_ingest)
    graph.add_node("review", review)
    graph.add_edge(START, "wiki_ingest")
    graph.add_edge("wiki_ingest", "review")
    graph.add_conditional_edges("review", route_after_review, {"wiki_ingest": "wiki_ingest", "end": END})
    return graph.compile()


def _build_qa_graph(store: FileSystemStore, provider: Any):
    graph = _build_ingest_graph(store, provider)
    # The shared graph is intentionally small; QA-only runs use a starting state that skips ingestion.
    return graph


def _new_run(store: FileSystemStore, mode: str, paper_id: str, question: str | None = None) -> PipelineState:
    run_id = uuid.uuid4().hex
    state: PipelineState = {"run_id": run_id, "workspace": str(store.workspace), "paper_id": paper_id, "mode": mode, "ingest_attempts": 0, "qa_attempts": 0}
    if question is not None:
        state["question"] = question
    store.checkpoint(run_id, state)
    return state


def run_ingest(
    workspace: Path,
    mineru_path: Path | None = None,
    source_pdf: Path | None = None,
    paper_id: str | None = None,
    title: str | None = None,
    authors: list[str] | None = None,
    year: int | None = None,
    llm_mode: str = "mock",
    *,
    mineru_token: str | None = None,
) -> dict[str, Any]:
    """Ingest a paper from local MinerU output or, when omitted, the precise API."""
    store = FileSystemStore(workspace)
    parsed_task: dict[str, Any] | None = None
    temporary_mineru_path: Path | None = None
    if mineru_path is None:
        if source_pdf is None:
            raise ValueError("source_pdf is required when mineru_path is not provided")
        temporary_mineru_path = Path(tempfile.mkdtemp(prefix="paperscout-mineru-"))
        try:
            parsed = parse_with_mineru_api(source_pdf, temporary_mineru_path, token=mineru_token)
            mineru_path = parsed.output_dir
            parsed_task = parsed.task_metadata
            imported = import_preparsed(
                workspace,
                mineru_path,
                source_pdf,
                paper_id,
                title,
                authors,
                year,
                task_metadata=parsed_task,
            )
        finally:
            shutil.rmtree(temporary_mineru_path, ignore_errors=True)
    else:
        imported = import_preparsed(workspace, mineru_path, source_pdf, paper_id, title, authors, year)
    state = _new_run(store, llm_mode, imported.paper_id)
    graph = _build_ingest_graph(store, _provider(llm_mode))
    final = graph.invoke(state)
    if final.get("review", {}).get("status") == "supported":
        store.publish_staged_wiki(final["run_id"])
        write_health_report(workspace)
        result = {"status": "published", "paper_id": imported.paper_id, "review": final["review"]}
        _event(store, final, "wiki_published", "review", "Published reviewed Wiki")
    else:
        result = {"status": "failed", "paper_id": imported.paper_id, "review": final.get("review", {})}
    _event(store, final, "run_finished", "review", result["status"])
    store.write_result(final["run_id"], result)
    return result


def run_qa(workspace: Path, question: str, paper_id: str | None = None, llm_mode: str = "mock") -> dict[str, Any]:
    store = FileSystemStore(workspace)
    sources_path = store.wiki / "indexes" / "sources.json"
    if not sources_path.exists():
        raise FileNotFoundError("Published Wiki does not exist; run run_ingest first")
    sources = read_json(sources_path)
    selected = paper_id or sources[0]["paper_id"]
    state = _new_run(store, llm_mode, selected, question)
    # Start at QA while retaining the same review and checkpoint machinery.
    state["current_node"] = "retrieval_qa"
    graph_builder = StateGraph(PipelineState)
    provider = _provider(llm_mode)

    def retrieval_qa(state: PipelineState) -> PipelineState:
        state["current_node"] = "retrieval_qa"
        chunks = retrieve(store.workspace, state["question"])
        state["qa_result"] = provider.generate_qa(state["question"], chunks) if isinstance(provider, MockLLM) else provider.generate_json("Generate a JSON answer with answer, claims, and citations.", {"question": state["question"], "chunks": chunks})
        state["review_target"] = "qa"
        state["qa_attempts"] = state.get("qa_attempts", 0) + 1
        _event(store, state, "node_completed", "retrieval_qa", "Generated QA result", {"retrieved_chunks": len(chunks)})
        return state

    def review(state: PipelineState) -> PipelineState:
        state["current_node"] = "review"
        _event(store, state, "node_started", "review", "Reviewing QA citations")
        qa = QAResult.model_validate(state["qa_result"])
        evidence_path = store.wiki / "evidence" / f"{state['paper_id']}.jsonl"
        status, feedback = validate_qa(qa, evidence_path)
        state["review"] = ReviewResult(status=status, feedback=feedback, checked_evidence=len(qa.citations)).model_dump()
        _event(store, state, "node_completed", "review", state["review"]["status"], {"feedback": feedback})
        return state

    def route(state: PipelineState) -> str:
        if state.get("review", {}).get("status") == "supported":
            return "end"
        return "retrieval_qa" if state.get("qa_attempts", 0) <= 1 else "end"

    graph_builder.add_node("retrieval_qa", retrieval_qa)
    graph_builder.add_node("review", review)
    graph_builder.add_edge(START, "retrieval_qa")
    graph_builder.add_edge("retrieval_qa", "review")
    graph_builder.add_conditional_edges("review", route, {"retrieval_qa": "retrieval_qa", "end": END})
    final = graph_builder.compile().invoke(state)
    result = {"status": "completed" if final["review"]["status"] == "supported" else "failed", "qa": final["qa_result"], "review": final["review"]}
    _event(store, final, "run_finished", "review", result["status"])
    store.write_result(final["run_id"], result)
    return result
