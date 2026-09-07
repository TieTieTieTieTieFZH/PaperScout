from __future__ import annotations

import hashlib
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .health import check_wiki_rules, write_health_report_for_wiki
from .importer import import_preparsed
from .llm import MockLLM, OpenAICompatibleResponsesLLM
from .mineru import parse_with_mineru_api
from .evidence import extract_section_evidence
from .models import AgentKind, EventKind, IngestGraphState, ReviewVerdict, RunStatus, WorkflowEvent
from .prompts import INGEST_SYSTEM_PROMPT, build_ingest_repair_prompt, build_ingest_user_prompt
from .storage import FileSystemStore, read_json, sha256_file, write_json
from .wiki import (
    render_citable_sections, render_paper_wiki, validate_wiki_markdown,
    write_canonical_indexes, write_section_evidence,
)


class IngestCoverageError(ValueError):
    pass


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
            f"({len(selected)}/{len(eligible)} eligible sections, budget={input_budget} chars)"
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


def _run_ingest_from_raw_store(store: FileSystemStore, paper_id: str, llm_mode: str) -> dict[str, Any]:
    if _existing_wiki_for_paper(store, paper_id):
        raise ValueError(f"Wiki already contains paper_id {paper_id}; refusing to overwrite published artifacts")
    provider = _provider(llm_mode)
    window = getattr(getattr(provider, "settings", None), "ingest_context_window", 128_000)
    state = _new_run(store, llm_mode, paper_id)
    try:
        state.current_node = "prepare_ingest_context"
        context = prepare_ingest_context(store, paper_id, window)
        state.input_coverage = context["coverage"]
        state.input_evidence_ids = [item.evidence_id for item in context["evidence"]]
        state.current_node = "ingest_agent"
        _event(store, state, EventKind.MODEL_STARTED, state.current_node, "Requesting Wiki candidate", agent=AgentKind.INGEST)
        raw_output = ingest_agent(provider, context)
        state.attempt = 1
        state.candidate_markdown = raw_output
        state.candidate_sha256 = hashlib.sha256(raw_output.encode("utf-8")).hexdigest()
        _event(store, state, EventKind.MODEL_COMPLETED, state.current_node, "Received Wiki candidate", {"attempt": 1}, agent=AgentKind.INGEST)
        (store.run_dir(state.run_id) / "ingest-output-1.md").write_text(raw_output, encoding="utf-8")
        try:
            candidate = validate_wiki_markdown(raw_output, paper=context["paper"], evidence=context["evidence"])
        except ValueError as first_error:
            state.rule_errors = [str(first_error)]
            write_json(store.run_dir(state.run_id) / "validation-errors-1.json", {"error": str(first_error)})
            _event(store, state, EventKind.MODEL_STARTED, "repair_ingest", "Requesting repaired Wiki candidate", {"attempt": 2}, agent=AgentKind.INGEST)
            repaired = repair_ingest_summary(provider, raw_output, str(first_error), context)
            state.attempt = 2
            state.candidate_markdown = repaired
            state.candidate_sha256 = hashlib.sha256(repaired.encode("utf-8")).hexdigest()
            _event(store, state, EventKind.MODEL_COMPLETED, "repair_ingest", "Received repaired Wiki candidate", {"attempt": 2}, agent=AgentKind.INGEST)
            (store.run_dir(state.run_id) / "ingest-output-2.md").write_text(repaired, encoding="utf-8")
            try:
                candidate = validate_wiki_markdown(repaired, paper=context["paper"], evidence=context["evidence"])
            except ValueError as second_error:
                state.rule_errors.append(str(second_error))
                state.status = RunStatus.FAILED
                state.last_error = str(second_error)
                write_json(store.run_dir(state.run_id) / "validation-errors-2.json", {"error": str(second_error)})
                result = {"status": "failed", "paper_id": paper_id, "error": str(second_error), "run_id": state.run_id}
                _event(store, state, EventKind.RUN_FAILED, "validate_wiki", str(second_error))
                store.write_result(state.run_id, result)
                return result

        if _raw_hashes(context["raw_dir"]) != context["raw_hashes"]:
            raise RuntimeError("Raw input changed during ingest; refusing to publish")
        state.current_node = "render_wiki"
        staging = render_wiki(store, state, context, candidate)
        state.staging_path = str(staging)
        write_health_report_for_wiki(staging)
        _event(store, state, EventKind.REVIEW_STARTED, "wiki_rules", "Checking staged Wiki rules")
        reviewed = check_wiki_rules(staging)
        state.review = reviewed
        _event(store, state, EventKind.REVIEW_COMPLETED, "wiki_rules", "Completed staged Wiki rule check", {"verdict": reviewed.verdict.value})
        if reviewed.verdict != ReviewVerdict.APPROVE:
            state.status = RunStatus.FAILED
            result = {"status": "failed", "paper_id": paper_id, "review": reviewed.model_dump(mode="json"), "run_id": state.run_id}
            _event(store, state, EventKind.RUN_FAILED, "wiki_rules", "Staged Wiki failed rule checks")
            store.write_result(state.run_id, result)
            return result
        store.publish_staged_wiki(state.run_id)
        state.published = True
        state.status = RunStatus.COMPLETED
        result = {"status": "published", "paper_id": paper_id, "review": reviewed.model_dump(mode="json"), "run_id": state.run_id}
        _event(store, state, EventKind.RUN_COMPLETED, "publish_wiki", "Published reviewed Wiki")
        store.write_result(state.run_id, result)
        return result
    except Exception as exc:
        state.status = RunStatus.FAILED
        state.last_error = str(exc)
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
