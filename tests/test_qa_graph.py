from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from paperscout.graph_runtime import GraphRuntime, graph_config
from paperscout.models import (
    AgentToolCall,
    EventKind,
    ProjectState,
    QAAnswer,
    ReadBudget,
    SessionMessage,
    SessionState,
    UserProfile,
    WorkflowEvent,
)
from paperscout.qa import build_qa_graph, parse_qa_model_response, resume_qa, run_qa
from paperscout.project_memory import persist_project_memory
from paperscout.read_tool import model_visible_read_result, read_project_file
from paperscout.session import persist_session
from paperscout.user_profile import save_user_profile


class ScriptedProvider:
    def __init__(
        self,
        responses: list[str],
        *,
        qa_context_window: int | None = None,
    ) -> None:
        self.responses = iter(responses)
        self.requests: list[list[dict[str, Any]]] = []
        if qa_context_window is not None:
            self.settings = SimpleNamespace(qa_context_window=qa_context_window)

    def generate_raw_text(self, messages: list[dict[str, Any]]) -> str:
        self.requests.append(messages)
        return next(self.responses)


def _tool_call(call_id: str, path: str, **arguments: Any) -> str:
    return json.dumps(
        {
            "type": "tool_call",
            "id": call_id,
            "name": "read_project_file",
            "arguments": {"path": path, **arguments},
        }
    )


def _final(
    *,
    answer: str,
    claims: list[dict[str, Any]],
    cited_evidence_ids: list[str],
    status: str = "answered",
    memory_patch: dict[str, Any] | None = None,
) -> str:
    return json.dumps(
        {
            "type": "final",
            "answer": answer,
            "claims": claims,
            "cited_evidence_ids": cited_evidence_ids,
            "status": status,
            "memory_patch": memory_patch
            or {
                "research_goal": None,
                "paper_aliases": {},
                "confirmed_decisions": [],
                "unresolved_questions": [],
                "research_hypotheses": [],
                "evidence_ids": cited_evidence_ids,
            },
        }
    )


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "wiki" / "indexes").mkdir(parents=True)
    (workspace / "wiki" / "papers").mkdir(parents=True)
    (workspace / "wiki" / "evidence" / "paper-1").mkdir(parents=True)
    (workspace / "raw" / "papers" / "paper-1" / "mineru").mkdir(parents=True)
    (workspace / "wiki" / "indexes" / "overview.md").write_text(
        "# PaperScout Wiki\n\n## Test Paper\n\n- Paper ID: `paper-1`\n"
        "- Wiki: [papers/paper-1.md](../papers/paper-1.md)\n",
        encoding="utf-8",
    )
    (workspace / "wiki" / "papers" / "paper-1.md").write_text(
        "# Test Paper\n\n## 方法\n\n该方法使用可核查的处理流程。\n\n[evidence:paper-1:s0001]\n",
        encoding="utf-8",
    )
    (workspace / "wiki" / "evidence" / "paper-1" / "s0001.md").write_text(
        "# Method\n\n- Evidence ID: `paper-1:s0001`\n- Pages: 2\n\n原文方法描述。\n",
        encoding="utf-8",
    )
    (workspace / "raw" / "papers" / "paper-1" / "mineru" / "full.md").write_text(
        "## Method\n\nMinerU 原始方法内容。\n",
        encoding="utf-8",
    )
    return workspace


def test_model_visible_tool_result_uses_slim_json_envelope(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outcome = read_project_file(
        workspace,
        {"path": "wiki/papers/paper-1.md", "max_chars": 20},
        ReadBudget(),
    )

    assert model_visible_read_result(outcome) == {
        "ok": True,
        "path": "wiki/papers/paper-1.md",
        "content": "# Test Paper\n\n## 方法\n",
        "truncated": True,
        "next_offset": 20,
        "evidence_ids": [],
    }
    assert "sha256" not in model_visible_read_result(outcome)
    assert "returned_chars" not in model_visible_read_result(outcome)

    complete = read_project_file(workspace, {"path": "wiki/indexes/overview.md"}, ReadBudget())
    assert model_visible_read_result(complete)["next_offset"] is None

    directory = read_project_file(workspace, {"path": "wiki/papers"}, ReadBudget())
    assert model_visible_read_result(directory) == {
        "ok": True,
        "path": "wiki/papers",
        "entries": ["wiki/papers/paper-1.md"],
        "truncated": False,
        "next_offset": None,
    }

    source_pdf = workspace / "raw" / "papers" / "paper-1" / "source.pdf"
    source_pdf.write_bytes(b"%PDF-test")
    resource = read_project_file(workspace, {"path": "raw/papers/paper-1/source.pdf"}, ReadBudget())
    assert model_visible_read_result(resource) == {
        "ok": True,
        "path": "raw/papers/paper-1/source.pdf",
        "resource": {"kind": "pdf", "media_type": "application/pdf"},
    }

    error = read_project_file(workspace, {"path": "runs/private.json"}, ReadBudget())
    assert model_visible_read_result(error) == {
        "ok": False,
        "error": {
            "code": "PATH_OUTSIDE_ALLOWED_ROOTS",
            "message": "path must remain inside wiki/ or raw/papers/",
        },
    }


def test_qa_response_protocol_is_strict_json() -> None:
    parsed = parse_qa_model_response(_tool_call("call-1", "wiki/indexes/overview.md"))
    assert parsed.type == "tool_call"

    with pytest.raises(ValueError, match="valid JSON"):
        parse_qa_model_response("```json\n{}\n```")
    with pytest.raises(ValueError, match="valid QA envelope"):
        parse_qa_model_response('{"type":"tool_call","id":"x","name":"other","arguments":{}}')


def test_public_qa_api_runs_with_deterministic_mock_provider(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    result = run_qa(workspace, "这篇论文的方法是什么？", "session-mock")

    assert result["status"] == "completed"
    assert result["answer_status"] == "answered"
    assert result["cited_evidence_ids"] == ["paper-1:s0001"]
    assert result["read_budget"]["calls_used"] == 2


def test_qa_resumes_at_tool_boundary_without_duplicate_side_effects(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    initial_provider = ScriptedProvider([_tool_call("call-1", "wiki/papers/paper-1.md")])

    interrupted = run_qa(
        workspace,
        "方法？",
        "session-resume",
        provider=initial_provider,
        interrupt_before=["read_project_file"],
    )

    assert interrupted["status"] == "interrupted"
    assert interrupted["next_nodes"] == ["read_project_file"]
    assert interrupted["read_budget"]["calls_used"] == 0
    assert initial_provider.requests
    assert not (workspace / "runs" / interrupted["run_id"] / "tools").exists()

    final_provider = ScriptedProvider(
        [
            _final(
                answer="论文记录了方法。 [evidence:paper-1:s0001]",
                claims=[
                    {
                        "text": "论文记录了方法。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                cited_evidence_ids=["paper-1:s0001"],
            )
        ]
    )
    result = resume_qa(workspace, interrupted["thread_id"], provider=final_provider)

    assert result["status"] == "completed"
    assert result["read_budget"]["calls_used"] == 1
    assert len(final_provider.requests) == 1
    tools = list((workspace / "runs" / result["run_id"] / "tools").iterdir())
    assert len(tools) == 1
    events_path = workspace / "runs" / result["run_id"] / "events.jsonl"
    events = [WorkflowEvent.model_validate_json(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert [event.sequence for event in events] == list(range(len(events)))
    assert EventKind.RUN_INTERRUPTED in [event.event_type for event in events]
    assert EventKind.RUN_RESUMED in [event.event_type for event in events]

    event_count = len(events)
    no_call_provider = ScriptedProvider([])
    repeated = resume_qa(workspace, interrupted["thread_id"], provider=no_call_provider)
    assert repeated == result
    assert not no_call_provider.requests
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == event_count


def test_qa_transient_model_and_tool_failures_remain_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    failed_model = run_qa(
        workspace,
        "方法？",
        "session-model-failure",
        provider=ScriptedProvider([]),
    )
    assert failed_model["status"] == "interrupted"
    assert failed_model["retryable"] is True
    assert failed_model["next_nodes"] == ["qa_agent"]

    resumed_model = resume_qa(
        workspace,
        failed_model["thread_id"],
        provider=ScriptedProvider(
            [
                _tool_call("call-after-model-failure", "wiki/papers/paper-1.md"),
                _final(
                    answer="论文记录了方法。 [evidence:paper-1:s0001]",
                    claims=[
                        {
                            "text": "论文记录了方法。",
                            "type": "paper_fact",
                            "paper_ids": ["paper-1"],
                            "evidence_ids": ["paper-1:s0001"],
                        }
                    ],
                    cited_evidence_ids=["paper-1:s0001"],
                ),
            ]
        ),
    )
    assert resumed_model["status"] == "completed"

    real_read = read_project_file
    monkeypatch.setattr(
        "paperscout.qa.read_project_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("temporary read failure")),
    )
    failed_tool = run_qa(
        workspace,
        "方法？",
        "session-tool-failure",
        provider=ScriptedProvider([_tool_call("call-tool-failure", "wiki/papers/paper-1.md")]),
    )
    assert failed_tool["status"] == "interrupted"
    assert failed_tool["retryable"] is True
    assert failed_tool["next_nodes"] == ["read_project_file"]
    monkeypatch.setattr("paperscout.qa.read_project_file", real_read)

    resumed_tool = resume_qa(
        workspace,
        failed_tool["thread_id"],
        provider=ScriptedProvider(
            [
                _final(
                    answer="论文记录了方法。 [evidence:paper-1:s0001]",
                    claims=[
                        {
                            "text": "论文记录了方法。",
                            "type": "paper_fact",
                            "paper_ids": ["paper-1"],
                            "evidence_ids": ["paper-1:s0001"],
                        }
                    ],
                    cited_evidence_ids=["paper-1:s0001"],
                )
            ]
        ),
    )
    assert resumed_tool["status"] == "completed"
    assert resumed_tool["read_budget"]["calls_used"] == 1


def test_qa_resume_replays_durable_tool_result_without_second_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    call = AgentToolCall(
        id="call-journal",
        name="read_project_file",
        arguments={"path": "wiki/papers/paper-1.md"},
    )
    interrupted = run_qa(
        workspace,
        "方法？",
        "session-tool-journal",
        provider=ScriptedProvider([json.dumps({"type": "tool_call", **call.model_dump(mode="json")})]),
        interrupt_before=["read_project_file"],
    )
    outcome = read_project_file(workspace, call.arguments, ReadBudget())
    audit = workspace / "runs" / interrupted["run_id"] / "tools" / "001"
    audit.mkdir(parents=True)
    (audit / "request.json").write_text(
        json.dumps(call.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    (audit / "result.json").write_text(
        json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "paperscout.qa.read_project_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("tool must not run twice")),
    )
    final_provider = ScriptedProvider(
        [
            _final(
                answer="论文记录了方法。 [evidence:paper-1:s0001]",
                claims=[
                    {
                        "text": "论文记录了方法。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                cited_evidence_ids=["paper-1:s0001"],
            )
        ]
    )

    result = resume_qa(workspace, interrupted["thread_id"], provider=final_provider)

    assert result["status"] == "completed"
    assert result["read_budget"]["calls_used"] == 1


def test_qa_resume_replays_durable_model_output_without_second_call(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    interrupted = run_qa(
        workspace,
        "方法？",
        "session-model-journal",
        provider=ScriptedProvider([]),
        interrupt_before=["qa_agent"],
    )
    output_path = workspace / "runs" / interrupted["run_id"] / "qa-output-1.txt"
    output_path.write_text(
        _tool_call("call-from-journal", "wiki/papers/paper-1.md"),
        encoding="utf-8",
    )
    final_provider = ScriptedProvider(
        [
            _final(
                answer="论文记录了方法。 [evidence:paper-1:s0001]",
                claims=[
                    {
                        "text": "论文记录了方法。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                cited_evidence_ids=["paper-1:s0001"],
            )
        ]
    )

    result = resume_qa(workspace, interrupted["thread_id"], provider=final_provider)

    assert result["status"] == "completed"
    assert len(final_provider.requests) == 1


def test_qa_answer_contract_rejects_unowned_cross_paper_evidence() -> None:
    with pytest.raises(ValidationError):
        QAAnswer.model_validate(
            {
                "answer": "综合结论",
                "claims": [
                    {
                        "text": "两篇论文采用相似流程。",
                        "type": "cross_paper_synthesis",
                        "paper_ids": ["paper-1", "paper-2"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                "cited_evidence_ids": ["paper-1:s0001"],
                "status": "answered",
                "memory_patch": {},
            }
        )


def test_qa_graph_progressively_reads_wiki_evidence_and_raw(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    protected_files = {
        path: path.read_bytes()
        for root in (workspace / "wiki", workspace / "raw")
        for path in root.rglob("*")
        if path.is_file()
    }
    provider = ScriptedProvider(
        [
            _tool_call("call-1", "wiki/indexes/overview.md"),
            _tool_call("call-2", "wiki/papers/paper-1.md"),
            _tool_call("call-3", "wiki/evidence/paper-1/s0001.md"),
            _tool_call("call-4", "raw/papers/paper-1/mineru/full.md"),
            _final(
                answer="论文描述了可核查的处理流程。 [paper:paper-1] [evidence:paper-1:s0001]",
                claims=[
                    {
                        "text": "论文描述了可核查的处理流程。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                cited_evidence_ids=["paper-1:s0001"],
            ),
        ]
    )

    result = run_qa(workspace, "这篇论文的方法是什么？", "session-1", provider=provider)

    assert result["status"] == "completed"
    assert result["answer_status"] == "answered"
    assert result["cited_evidence_ids"] == ["paper-1:s0001"]
    assert result["read_budget"]["calls_used"] == 4
    assert {path: path.read_bytes() for path in protected_files} == protected_files
    assert [record["path"] for record in result["read_resources"]] == [
        "wiki/indexes/overview.md",
        "wiki/papers/paper-1.md",
        "wiki/evidence/paper-1/s0001.md",
        "raw/papers/paper-1/mineru/full.md",
    ]
    assert all("sha256" not in request[-1].get("content", {}) for request in provider.requests[1:])
    events = (workspace / "runs" / result["run_id"] / "events.jsonl").read_text(encoding="utf-8")
    assert events.count('"event_type":"tool.started"') == 4
    assert events.count('"event_type":"tool.completed"') == 4
    assert (workspace / "runtime" / "checkpoints.sqlite").is_file()
    tool_audit = json.loads(
        (workspace / "runs" / result["run_id"] / "tools" / "002" / "result.json").read_text(
            encoding="utf-8"
        )
    )
    assert tool_audit["result"]["sha256"]
    assert tool_audit["budget"]["calls_used"] == 2
    with GraphRuntime.open(workspace) as runtime:
        graph = build_qa_graph(runtime, provider=ScriptedProvider([]))
        snapshot = graph.get_state(graph_config(result["thread_id"]))
    assert snapshot.values["status"] == "completed"
    assert snapshot.values["current_node"] == "complete_qa"


def test_invalid_tool_arguments_are_returned_to_model_and_can_be_corrected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    provider = ScriptedProvider(
        [
            _tool_call("bad", "wiki/papers/paper-1.md", max_chars=50_001),
            _tool_call("fixed", "wiki/papers/paper-1.md"),
            _final(
                answer="已根据论文 Wiki 回答。 [evidence:paper-1:s0001]",
                claims=[
                    {
                        "text": "论文 Wiki 记录了方法。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                cited_evidence_ids=["paper-1:s0001"],
            ),
        ]
    )

    result = run_qa(workspace, "方法？", "session-correct", provider=provider)

    assert result["status"] == "completed"
    assert result["read_budget"]["calls_used"] == 1
    first_tool_result = provider.requests[1][-1]["content"]
    assert first_tool_result["ok"] is False
    assert first_tool_result["error"]["code"] == "INVALID_ARGUMENTS"


def test_budget_exhaustion_can_end_with_insufficient_evidence(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    provider = ScriptedProvider(
        [
            _tool_call("call-1", "wiki/indexes/overview.md"),
            _tool_call("call-2", "wiki/papers/paper-1.md"),
            _final(
                answer="读取预算已耗尽，现有资料不足以核查具体方法。",
                claims=[],
                cited_evidence_ids=[],
                status="insufficient_evidence",
            ),
        ]
    )

    result = run_qa(
        workspace,
        "具体方法？",
        "session-budget",
        provider=provider,
        read_budget=ReadBudget(max_calls=1),
    )

    assert result["status"] == "completed"
    assert result["answer_status"] == "insufficient_evidence"
    assert result["read_budget"]["calls_used"] == 1
    exhausted_result = provider.requests[2][-1]["content"]
    assert exhausted_result["error"]["code"] == "BUDGET_EXCEEDED"


@pytest.mark.parametrize(
    "response,error",
    [
        ("not json", "valid JSON"),
        (
            _final(
                answer="伪造引用",
                claims=[
                    {
                        "text": "未读取的事实。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s9999"],
                    }
                ],
                cited_evidence_ids=["paper-1:s9999"],
            ),
            "not observed in tool results",
        ),
    ],
)
def test_malformed_or_unread_final_answer_fails_closed(
    tmp_path: Path, response: str, error: str
) -> None:
    workspace = _workspace(tmp_path)
    provider = ScriptedProvider([response])

    result = run_qa(workspace, "问题", "session-fail", provider=provider)

    assert result["status"] == "failed"
    assert error in result["error"]
    assert (workspace / "runs" / result["run_id"] / "result.json").is_file()


def test_completed_qa_persists_user_readable_session_without_tool_text_in_state(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    provider = ScriptedProvider(
        [
            _tool_call("call-session", "wiki/papers/paper-1.md"),
            _final(
                answer="论文记录了方法。 [evidence:paper-1:s0001]",
                claims=[
                    {
                        "text": "论文记录了方法。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                cited_evidence_ids=["paper-1:s0001"],
            ),
        ]
    )

    result = run_qa(workspace, "方法？", "session-files", provider=provider)

    assert result["status"] == "completed"
    session_dir = workspace / "memory" / "sessions" / "session-files"
    state_payload = json.loads((session_dir / "state.json").read_text(encoding="utf-8"))
    state = SessionState.model_validate(state_payload)
    messages = [
        SessionMessage.model_validate_json(line)
        for line in (session_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [message.role for message in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "system",
    ]
    assert isinstance(messages[-1].content, dict)
    assert messages[-1].content["type"] == "answer_review"
    assert messages[-1].content["verdict"] == "APPROVE"
    assert "该方法使用可核查的处理流程" in json.dumps(
        messages[2].content, ensure_ascii=False
    )
    assert state.session_id == "session-files"
    assert state.messages == []
    assert state.memory.evidence_ids == ["paper-1:s0001"]
    assert [record.path for record in state.read_resources] == ["wiki/papers/paper-1.md"]
    assert "messages" not in state_payload
    assert "该方法使用可核查的处理流程" not in json.dumps(
        state_payload, ensure_ascii=False
    )
    assert (session_dir / "summary.md").read_text(encoding="utf-8") == ""


def test_same_session_continues_with_history_memory_and_reused_tool_call_ids(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    first = run_qa(
        workspace,
        "先读论文 Wiki。",
        "session-continue",
        provider=ScriptedProvider(
            [
                _tool_call("shared-call", "wiki/papers/paper-1.md"),
                _final(
                    answer="已读取论文方法。 [evidence:paper-1:s0001]",
                    claims=[
                        {
                            "text": "论文记录了方法。",
                            "type": "paper_fact",
                            "paper_ids": ["paper-1"],
                            "evidence_ids": ["paper-1:s0001"],
                        }
                    ],
                    cited_evidence_ids=["paper-1:s0001"],
                    memory_patch={
                        "research_goal": "核对论文方法",
                        "paper_aliases": {"第一篇": "paper-1"},
                        "unresolved_questions": ["方法如何实现？"],
                        "evidence_ids": ["paper-1:s0001"],
                    },
                ),
            ]
        ),
    )
    second_provider = ScriptedProvider(
        [
            _tool_call("shared-call", "wiki/evidence/paper-1/s0001.md"),
            _final(
                answer="原文证据支持该方法。 [evidence:paper-1:s0001]",
                claims=[
                    {
                        "text": "原文证据支持该方法。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                cited_evidence_ids=["paper-1:s0001"],
                memory_patch={
                    "paper_aliases": {"这篇论文": "paper-1"},
                    "confirmed_decisions": ["继续核对原文"],
                    "unresolved_questions": ["方法如何实现？", "实验如何验证？"],
                    "evidence_ids": ["paper-1:s0001"],
                },
            ),
        ]
    )

    second = run_qa(
        workspace,
        "再核对原文证据。",
        "session-continue",
        provider=second_provider,
    )

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    first_request = second_provider.requests[0]
    assert any(message.get("content") == "先读论文 Wiki。" for message in first_request)
    assert first_request[-1]["content"] == "再核对原文证据。"
    assert "paper-1:s0001" in first_request[1]["content"]
    session_dir = workspace / "memory" / "sessions" / "session-continue"
    messages = (session_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()
    state = SessionState.model_validate_json(
        (session_dir / "state.json").read_text(encoding="utf-8")
    )
    assert len(messages) == 10
    assert state.memory.research_goal == "核对论文方法"
    assert state.memory.paper_aliases == {"第一篇": "paper-1", "这篇论文": "paper-1"}
    assert state.memory.confirmed_decisions == ["继续核对原文"]
    assert state.memory.unresolved_questions == ["方法如何实现？", "实验如何验证？"]
    assert state.memory.evidence_ids == ["paper-1:s0001"]
    assert [record.path for record in state.read_resources] == [
        "wiki/papers/paper-1.md",
        "wiki/evidence/paper-1/s0001.md",
    ]


def test_session_id_cannot_escape_the_session_root(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    with pytest.raises(ValueError, match="session_id"):
        run_qa(workspace, "问题", "../outside", provider=ScriptedProvider([]))

    assert not (workspace / "memory" / "outside").exists()


def test_qa_session_persistence_failure_resumes_without_repeating_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    provider = ScriptedProvider(
        [
            _final(
                answer="当前资料不足。",
                claims=[],
                cited_evidence_ids=[],
                status="insufficient_evidence",
            )
        ]
    )
    persist_then_fail_calls = 0

    def persist_then_fail(*args: Any, **kwargs: Any) -> Any:
        nonlocal persist_then_fail_calls
        persist_then_fail_calls += 1
        persist_session(*args, **kwargs)
        raise OSError("temporary session failure")

    with monkeypatch.context() as patcher:
        patcher.setattr("paperscout.qa.persist_session", persist_then_fail)
        interrupted = run_qa(
            workspace,
            "问题",
            "session-persist-resume",
            provider=provider,
        )

    assert interrupted["status"] == "interrupted"
    assert interrupted["next_nodes"] == ["complete_qa"]
    assert "temporary session failure" in interrupted["error"]
    assert len(provider.requests) == 1
    assert persist_then_fail_calls == 1

    no_call_provider = ScriptedProvider([])
    result = resume_qa(
        workspace,
        interrupted["thread_id"],
        provider=no_call_provider,
    )

    assert result["status"] == "completed"
    assert not no_call_provider.requests
    session_dir = workspace / "memory" / "sessions" / "session-persist-resume"
    assert (session_dir / "state.json").is_file()
    assert (session_dir / "messages.jsonl").is_file()
    assert (session_dir / "summary.md").is_file()
    assert len((session_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()) == 3


def test_session_context_keeps_all_history_below_dynamic_threshold(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    for turn in range(1, 6):
        result = run_qa(
            workspace,
            f"短问题{turn}",
            "session-below-threshold",
            provider=ScriptedProvider(
                [
                    _final(
                        answer="简短回答。",
                        claims=[],
                        cited_evidence_ids=[],
                        status="insufficient_evidence",
                    )
                ]
            ),
        )
        assert result["status"] == "completed"

    sixth_provider = ScriptedProvider(
        [
            _final(
                answer="简短回答。",
                claims=[],
                cited_evidence_ids=[],
                status="insufficient_evidence",
            )
        ]
    )
    sixth = run_qa(
        workspace,
        "短问题6",
        "session-below-threshold",
        provider=sixth_provider,
    )

    assert sixth["status"] == "completed"
    retained_user_messages = [
        message["content"]
        for message in sixth_provider.requests[0][2:]
        if message["role"] == "user"
    ]
    assert retained_user_messages == [f"短问题{turn}" for turn in range(1, 7)]
    session_dir = workspace / "memory" / "sessions" / "session-below-threshold"
    state = SessionState.model_validate_json(
        (session_dir / "state.json").read_text(encoding="utf-8")
    )
    assert state.compacted_turns == 0
    assert state.summary == ""
    events = [
        WorkflowEvent.model_validate_json(line)
        for line in (workspace / "runs" / sixth["run_id"] / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert EventKind.CONTEXT_COMPACTED not in [event.event_type for event in events]


def test_session_context_compacts_at_dynamic_threshold_and_keeps_four_turns(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    old_tool_text = "FIRST_TOOL_BODY_SHOULD_NOT_REENTER_CONTEXT"
    paper_path = workspace / "wiki" / "papers" / "paper-1.md"
    paper_path.write_text(
        paper_path.read_text(encoding="utf-8") + f"\n{old_tool_text}\n",
        encoding="utf-8",
    )
    first = run_qa(
        workspace,
        "问题一",
        "session-compact",
        provider=ScriptedProvider(
            [
                _tool_call("old-tool", "wiki/papers/paper-1.md"),
                _final(
                    answer="论文记录了方法。 [evidence:paper-1:s0001]",
                    claims=[
                        {
                            "text": "论文记录了方法。",
                            "type": "paper_fact",
                            "paper_ids": ["paper-1"],
                            "evidence_ids": ["paper-1:s0001"],
                        }
                    ],
                    cited_evidence_ids=["paper-1:s0001"],
                    memory_patch={
                        "research_goal": "比较论文方法",
                        "paper_aliases": {"目标论文": "paper-1"},
                        "unresolved_questions": ["方法差异是什么？"],
                        "evidence_ids": ["paper-1:s0001"],
                    },
                ),
            ]
        ),
    )
    assert first["status"] == "completed"
    for turn in range(2, 6):
        result = run_qa(
            workspace,
            f"问题{turn}",
            "session-compact",
            provider=ScriptedProvider(
                [
                    _final(
                        answer="当前资料不足。",
                        claims=[],
                        cited_evidence_ids=[],
                        status="insufficient_evidence",
                    )
                ]
            ),
        )
        assert result["status"] == "completed"

    sixth_provider = ScriptedProvider(
        [
            _final(
                answer="当前资料不足。",
                claims=[],
                cited_evidence_ids=[],
                status="insufficient_evidence",
            )
        ],
        qa_context_window=1_024,
    )
    sixth = run_qa(
        workspace,
        "问题6",
        "session-compact",
        provider=sixth_provider,
    )

    assert sixth["status"] == "completed"
    request = sixth_provider.requests[0]
    retained_user_messages = [
        message["content"]
        for message in request[2:]
        if message["role"] == "user"
    ]
    assert retained_user_messages == ["问题2", "问题3", "问题4", "问题5", "问题6"]
    assert old_tool_text not in json.dumps(request, ensure_ascii=False)
    history_summary = request[1]["content"]
    assert "问题一" in history_summary
    assert "比较论文方法" in history_summary
    assert "目标论文" in history_summary
    assert "方法差异是什么？" in history_summary
    assert "wiki/papers/paper-1.md" in history_summary
    assert "paper-1:s0001" in history_summary
    assert first["read_resources"][0]["sha256"] in history_summary

    session_dir = workspace / "memory" / "sessions" / "session-compact"
    message_log = (session_dir / "messages.jsonl").read_text(encoding="utf-8")
    summary = (session_dir / "summary.md").read_text(encoding="utf-8")
    state = SessionState.model_validate_json(
        (session_dir / "state.json").read_text(encoding="utf-8")
    )
    assert len(message_log.splitlines()) == 20
    assert old_tool_text in message_log
    assert old_tool_text not in summary
    assert "问题一" in summary
    assert "问题2" not in summary
    assert state.summary == summary
    assert state.compacted_turns == 1
    events = [
        WorkflowEvent.model_validate_json(line)
        for line in (workspace / "runs" / sixth["run_id"] / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    compacted_events = [
        event for event in events if event.event_type == EventKind.CONTEXT_COMPACTED
    ]
    assert len(compacted_events) == 1
    assert compacted_events[0].data["context_window"] == 1_024
    assert compacted_events[0].data["threshold_tokens"] == 614
    assert compacted_events[0].data["compacted_turns"] == 1
    assert compacted_events[0].data["retained_history_turns"] == 4

    larger_window_provider = ScriptedProvider(
        [
            _final(
                answer="当前资料不足。",
                claims=[],
                cited_evidence_ids=[],
                status="insufficient_evidence",
            )
        ]
    )
    seventh = run_qa(
        workspace,
        "问题7",
        "session-compact",
        provider=larger_window_provider,
    )

    assert seventh["status"] == "completed"
    retained_after_window_growth = [
        message["content"]
        for message in larger_window_provider.requests[0][2:]
        if message["role"] == "user"
    ]
    assert retained_after_window_growth == [
        "问题2",
        "问题3",
        "问题4",
        "问题5",
        "问题6",
        "问题7",
    ]
    assert old_tool_text not in json.dumps(
        larger_window_provider.requests[0], ensure_ascii=False
    )
    persisted_after_window_growth = SessionState.model_validate_json(
        (session_dir / "state.json").read_text(encoding="utf-8")
    )
    assert persisted_after_window_growth.compacted_turns == 1


def test_changed_session_resource_is_invalidated_and_must_be_read_again(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    paper_path = workspace / "wiki" / "papers" / "paper-1.md"
    old_tool_text = "OLD_RESOURCE_BODY_MUST_NOT_BE_REUSED"
    paper_path.write_text(
        paper_path.read_text(encoding="utf-8") + f"\n{old_tool_text}\n",
        encoding="utf-8",
    )
    first = run_qa(
        workspace,
        "先读取论文。",
        "session-stale-resource",
        provider=ScriptedProvider(
            [
                _tool_call("first-read", "wiki/papers/paper-1.md"),
                _final(
                    answer="论文记录了方法。 [evidence:paper-1:s0001]",
                    claims=[
                        {
                            "text": "论文记录了方法。",
                            "type": "paper_fact",
                            "paper_ids": ["paper-1"],
                            "evidence_ids": ["paper-1:s0001"],
                        }
                    ],
                    cited_evidence_ids=["paper-1:s0001"],
                ),
            ]
        ),
    )
    old_hash = first["read_resources"][0]["sha256"]
    new_tool_text = "NEW_RESOURCE_BODY_AFTER_REBUILD"
    paper_path.write_text(
        "# Test Paper\n\n## 方法\n\n更新后的方法。\n\n"
        f"[evidence:paper-1:s0001]\n\n{new_tool_text}\n",
        encoding="utf-8",
    )
    second_provider = ScriptedProvider(
        [
            _tool_call("second-read", "wiki/papers/paper-1.md"),
            _final(
                answer="更新后的 Wiki 仍记录该方法。 [evidence:paper-1:s0001]",
                claims=[
                    {
                        "text": "更新后的 Wiki 仍记录该方法。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                cited_evidence_ids=["paper-1:s0001"],
            ),
        ]
    )

    second = run_qa(
        workspace,
        "文件更新后再核对。",
        "session-stale-resource",
        provider=second_provider,
    )

    assert second["status"] == "completed"
    initial_request = second_provider.requests[0]
    assert old_tool_text not in json.dumps(initial_request, ensure_ascii=False)
    assert "wiki/papers/paper-1.md" in initial_request[1]["content"]
    assert "必须重新读取" in initial_request[1]["content"]
    assert '"evidence_ids": []' in initial_request[1]["content"]
    historical_tools = [
        message for message in initial_request[2:] if message["role"] == "tool"
    ]
    assert historical_tools == [
        {
            "role": "tool",
            "content": {
                "ok": False,
                "error": {
                    "code": "STALE_SESSION_RESOURCE",
                    "message": "wiki/papers/paper-1.md changed or is unavailable; read it again",
                },
            },
            "tool_call_id": "first-read",
        }
    ]
    session_dir = workspace / "memory" / "sessions" / "session-stale-resource"
    state = SessionState.model_validate_json(
        (session_dir / "state.json").read_text(encoding="utf-8")
    )
    assert len(state.read_resources) == 1
    assert state.read_resources[0].sha256 != old_hash
    assert state.memory.evidence_ids == ["paper-1:s0001"]
    message_log = (session_dir / "messages.jsonl").read_text(encoding="utf-8")
    assert old_tool_text in message_log
    assert new_tool_text in message_log


def test_stale_resource_keeps_evidence_memory_backed_by_an_unchanged_resource(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    first = run_qa(
        workspace,
        "读取 Wiki 和原文证据。",
        "session-partial-stale",
        provider=ScriptedProvider(
            [
                _tool_call("wiki-read", "wiki/papers/paper-1.md"),
                _tool_call("evidence-read", "wiki/evidence/paper-1/s0001.md"),
                _final(
                    answer="两份资料均指向该方法。 [evidence:paper-1:s0001]",
                    claims=[
                        {
                            "text": "两份资料均指向该方法。",
                            "type": "paper_fact",
                            "paper_ids": ["paper-1"],
                            "evidence_ids": ["paper-1:s0001"],
                        }
                    ],
                    cited_evidence_ids=["paper-1:s0001"],
                ),
            ]
        ),
    )
    assert first["status"] == "completed"
    (workspace / "wiki" / "papers" / "paper-1.md").write_text(
        "# Rebuilt Wiki\n\n[evidence:paper-1:s0001]\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(
        [
            _final(
                answer="本轮未重新读取，不能返回论文事实。",
                claims=[],
                cited_evidence_ids=[],
                status="insufficient_evidence",
            )
        ]
    )

    second = run_qa(
        workspace,
        "哪些已读资源仍有效？",
        "session-partial-stale",
        provider=provider,
    )

    assert second["status"] == "completed"
    context_prompt = provider.requests[0][1]["content"]
    assert '"evidence_ids": ["paper-1:s0001"]' in context_prompt
    historical_tools = [
        message for message in provider.requests[0][2:] if message["role"] == "tool"
    ]
    assert historical_tools[0]["content"]["error"]["code"] == "STALE_SESSION_RESOURCE"
    assert historical_tools[1]["content"]["path"] == "wiki/evidence/paper-1/s0001.md"
    session_dir = workspace / "memory" / "sessions" / "session-partial-stale"
    state = SessionState.model_validate_json(
        (session_dir / "state.json").read_text(encoding="utf-8")
    )
    assert [record.path for record in state.read_resources] == [
        "wiki/evidence/paper-1/s0001.md"
    ]
    assert state.memory.evidence_ids == ["paper-1:s0001"]


def test_project_memory_is_shared_across_sessions_and_isolated_by_project(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    first_question = "记录 Alpha 项目的长期研究方向。"
    first = run_qa(
        workspace,
        first_question,
        "project-session-a",
        project_id="project-alpha",
        provider=ScriptedProvider(
            [
                _final(
                    answer="已记录项目方向。",
                    claims=[],
                    cited_evidence_ids=[],
                    memory_patch={
                        "research_goal": "比较检索增强方法",
                        "paper_aliases": {"基线论文": "paper-1"},
                        "confirmed_decisions": ["先比较检索模块"],
                        "unresolved_questions": ["生成器是否需要微调？"],
                        "research_hypotheses": ["稀疏与稠密检索可以互补"],
                        "evidence_ids": [],
                    },
                )
            ]
        ),
    )

    assert first["status"] == "completed"
    assert first["project_id"] == "project-alpha"
    project_path = workspace / "memory" / "projects" / "project-alpha" / "state.json"
    project_payload = json.loads(project_path.read_text(encoding="utf-8"))
    project = ProjectState.model_validate(project_payload)
    assert project.project_id == "project-alpha"
    assert project.memory.research_goal == "比较检索增强方法"
    assert project.memory.confirmed_decisions == ["先比较检索模块"]
    assert "messages" not in project_payload
    assert "read_resources" not in project_payload

    shared_provider = ScriptedProvider(
        [
            _final(
                answer="已加载共享项目记忆。",
                claims=[],
                cited_evidence_ids=[],
            )
        ]
    )
    shared = run_qa(
        workspace,
        "继续 Alpha 项目。",
        "project-session-b",
        project_id="project-alpha",
        provider=shared_provider,
    )

    assert shared["status"] == "completed"
    shared_request = json.dumps(shared_provider.requests[0], ensure_ascii=False)
    assert "当前项目 ID：project-alpha" in shared_request
    assert "比较检索增强方法" in shared_request
    assert "先比较检索模块" in shared_request
    assert first_question not in shared_request

    default_provider = ScriptedProvider(
        [
            _final(
                answer="默认项目没有 Alpha 记忆。",
                claims=[],
                cited_evidence_ids=[],
            )
        ]
    )
    default = run_qa(
        workspace,
        "检查默认项目。",
        "default-project-session",
        provider=default_provider,
    )

    assert default["status"] == "completed"
    assert default["project_id"] == "default"
    assert "比较检索增强方法" not in json.dumps(
        default_provider.requests[0], ensure_ascii=False
    )
    assert (workspace / "memory" / "projects" / "default" / "state.json").is_file()


def test_project_id_is_safe_and_session_cannot_change_projects(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    with pytest.raises(ValueError, match="project_id"):
        run_qa(
            workspace,
            "问题",
            "safe-session",
            project_id="../outside",
            provider=ScriptedProvider([]),
        )
    assert not (workspace / "memory" / "outside").exists()

    run_qa(
        workspace,
        "建立项目绑定。",
        "bound-session",
        project_id="project-a",
        provider=ScriptedProvider(
            [
                _final(
                    answer="已建立绑定。",
                    claims=[],
                    cited_evidence_ids=[],
                )
            ]
        ),
    )
    with pytest.raises(ValueError, match="project_id"):
        run_qa(
            workspace,
            "不能切换项目。",
            "bound-session",
            project_id="project-b",
            provider=ScriptedProvider([]),
        )


def test_project_memory_persistence_failure_resumes_without_repeating_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    provider = ScriptedProvider(
        [
            _final(
                answer="已记录项目决策。",
                claims=[],
                cited_evidence_ids=[],
                memory_patch={
                    "confirmed_decisions": ["保留可追溯引用"],
                },
            )
        ]
    )
    persist_then_fail_calls = 0

    def persist_then_fail(*args: Any, **kwargs: Any) -> Any:
        nonlocal persist_then_fail_calls
        persist_then_fail_calls += 1
        persist_project_memory(*args, **kwargs)
        raise OSError("temporary project memory failure")

    with monkeypatch.context() as patcher:
        patcher.setattr("paperscout.qa.persist_project_memory", persist_then_fail)
        interrupted = run_qa(
            workspace,
            "记录项目决策。",
            "project-persist-session",
            project_id="recovery-project",
            provider=provider,
        )

    assert interrupted["status"] == "interrupted"
    assert interrupted["next_nodes"] == ["complete_qa"]
    assert "temporary project memory failure" in interrupted["error"]
    assert len(provider.requests) == 1
    assert persist_then_fail_calls == 1

    result = resume_qa(
        workspace,
        interrupted["thread_id"],
        provider=ScriptedProvider([]),
    )

    assert result["status"] == "completed"
    assert result["project_id"] == "recovery-project"
    project = ProjectState.model_validate_json(
        (
            workspace
            / "memory"
            / "projects"
            / "recovery-project"
            / "state.json"
        ).read_text(encoding="utf-8")
    )
    assert project.memory.confirmed_decisions == ["保留可追溯引用"]
    messages = (
        workspace
        / "memory"
        / "sessions"
        / "project-persist-session"
        / "messages.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(messages) == 3


def test_user_profile_is_loaded_across_projects_and_remains_read_only_for_qa(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    profile = save_user_profile(
        workspace,
        UserProfile(
            research_directions=["检索增强生成", "学术问答"],
            answer_style_preferences=["先给结论，再给证据"],
            citation_preferences=["每项论文事实都附 Evidence ID"],
        ),
    )
    profile_path = workspace / "memory" / "profile.json"
    original_profile = profile_path.read_bytes()

    providers: list[ScriptedProvider] = []
    for project_id in ("profile-project-a", "profile-project-b"):
        provider = ScriptedProvider(
            [
                _final(
                    answer="已按用户偏好回答。",
                    claims=[],
                    cited_evidence_ids=[],
                )
            ]
        )
        providers.append(provider)
        result = run_qa(
            workspace,
            "按我的偏好回答。",
            f"{project_id}-session",
            project_id=project_id,
            provider=provider,
        )
        assert result["status"] == "completed"

    for provider in providers:
        context = json.dumps(provider.requests[0], ensure_ascii=False)
        assert "检索增强生成" in context
        assert "先给结论，再给证据" in context
        assert "每项论文事实都附 Evidence ID" in context
        assert "只读" in context
    assert profile_path.read_bytes() == original_profile
    assert UserProfile.model_validate_json(profile_path.read_text(encoding="utf-8")) == profile


def test_qa_rejects_profile_patch_and_preserves_user_profile(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    save_user_profile(
        workspace,
        UserProfile(answer_style_preferences=["简洁回答"]),
    )
    profile_path = workspace / "memory" / "profile.json"
    original_profile = profile_path.read_bytes()
    invalid_final = json.loads(
        _final(
            answer="不能修改 Profile。",
            claims=[],
            cited_evidence_ids=[],
        )
    )
    invalid_final["profile_patch"] = {"answer_style_preferences": ["详细回答"]}

    result = run_qa(
        workspace,
        "尝试修改偏好。",
        "profile-patch-session",
        provider=ScriptedProvider([json.dumps(invalid_final, ensure_ascii=False)]),
    )

    assert result["status"] == "failed"
    assert "valid QA envelope" in result["error"]
    assert profile_path.read_bytes() == original_profile


def test_missing_user_profile_is_empty_non_mutating_and_invalid_profile_fails(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    provider = ScriptedProvider(
        [
            _final(
                answer="当前没有全局偏好。",
                claims=[],
                cited_evidence_ids=[],
            )
        ]
    )

    result = run_qa(
        workspace,
        "读取默认偏好。",
        "missing-profile-session",
        provider=provider,
    )

    assert result["status"] == "completed"
    assert '"answer_style_preferences": []' in provider.requests[0][1]["content"]
    profile_path = workspace / "memory" / "profile.json"
    assert not profile_path.exists()

    profile_path.write_text('{"unknown": true}\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        run_qa(
            workspace,
            "损坏的 Profile 必须失败。",
            "invalid-profile-session",
            provider=ScriptedProvider([]),
        )


def test_answer_evidence_change_after_rules_fails_before_session_persistence(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    provider = ScriptedProvider(
        [
            _tool_call("answer-rule-read", "wiki/papers/paper-1.md"),
            _final(
                answer="论文记录了方法。 [evidence:paper-1:s0001]",
                claims=[
                    {
                        "text": "论文记录了方法。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                cited_evidence_ids=["paper-1:s0001"],
            ),
        ]
    )

    interrupted = run_qa(
        workspace,
        "论文的方法是什么？",
        "answer-rule-hash-session",
        provider=provider,
        interrupt_before=["verify_answer_evidence"],
    )

    assert interrupted["status"] == "interrupted"
    assert interrupted["next_nodes"] == ["verify_answer_evidence"]
    evidence_path = workspace / "wiki" / "evidence" / "paper-1" / "s0001.md"
    evidence_path.write_text(
        evidence_path.read_text(encoding="utf-8") + "\n审核后发生变化。\n",
        encoding="utf-8",
    )

    result = resume_qa(
        workspace,
        interrupted["thread_id"],
        provider=ScriptedProvider([]),
    )

    assert result["status"] == "failed"
    assert "changed after deterministic review" in result["error"]
    assert not (
        workspace / "memory" / "sessions" / "answer-rule-hash-session"
    ).exists()


def test_semantic_answer_review_approve_is_isolated_and_audited(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    qa_provider = ScriptedProvider(
        [
            _tool_call("answer-review-read", "wiki/papers/paper-1.md"),
            _final(
                answer="论文记录了方法。 [evidence:paper-1:s0001]",
                claims=[
                    {
                        "text": "论文记录了方法。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                cited_evidence_ids=["paper-1:s0001"],
            ),
        ]
    )
    review_provider = ScriptedProvider(
        ["VERDICT: APPROVE\n\n未发现需要修改的问题。"]
    )

    result = run_qa(
        workspace,
        "论文的方法是什么？",
        "semantic-answer-review-session",
        provider=qa_provider,
        review_provider=review_provider,
    )

    assert result["status"] == "completed"
    assert result["review"] == {
        "verdict": "APPROVE",
        "feedback": ["未发现需要修改的问题。"],
    }
    assert len(qa_provider.requests) == 2
    assert len(review_provider.requests) == 1
    assert [message["role"] for message in review_provider.requests[0]] == ["system", "user"]
    review_request = review_provider.requests[0][1]["content"]
    assert "论文的方法是什么？" in review_request
    assert "论文记录了方法。 [evidence:paper-1:s0001]" in review_request
    assert "原文方法描述。" in review_request
    audit = workspace / "runs" / result["run_id"] / "review" / "answer" / "1"
    assert (audit / "request.md").read_text(encoding="utf-8") == review_request
    assert (audit / "response.md").read_text(encoding="utf-8").startswith(
        "VERDICT: APPROVE"
    )
    assert json.loads((audit / "result.json").read_text(encoding="utf-8")) == {
        "review_type": "answer",
        "attempt": 1,
        "evidence_ids": ["paper-1:s0001"],
        "decision": {
            "verdict": "APPROVE",
            "feedback": ["未发现需要修改的问题。"],
        },
    }
    events = [
        json.loads(line)
        for line in (workspace / "runs" / result["run_id"] / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    semantic_review_events = [
        event
        for event in events
        if event["node"] == "answer_review"
    ]
    assert [event["event_type"] for event in semantic_review_events] == [
        "review.started",
        "review.completed",
    ]
    assert {event["agent"] for event in semantic_review_events} == {"answer_review"}


def test_semantic_answer_review_invalid_verdict_fails_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    qa_provider = ScriptedProvider(
        [
            _tool_call("answer-review-invalid-read", "wiki/papers/paper-1.md"),
            _final(
                answer="论文记录了方法。 [evidence:paper-1:s0001]",
                claims=[
                    {
                        "text": "论文记录了方法。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                cited_evidence_ids=["paper-1:s0001"],
            ),
        ]
    )

    result = run_qa(
        workspace,
        "方法？",
        "semantic-answer-review-invalid-session",
        provider=qa_provider,
        review_provider=ScriptedProvider(["可以返回。"]),
    )

    assert result["status"] == "failed"
    assert "first line must be VERDICT" in result["error"]
    assert not (
        workspace / "memory" / "sessions" / "semantic-answer-review-invalid-session"
    ).exists()
    audit = workspace / "runs" / result["run_id"] / "review" / "answer" / "1"
    failed = json.loads((audit / "result.json").read_text(encoding="utf-8"))
    assert failed["status"] == "failed"


def test_semantic_answer_review_revise_regenerates_and_rechecks_full_answer(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    qa_provider = ScriptedProvider(
        [
            _tool_call("answer-review-revise-read", "wiki/papers/paper-1.md"),
            _final(
                answer="论文方法适用于所有任务。 [evidence:paper-1:s0001]",
                claims=[
                    {
                        "text": "论文方法适用于所有任务。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                cited_evidence_ids=["paper-1:s0001"],
            ),
            _final(
                answer="论文记录了一个可核查的方法流程。 [evidence:paper-1:s0001]",
                claims=[
                    {
                        "text": "论文记录了一个可核查的方法流程。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                cited_evidence_ids=["paper-1:s0001"],
            ),
        ]
    )
    review_provider = ScriptedProvider(
        [
            "VERDICT: REVISE\n\n适用范围表述过强，请缩小结论。",
            "VERDICT: APPROVE\n\n修订后的回答得到证据支持。",
        ]
    )

    result = run_qa(
        workspace,
        "方法？",
        "semantic-answer-review-revise-session",
        provider=qa_provider,
        review_provider=review_provider,
    )

    assert result["status"] == "completed"
    assert result["answer"] == "论文记录了一个可核查的方法流程。 [evidence:paper-1:s0001]"
    assert result["review"]["verdict"] == "APPROVE"
    assert result["review_attempts"] == 2
    assert result["safe_fallback"] is False
    assert len(qa_provider.requests) == 3
    assert len(review_provider.requests) == 2
    feedback_messages = [
        message
        for message in qa_provider.requests[2]
        if message["role"] == "system"
        and isinstance(message["content"], dict)
        and message["content"].get("type") == "answer_review"
    ]
    assert feedback_messages[-1]["content"]["verdict"] == "REVISE"
    assert "缩小结论" in feedback_messages[-1]["content"]["feedback"][0]
    review_root = workspace / "runs" / result["run_id"] / "review" / "answer"
    assert (review_root / "1" / "result.json").is_file()
    assert (review_root / "2" / "result.json").is_file()


def test_rejected_answer_drafts_remain_in_audit_but_not_next_model_context(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    rejected_text = "被拒绝的过强结论"
    review_feedback = "该结论超出 Evidence 支持范围"
    approved_text = "最终批准的受支持结论"
    first = run_qa(
        workspace,
        "方法？",
        "semantic-answer-review-context-session",
        provider=ScriptedProvider(
            [
                _tool_call("answer-review-context-read", "wiki/papers/paper-1.md"),
                _final(
                    answer=f"{rejected_text}。 [evidence:paper-1:s0001]",
                    claims=[
                        {
                            "text": f"{rejected_text}。",
                            "type": "paper_fact",
                            "paper_ids": ["paper-1"],
                            "evidence_ids": ["paper-1:s0001"],
                        }
                    ],
                    cited_evidence_ids=["paper-1:s0001"],
                ),
                _final(
                    answer=f"{approved_text}。 [evidence:paper-1:s0001]",
                    claims=[
                        {
                            "text": f"{approved_text}。",
                            "type": "paper_fact",
                            "paper_ids": ["paper-1"],
                            "evidence_ids": ["paper-1:s0001"],
                        }
                    ],
                    cited_evidence_ids=["paper-1:s0001"],
                ),
            ]
        ),
        review_provider=ScriptedProvider(
            [
                f"VERDICT: REVISE\n\n{review_feedback}。",
                "VERDICT: APPROVE\n\n审核通过。",
            ]
        ),
    )
    assert first["status"] == "completed"

    second_provider = ScriptedProvider(
        [
            _final(
                answer="当前资料不足。",
                claims=[],
                cited_evidence_ids=[],
                status="insufficient_evidence",
            )
        ]
    )
    second = run_qa(
        workspace,
        "还有哪些信息？",
        "semantic-answer-review-context-session",
        provider=second_provider,
        review_provider=ScriptedProvider(["VERDICT: APPROVE\n\n审核通过。"]),
    )

    assert second["status"] == "completed"
    request = json.dumps(second_provider.requests[0], ensure_ascii=False)
    assert approved_text in request
    assert rejected_text not in request
    assert review_feedback not in request
    message_log = (
        workspace
        / "memory"
        / "sessions"
        / "semantic-answer-review-context-session"
        / "messages.jsonl"
    ).read_text(encoding="utf-8")
    assert approved_text in message_log
    assert rejected_text in message_log
    assert review_feedback in message_log


def test_semantic_answer_review_reject_can_drive_additional_read(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    qa_provider = ScriptedProvider(
        [
            _tool_call("answer-review-reject-read", "wiki/papers/paper-1.md"),
            _final(
                answer="论文记录了方法。 [evidence:paper-1:s0001]",
                claims=[
                    {
                        "text": "论文记录了方法。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                cited_evidence_ids=["paper-1:s0001"],
            ),
            _tool_call(
                "answer-review-reject-evidence",
                "wiki/evidence/paper-1/s0001.md",
            ),
            _final(
                answer="原文章节描述了方法流程。 [evidence:paper-1:s0001]",
                claims=[
                    {
                        "text": "原文章节描述了方法流程。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                cited_evidence_ids=["paper-1:s0001"],
            ),
        ]
    )
    review_provider = ScriptedProvider(
        [
            "VERDICT: REJECT\n\n需要读取实际章节后完整重答。",
            "VERDICT: APPROVE\n\n重新回答得到章节支持。",
        ]
    )

    result = run_qa(
        workspace,
        "方法？",
        "semantic-answer-review-reject-session",
        provider=qa_provider,
        review_provider=review_provider,
    )

    assert result["status"] == "completed"
    assert result["read_budget"]["calls_used"] == 2
    assert result["review_attempts"] == 2
    assert len(qa_provider.requests) == 4
    assert len(review_provider.requests) == 2


def test_semantic_answer_review_uses_safe_insufficient_fallback_after_two_repairs(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    candidate = _final(
        answer="论文记录了方法。 [evidence:paper-1:s0001]",
        claims=[
            {
                "text": "论文记录了方法。",
                "type": "paper_fact",
                "paper_ids": ["paper-1"],
                "evidence_ids": ["paper-1:s0001"],
            }
        ],
        cited_evidence_ids=["paper-1:s0001"],
    )
    qa_provider = ScriptedProvider(
        [
            _tool_call("answer-review-fallback-read", "wiki/papers/paper-1.md"),
            candidate,
            candidate,
            candidate,
        ]
    )
    review_provider = ScriptedProvider(
        [
            "VERDICT: REVISE\n\n第一次修订。",
            "VERDICT: REVISE\n\n第二次修订。",
            "VERDICT: REJECT\n\n仍不能得到支持。",
        ]
    )

    result = run_qa(
        workspace,
        "方法？",
        "semantic-answer-review-fallback-session",
        provider=qa_provider,
        review_provider=review_provider,
    )

    assert result["status"] == "completed"
    assert result["answer_status"] == "insufficient_evidence"
    assert result["claims"] == []
    assert result["cited_evidence_ids"] == []
    assert result["memory_patch"]["evidence_ids"] == []
    assert result["review"]["verdict"] == "REJECT"
    assert result["review_attempts"] == 3
    assert result["safe_fallback"] is True
    assert len(qa_provider.requests) == 4
    assert len(review_provider.requests) == 3
    review_root = workspace / "runs" / result["run_id"] / "review" / "answer"
    assert sorted(path.name for path in review_root.iterdir()) == ["1", "2", "3"]


def test_semantic_answer_review_transient_failure_resumes_without_repeating_qa(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    qa_provider = ScriptedProvider(
        [
            _tool_call("answer-review-resume-read", "wiki/papers/paper-1.md"),
            _final(
                answer="论文记录了方法。 [evidence:paper-1:s0001]",
                claims=[
                    {
                        "text": "论文记录了方法。",
                        "type": "paper_fact",
                        "paper_ids": ["paper-1"],
                        "evidence_ids": ["paper-1:s0001"],
                    }
                ],
                cited_evidence_ids=["paper-1:s0001"],
            ),
        ]
    )

    interrupted = run_qa(
        workspace,
        "方法？",
        "semantic-answer-review-resume-session",
        provider=qa_provider,
        review_provider=ScriptedProvider([]),
    )

    assert interrupted["status"] == "interrupted"
    assert interrupted["next_nodes"] == ["answer_review"]
    audit = workspace / "runs" / interrupted["run_id"] / "review" / "answer" / "1"
    assert (audit / "request.md").is_file()
    assert not (audit / "response.md").exists()

    resumed_review = ScriptedProvider(["VERDICT: APPROVE\n\n审核通过。"])
    result = resume_qa(
        workspace,
        interrupted["thread_id"],
        provider=ScriptedProvider([]),
        review_provider=resumed_review,
    )

    assert result["status"] == "completed"
    assert len(qa_provider.requests) == 2
    assert len(resumed_review.requests) == 1
