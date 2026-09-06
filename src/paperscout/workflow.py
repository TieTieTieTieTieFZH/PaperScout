from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, TypedDict

from .health import review_wiki, write_health_report_for_wiki
from .importer import import_preparsed
from .llm import MockLLM, OpenAICompatibleResponsesLLM
from .mineru import parse_with_mineru_api
from .models import AssistantAgentMessage, QAResult, ReadRawToolArguments, ReviewResult, RunEvent, ToolAgentMessage
from .prompts import INGEST_BUDGET_EXHAUSTED_PROMPT, INGEST_SYSTEM_PROMPT, build_ingest_repair_prompt, build_ingest_user_prompt
from .storage import FileSystemStore, read_json, sha256_file, write_json
from .wiki import (
    extract_evidence, migrate_legacy_wiki, render_citable_document, render_summary, retrieve,
    validate_qa, validate_summary_markdown, write_evidence, write_indexes,
)


class PipelineState(TypedDict, total=False):
    run_id: str
    workspace: str
    paper_id: str
    question: str
    mode: str
    current_node: str
    review: dict[str, Any]
    qa_result: dict[str, Any]


CITABLE_DOCUMENT_PATH = "mineru/citable-evidence.md"
MAX_RAW_READS = 24


def _provider(mode: str):
    if mode == "mock":
        return MockLLM()
    if mode == "real":
        return OpenAICompatibleResponsesLLM()
    raise ValueError("llm_mode must be 'mock' or 'real'")


def _event(store: FileSystemStore, state: PipelineState, event_type: str, node: str, message: str, data: dict[str, Any] | None = None) -> None:
    store.append_event(state["run_id"], RunEvent(event_type=event_type, node=node, message=message, data=data or {}))
    store.checkpoint(state["run_id"], dict(state))


def _new_run(store: FileSystemStore, mode: str, paper_id: str, question: str | None = None) -> PipelineState:
    state: PipelineState = {"run_id": uuid.uuid4().hex, "workspace": str(store.workspace), "paper_id": paper_id, "mode": mode}
    if question is not None:
        state["question"] = question
    store.checkpoint(state["run_id"], state)
    return state


def _raw_hashes(raw_dir: Path) -> dict[str, str]:
    return {
        str(path.relative_to(raw_dir)): sha256_file(path)
        for path in sorted(raw_dir.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _existing_wiki_for_paper(store: FileSystemStore, paper_id: str) -> bool:
    if (store.wiki / "summaries" / f"{paper_id}.md").exists() or (store.wiki / "evidence" / f"{paper_id}.jsonl").exists():
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
    evidence = extract_evidence(raw_dir, paper_id)
    if not evidence:
        raise ValueError("No usable evidence could be extracted from raw MinerU output")
    citable_document = render_citable_document(evidence)
    return {
        "raw_dir": raw_dir,
        "paper": paper,
        "evidence": evidence,
        "citable_document": citable_document,
        "inline_document": citable_document if len(citable_document) <= context_window // 2 else None,
        "per_read_chars": max(1, context_window // 4),
        "total_chars": max(1, (context_window * 3) // 4),
        "raw_hashes": _raw_hashes(raw_dir),
    }


def _read_citable_document(context: dict[str, Any], call: ReadRawToolArguments, remaining_chars: int) -> dict[str, Any]:
    if call.path != CITABLE_DOCUMENT_PATH:
        return {"error": f"只允许读取 {CITABLE_DOCUMENT_PATH}"}
    content = context["citable_document"]
    limit = min(call.max_chars or context["per_read_chars"], context["per_read_chars"], remaining_chars)
    start = min(call.offset_chars, len(content))
    end = min(start + limit, len(content))
    return {"path": CITABLE_DOCUMENT_PATH, "offset_chars": start, "content": content[start:end], "truncated": end < len(content)}


def _run_ingest_messages(provider: Any, context: dict[str, Any], prompt: str, event_callback: Any | None) -> str:
    messages: list[dict[str, Any]] = [{"role": "system", "content": INGEST_SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
    consumed = 0
    reads = 0
    while reads < MAX_RAW_READS and consumed < context["total_chars"]:
        raw_output = provider.generate_raw_text(messages)
        try:
            assistant = AssistantAgentMessage.model_validate(json.loads(raw_output))
        except (TypeError, json.JSONDecodeError, ValueError):
            return str(raw_output)
        if assistant.content is not None or not assistant.tool_calls:
            return str(raw_output)
        messages.append(assistant.model_dump(exclude_none=True))
        for tool_call in assistant.tool_calls:
            if reads >= MAX_RAW_READS or consumed >= context["total_chars"]:
                result = {"error": "read_raw 读取额度已耗尽"}
            elif tool_call.name != "read_raw":
                result = {"error": f"工具 {tool_call.name} 不在白名单中"}
            else:
                try:
                    arguments = ReadRawToolArguments.model_validate(tool_call.arguments)
                    result = _read_citable_document(context, arguments, context["total_chars"] - consumed)
                    consumed += len(result.get("content", ""))
                    reads += 1
                    if event_callback:
                        event_callback("raw_read", "ingest_agent", "Read citable document", {"path": arguments.path, "chars": len(result.get("content", ""))})
                except ValueError as exc:
                    result = {"error": f"read_raw 参数无效: {exc}"}
            messages.append(ToolAgentMessage(role="tool", tool_call_id=tool_call.id, content=result).model_dump())
    messages.append({"role": "user", "content": INGEST_BUDGET_EXHAUSTED_PROMPT})
    return provider.generate_raw_text(messages)


def ingest_agent(provider: Any, context: dict[str, Any], event_callback: Any | None = None) -> str:
    prompt = build_ingest_user_prompt(
        paper=context["paper"], citable_document=context["inline_document"],
        per_read_chars=context["per_read_chars"], total_chars=context["total_chars"], max_raw_reads=MAX_RAW_READS,
    )
    return _run_ingest_messages(provider, context, prompt, event_callback)


def repair_ingest_summary(provider: Any, raw_output: str, error: str, context: dict[str, Any]) -> str:
    prompt = build_ingest_repair_prompt(raw_output=raw_output, validation_error=error, citable_document=context["inline_document"])
    return _run_ingest_messages(provider, context, prompt, None)


def render_wiki(store: FileSystemStore, state: PipelineState, context: dict[str, Any], summary_body: str) -> Path:
    """Publish only deterministic artifacts derived from a validated Markdown body."""
    store.run_dir(state["run_id"]).mkdir(parents=True, exist_ok=True)
    (store.run_dir(state["run_id"]) / "ingest-summary.md").write_text(summary_body, encoding="utf-8")
    staging = store.prepare_staging_wiki(state["run_id"])
    migrate_legacy_wiki(staging)
    paper_id = state["paper_id"]
    write_evidence(staging / "evidence" / f"{paper_id}.jsonl", context["evidence"])
    summary_path = staging / "summaries" / f"{paper_id}.md"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(render_summary(context["paper"], summary_body), encoding="utf-8")
    write_indexes(staging, context["paper"], summary_path)
    return staging


def _run_ingest_from_raw_store(store: FileSystemStore, paper_id: str, llm_mode: str) -> dict[str, Any]:
    if _existing_wiki_for_paper(store, paper_id):
        raise ValueError(f"Wiki already contains paper_id {paper_id}; refusing to overwrite published artifacts")
    provider = _provider(llm_mode)
    window = getattr(getattr(provider, "settings", None), "ingest_context_window", 128_000)
    state = _new_run(store, llm_mode, paper_id)
    try:
        state["current_node"] = "prepare_ingest_context"
        _event(store, state, "node_started", state["current_node"], "Preparing citable raw evidence")
        context = prepare_ingest_context(store, paper_id, window)
        _event(store, state, "node_completed", state["current_node"], "Prepared citable document", {"evidence_count": len(context["evidence"]), "inlined": context["inline_document"] is not None})

        callback = lambda event_type, node, message, data: _event(store, state, event_type, node, message, data)
        state["current_node"] = "ingest_agent"
        _event(store, state, "node_started", state["current_node"], "Requesting final summary Markdown")
        raw_output = ingest_agent(provider, context, callback)
        (store.run_dir(state["run_id"]) / "ingest-output-1.md").write_text(raw_output, encoding="utf-8")
        try:
            summary_body = validate_summary_markdown(raw_output, context["evidence"])
        except ValueError as first_error:
            write_json(store.run_dir(state["run_id"]) / "validation-errors-1.json", {"error": str(first_error)})
            _event(store, state, "validation_failed", "validate_summary_markdown", str(first_error), {"attempt": 1})
            repaired = repair_ingest_summary(provider, raw_output, str(first_error), context)
            (store.run_dir(state["run_id"]) / "ingest-output-2.md").write_text(repaired, encoding="utf-8")
            try:
                summary_body = validate_summary_markdown(repaired, context["evidence"])
            except ValueError as second_error:
                write_json(store.run_dir(state["run_id"]) / "validation-errors-2.json", {"error": str(second_error)})
                result = {"status": "failed", "paper_id": paper_id, "error": str(second_error), "run_id": state["run_id"]}
                _event(store, state, "run_finished", "validate_summary_markdown", "failed")
                store.write_result(state["run_id"], result)
                return result

        if _raw_hashes(context["raw_dir"]) != context["raw_hashes"]:
            raise RuntimeError("Raw input changed during ingest; refusing to publish")
        state["current_node"] = "render_wiki"
        staging = render_wiki(store, state, context, summary_body)
        write_health_report_for_wiki(staging)
        reviewed = review_wiki(staging)
        state["review"] = reviewed.model_dump()
        if reviewed.status != "supported":
            result = {"status": "failed", "paper_id": paper_id, "review": state["review"], "run_id": state["run_id"]}
            store.write_result(state["run_id"], result)
            return result
        store.publish_staged_wiki(state["run_id"])
        result = {"status": "published", "paper_id": paper_id, "review": state["review"], "run_id": state["run_id"]}
        _event(store, state, "wiki_published", "render_wiki", "Published reviewed Wiki")
        store.write_result(state["run_id"], result)
        return result
    except Exception as exc:
        _event(store, state, "run_failed", state.get("current_node", "ingest"), str(exc))
        result = {"status": "failed", "paper_id": paper_id, "error": str(exc), "run_id": state["run_id"]}
        store.write_result(state["run_id"], result)
        return result


def run_ingest_from_raw(workspace: Path, paper_id: str, llm_mode: str = "mock") -> dict[str, Any]:
    return _run_ingest_from_raw_store(FileSystemStore(workspace), paper_id, llm_mode)


def migrate_wiki(workspace: Path) -> dict[str, Any]:
    """Publish a legacy-artifact cleanup without reading raw or calling an LLM."""
    store = FileSystemStore(workspace)
    if not store.wiki.is_dir():
        raise FileNotFoundError("Published Wiki does not exist")
    state = _new_run(store, "migration", "wiki-migration")
    state["current_node"] = "migrate_wiki"
    staging = store.prepare_staging_wiki(state["run_id"])
    migrate_legacy_wiki(staging)
    write_health_report_for_wiki(staging)
    reviewed = review_wiki(staging)
    state["review"] = reviewed.model_dump()
    if reviewed.status != "supported":
        result = {"status": "failed", "review": state["review"], "run_id": state["run_id"]}
        store.write_result(state["run_id"], result)
        return result
    store.publish_staged_wiki(state["run_id"])
    result = {"status": "published", "review": state["review"], "run_id": state["run_id"]}
    _event(store, state, "wiki_migrated", "migrate_wiki", "Published migrated Wiki")
    store.write_result(state["run_id"], result)
    return result


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


def run_qa(workspace: Path, question: str, paper_id: str | None = None, llm_mode: str = "mock") -> dict[str, Any]:
    store = FileSystemStore(workspace)
    if not (store.wiki / "indexes" / "sources.json").exists():
        raise FileNotFoundError("Published Wiki does not exist; run run_ingest first")
    state = _new_run(store, llm_mode, paper_id or "cross-paper", question)
    provider = _provider(llm_mode)
    chunks, evidence_by_id = retrieve(store.workspace, question, paper_id=paper_id)
    instruction = "Answer only from the supplied summaries and evidence. Cite only supplied evidence_id/page/quote records; state when evidence is insufficient."
    raw_qa = provider.generate_qa(question, chunks) if isinstance(provider, MockLLM) else provider.generate_json(instruction, {"question": question, "chunks": chunks})
    try:
        qa = QAResult.model_validate(raw_qa)
        status, feedback = validate_qa(qa, evidence_by_id)
    except ValueError as exc:
        status, feedback, qa = "unsupported", [str(exc)], QAResult(answer="")
    review = ReviewResult(status=status, feedback=feedback, checked_evidence=len(evidence_by_id))
    state["qa_result"] = qa.model_dump()
    state["review"] = review.model_dump()
    result = {"status": "completed" if status == "supported" else "failed", "qa": state["qa_result"], "review": state["review"]}
    _event(store, state, "run_finished", "retrieval_qa", result["status"], {"retrieved_chunks": len(chunks)})
    store.write_result(state["run_id"], result)
    return result
