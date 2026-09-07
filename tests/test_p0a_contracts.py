from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from paperscout.graph_runtime import GraphRuntime, graph_config
from paperscout.models import (
    AgentKind,
    EventKind,
    IngestGraphState,
    ProjectState,
    QAGraphState,
    SessionState,
    WikiCandidate,
    WorkflowEvent,
)


def _wiki_payload() -> dict:
    kinds = [
        "research_question",
        "core_idea",
        "method",
        "experiment_overview",
        "conclusion_and_limitations",
    ]
    return {
        "paper_id": "paper-1",
        "title": "Test paper",
        "sections": [
            {"kind": kind, "content": f"Content for {kind}", "evidence_ids": ["paper-1:s0002"]}
            for kind in kinds
        ],
        "input_evidence_ids": ["paper-1:s0002"],
        "source_sha256": "source-hash",
    }


def test_wiki_candidate_requires_canonical_order_and_input_owned_evidence() -> None:
    candidate = WikiCandidate.model_validate(_wiki_payload())
    assert len(candidate.sections) == 5

    wrong_order = _wiki_payload()
    wrong_order["sections"][0], wrong_order["sections"][1] = wrong_order["sections"][1], wrong_order["sections"][0]
    with pytest.raises(ValidationError):
        WikiCandidate.model_validate(wrong_order)

    wrong_paper = _wiki_payload()
    wrong_paper["sections"][0]["evidence_ids"] = ["paper-2:s0002"]
    with pytest.raises(ValidationError):
        WikiCandidate.model_validate(wrong_paper)


def test_session_and_graph_states_are_strict_contracts(tmp_path: Path) -> None:
    session = SessionState(session_id="session-1")
    project = ProjectState(project_id="default")
    assert session.project_id == "default"
    assert session.memory.evidence_ids == []
    assert project.memory.evidence_ids == []
    ingest = IngestGraphState(
        run_id="run-1",
        thread_id="ingest:run-1",
        workspace=str(tmp_path),
        paper_id="paper-1",
        llm_mode="mock",
    )
    qa = QAGraphState(
        run_id="run-2",
        thread_id="qa:session-1",
        session_id="session-1",
        workspace=str(tmp_path),
        question="What is the method?",
    )
    assert ingest.published is False
    assert qa.project_id == "default"
    assert qa.read_budget.calls_used == 0
    with pytest.raises(ValidationError):
        SessionState.model_validate({"session_id": "session-1", "unknown": True})
    with pytest.raises(ValidationError):
        ProjectState.model_validate({"project_id": "default", "unknown": True})


def test_event_contract_requires_correlation_and_known_event_type() -> None:
    event = WorkflowEvent(
        event_id="event-1",
        sequence=0,
        run_id="run-1",
        thread_id="ingest:run-1",
        agent=AgentKind.INGEST,
        event_type=EventKind.RUN_STARTED,
    )
    assert event.event_type.value == "run.started"
    with pytest.raises(ValidationError):
        WorkflowEvent.model_validate(
            {
                "event_id": "event-2",
                "sequence": 1,
                "run_id": "run-1",
                "thread_id": "ingest:run-1",
                "agent": "ingest",
                "event_type": "node.started",
            }
        )


class CounterState(TypedDict):
    value: int


def _counter_graph(runtime: GraphRuntime):
    builder = StateGraph(CounterState)
    builder.add_node("increment", lambda state: {"value": state["value"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    return runtime.compile(builder)


def test_sqlite_checkpointer_survives_runtime_reconstruction(tmp_path: Path) -> None:
    config = graph_config("thread-1")
    with GraphRuntime.open(tmp_path) as runtime:
        graph = _counter_graph(runtime)
        assert graph.invoke({"value": 1}, config)["value"] == 2
        assert runtime.checkpoint_path.is_file()

    with GraphRuntime.open(tmp_path) as runtime:
        graph = _counter_graph(runtime)
        snapshot = graph.get_state(config)
        assert snapshot.values["value"] == 2


def test_graph_config_rejects_empty_thread_id() -> None:
    with pytest.raises(ValueError):
        graph_config("  ")
