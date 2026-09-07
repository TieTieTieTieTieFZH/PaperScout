from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Annotated, Any

from langgraph.graph import END, START, StateGraph
from pydantic import Field, TypeAdapter, ValidationError

from .graph_runtime import GraphRuntime, graph_config
from .llm import MockLLM, OpenAICompatibleResponsesLLM
from .models import (
    AgentKind,
    AgentToolCall,
    EventKind,
    QAAnswer,
    QAFinalEnvelope,
    QAGraphState,
    QAToolCallEnvelope,
    ReadBudget,
    RunStatus,
    SessionMessage,
    WorkflowEvent,
)
from .prompts import QA_SYSTEM_PROMPT, build_qa_context_prompt
from .read_tool import model_visible_read_result, read_project_file
from .storage import FileSystemStore, write_json


QAResponseEnvelope = Annotated[QAToolCallEnvelope | QAFinalEnvelope, Field(discriminator="type")]
QA_RESPONSE_ADAPTER = TypeAdapter(QAResponseEnvelope)
ANSWER_EVIDENCE_MARK = re.compile(r"\[evidence:([^\]]+)\]")


def parse_qa_model_response(raw_output: str) -> QAToolCallEnvelope | QAFinalEnvelope:
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise ValueError("QA model response must be valid JSON without code fences or extra text") from exc
    try:
        return QA_RESPONSE_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise ValueError("QA model response must be a valid QA envelope") from exc


def _provider(mode: str) -> Any:
    if mode == "mock":
        return MockLLM()
    if mode == "real":
        return OpenAICompatibleResponsesLLM()
    raise ValueError("llm_mode must be 'mock' or 'real'")


def _message(role: str, content: str | dict[str, Any], tool_call_id: str | None = None) -> SessionMessage:
    return SessionMessage(
        message_id=uuid.uuid4().hex,
        role=role,
        content=content,
        tool_call_id=tool_call_id,
    )


def _event(
    store: FileSystemStore,
    state: QAGraphState,
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
            session_id=state.session_id,
            agent=agent,
            event_type=event_type,
            node=node,
            message=message,
            data=data or {},
        )
    )
    state.event_sequence += 1


def _model_messages(state: QAGraphState) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": QA_SYSTEM_PROMPT},
        {
            "role": "system",
            "content": build_qa_context_prompt(
                history_summary=state.history_summary,
                memory=state.memory.model_dump(mode="json"),
            ),
        },
    ]
    for item in state.messages:
        message: dict[str, Any] = {"role": item.role, "content": item.content}
        if item.tool_call_id:
            message["tool_call_id"] = item.tool_call_id
        messages.append(message)
    return messages


def _new_state(
    store: FileSystemStore,
    question: str,
    session_id: str,
    read_budget: ReadBudget | None,
) -> QAGraphState:
    run_id = uuid.uuid4().hex
    state = QAGraphState(
        run_id=run_id,
        thread_id=f"qa:{session_id}:{run_id}",
        session_id=session_id,
        workspace=str(store.workspace),
        question=question,
        status=RunStatus.RUNNING,
        messages=[_message("user", question)],
        read_budget=read_budget or ReadBudget(),
    )
    _event(store, state, EventKind.RUN_STARTED, "qa", "QA run started", agent=AgentKind.QA)
    return state


def build_qa_graph(runtime: GraphRuntime, *, provider: Any):
    """Compile the QA model/tool loop against the owned SQLite checkpointer."""

    def model_node(state: QAGraphState) -> dict[str, Any]:
        state.current_node = "qa_agent"
        store = FileSystemStore(Path(state.workspace))
        if state.model_steps >= state.max_model_steps:
            return {
                "current_node": state.current_node,
                "last_error": "QA model step budget is exhausted",
            }
        try:
            _event(
                store,
                state,
                EventKind.MODEL_STARTED,
                state.current_node,
                "Requesting QA action",
                {"model_step": state.model_steps + 1},
                agent=AgentKind.QA,
            )
            raw_output = provider.generate_raw_text(_model_messages(state))
            output_path = store.run_dir(state.run_id) / f"qa-output-{state.model_steps + 1}.txt"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(raw_output, encoding="utf-8")
            _event(
                store,
                state,
                EventKind.MODEL_COMPLETED,
                state.current_node,
                "Received QA action",
                {"model_step": state.model_steps + 1},
                agent=AgentKind.QA,
            )
            parsed = parse_qa_model_response(raw_output)
            messages = list(state.messages)
            parsed_payload = parsed.model_dump(mode="json")
            if isinstance(parsed, QAToolCallEnvelope):
                used_ids = {message.tool_call_id for message in messages if message.tool_call_id}
                if parsed.id in used_ids:
                    raise ValueError(f"duplicate QA tool call id: {parsed.id}")
                messages.append(_message("assistant", parsed_payload, parsed.id))
                return {
                    "current_node": state.current_node,
                    "event_sequence": state.event_sequence,
                    "messages": [message.model_dump(mode="json") for message in messages],
                    "current_tool_calls": [
                        AgentToolCall(
                            id=parsed.id,
                            name=parsed.name,
                            arguments=parsed.arguments,
                        ).model_dump(mode="json")
                    ],
                    "candidate_answer": None,
                    "model_steps": state.model_steps + 1,
                    "last_error": None,
                }
            answer = QAAnswer.model_validate(parsed.model_dump(exclude={"type"}))
            messages.append(_message("assistant", parsed_payload))
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "messages": [message.model_dump(mode="json") for message in messages],
                "current_tool_calls": [],
                "candidate_answer": answer.model_dump(mode="json"),
                "model_steps": state.model_steps + 1,
                "last_error": None,
            }
        except Exception as exc:
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "model_steps": state.model_steps + 1,
                "last_error": str(exc),
            }

    def tool_node(state: QAGraphState) -> dict[str, Any]:
        state.current_node = "read_project_file"
        store = FileSystemStore(Path(state.workspace))
        try:
            if len(state.current_tool_calls) != 1:
                raise ValueError("QA must request exactly one tool call per model action")
            call = state.current_tool_calls[0]
            if call.name != "read_project_file":
                raise ValueError(f"unsupported QA tool: {call.name}")
            _event(
                store,
                state,
                EventKind.TOOL_STARTED,
                state.current_node,
                "Reading project resource",
                {"tool_call_id": call.id},
                agent=AgentKind.QA,
            )
            outcome = read_project_file(Path(state.workspace), call.arguments, state.read_budget)
            audit_dir = store.run_dir(state.run_id) / "tools" / f"{state.model_steps:03d}"
            write_json(audit_dir / "request.json", call.model_dump(mode="json"))
            write_json(audit_dir / "result.json", outcome.model_dump(mode="json"))
            visible = model_visible_read_result(outcome)
            _event(
                store,
                state,
                EventKind.TOOL_COMPLETED,
                state.current_node,
                "Project resource read completed",
                {
                    "tool_call_id": call.id,
                    "ok": visible["ok"],
                    "error_code": outcome.result.error_code,
                },
                agent=AgentKind.QA,
            )
            messages = [*state.messages, _message("tool", visible, call.id)]
            resources = list(state.read_resources)
            if outcome.record is not None:
                resources.append(outcome.record)
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "messages": [message.model_dump(mode="json") for message in messages],
                "read_budget": outcome.budget.model_dump(mode="json"),
                "read_resources": [record.model_dump(mode="json") for record in resources],
                "current_tool_calls": [],
                "last_error": None,
            }
        except Exception as exc:
            return {
                "current_node": state.current_node,
                "event_sequence": state.event_sequence,
                "last_error": str(exc),
            }

    def validate_answer_node(state: QAGraphState) -> dict[str, Any]:
        state.current_node = "validate_answer"
        try:
            if state.candidate_answer is None:
                raise ValueError("QA final answer is missing")
            observed = {
                evidence_id
                for record in state.read_resources
                for evidence_id in record.evidence_ids
            }
            cited = set(state.candidate_answer.cited_evidence_ids)
            unknown = sorted(cited - observed)
            if unknown:
                raise ValueError(f"answer cites evidence not observed in tool results: {unknown}")
            memory_unknown = sorted(set(state.candidate_answer.memory_patch.evidence_ids) - observed)
            if memory_unknown:
                raise ValueError(f"memory patch cites evidence not observed in tool results: {memory_unknown}")
            body_citations = set(ANSWER_EVIDENCE_MARK.findall(state.candidate_answer.answer))
            if body_citations != cited:
                raise ValueError("answer body evidence markers must match cited_evidence_ids")
            return {"current_node": state.current_node, "last_error": None}
        except Exception as exc:
            return {"current_node": state.current_node, "last_error": str(exc)}

    def complete_node(state: QAGraphState) -> dict[str, Any]:
        state.current_node = "complete_qa"
        store = FileSystemStore(Path(state.workspace))
        if state.candidate_answer is None:
            return {"current_node": state.current_node, "last_error": "QA final answer is missing"}
        answer = state.candidate_answer
        result = {
            "status": "completed",
            "run_id": state.run_id,
            "thread_id": state.thread_id,
            "session_id": state.session_id,
            "answer": answer.answer,
            "claims": [claim.model_dump(mode="json") for claim in answer.claims],
            "cited_evidence_ids": answer.cited_evidence_ids,
            "answer_status": answer.status.value,
            "memory_patch": answer.memory_patch.model_dump(mode="json"),
            "read_resources": [record.model_dump(mode="json") for record in state.read_resources],
            "read_budget": state.read_budget.model_dump(mode="json"),
        }
        _event(store, state, EventKind.RUN_COMPLETED, state.current_node, "QA run completed", agent=AgentKind.QA)
        store.write_result(state.run_id, result)
        return {
            "current_node": state.current_node,
            "event_sequence": state.event_sequence,
            "status": RunStatus.COMPLETED,
            "result": result,
            "last_error": None,
        }

    def fail_node(state: QAGraphState) -> dict[str, Any]:
        state.current_node = "fail_qa"
        store = FileSystemStore(Path(state.workspace))
        error = state.last_error or "QA run failed"
        result = {
            "status": "failed",
            "run_id": state.run_id,
            "thread_id": state.thread_id,
            "session_id": state.session_id,
            "error": error,
            "read_budget": state.read_budget.model_dump(mode="json"),
        }
        _event(store, state, EventKind.RUN_FAILED, state.current_node, error, agent=AgentKind.QA)
        store.write_result(state.run_id, result)
        return {
            "current_node": state.current_node,
            "event_sequence": state.event_sequence,
            "status": RunStatus.FAILED,
            "result": result,
        }

    def route_model(state: QAGraphState) -> str:
        if state.last_error:
            return "fail"
        if state.current_tool_calls:
            return "tool"
        if state.candidate_answer is not None:
            return "answer"
        return "fail"

    def route_error(state: QAGraphState) -> str:
        return "fail" if state.last_error else "continue"

    builder = StateGraph(QAGraphState)
    builder.add_node("qa_agent", model_node)
    builder.add_node("read_project_file", tool_node)
    builder.add_node("validate_answer", validate_answer_node)
    builder.add_node("complete_qa", complete_node)
    builder.add_node("fail_qa", fail_node)
    builder.add_edge(START, "qa_agent")
    builder.add_conditional_edges(
        "qa_agent",
        route_model,
        {"tool": "read_project_file", "answer": "validate_answer", "fail": "fail_qa"},
    )
    builder.add_conditional_edges(
        "read_project_file",
        route_error,
        {"continue": "qa_agent", "fail": "fail_qa"},
    )
    builder.add_conditional_edges(
        "validate_answer",
        route_error,
        {"continue": "complete_qa", "fail": "fail_qa"},
    )
    builder.add_edge("complete_qa", END)
    builder.add_edge("fail_qa", END)
    return runtime.compile(builder)


def run_qa(
    workspace: Path,
    question: str,
    session_id: str,
    llm_mode: str = "mock",
    *,
    provider: Any | None = None,
    read_budget: ReadBudget | None = None,
) -> dict[str, Any]:
    store = FileSystemStore(workspace)
    selected_provider = provider or _provider(llm_mode)
    state = _new_state(store, question, session_id, read_budget)
    try:
        with GraphRuntime.open(store.workspace) as runtime:
            graph = build_qa_graph(runtime, provider=selected_provider)
            final_state = graph.invoke(state.model_dump(mode="json"), graph_config(state.thread_id))
        result = final_state.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("QA graph completed without a result")
        return result
    except Exception as exc:
        state.status = RunStatus.FAILED
        state.last_error = str(exc)
        events_path = store.run_dir(state.run_id) / "events.jsonl"
        if events_path.exists():
            state.event_sequence = len(events_path.read_text(encoding="utf-8").splitlines())
        _event(store, state, EventKind.RUN_FAILED, state.current_node or "qa", str(exc), agent=AgentKind.QA)
        result = {
            "status": "failed",
            "run_id": state.run_id,
            "thread_id": state.thread_id,
            "session_id": session_id,
            "error": str(exc),
            "read_budget": state.read_budget.model_dump(mode="json"),
        }
        store.write_result(state.run_id, result)
        return result
