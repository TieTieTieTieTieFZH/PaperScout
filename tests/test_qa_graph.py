from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from paperscout.graph_runtime import GraphRuntime, graph_config
from paperscout.models import AgentToolCall, EventKind, QAAnswer, ReadBudget, WorkflowEvent
from paperscout.qa import build_qa_graph, parse_qa_model_response, resume_qa, run_qa
from paperscout.read_tool import model_visible_read_result, read_project_file


class ScriptedProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.requests: list[list[dict[str, Any]]] = []

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
) -> str:
    return json.dumps(
        {
            "type": "final",
            "answer": answer,
            "claims": claims,
            "cited_evidence_ids": cited_evidence_ids,
            "status": status,
            "memory_patch": {
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
