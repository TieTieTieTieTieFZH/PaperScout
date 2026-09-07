from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from paperscout.graph_runtime import GraphRuntime, graph_config
from paperscout.models import AgentKind, EventKind, ReviewDecision, ReviewVerdict, WorkflowEvent
from paperscout.workflow import build_ingest_graph, run_ingest_from_raw


PAPER_ID = "2409.18839v1"


def _raw_tree(workspace: Path, paper_id: str = PAPER_ID) -> Path:
    raw = workspace / "raw" / "papers" / paper_id
    (raw / "mineru").mkdir(parents=True)
    (raw / "metadata.json").write_text(json.dumps({
        "paper_id": paper_id, "title": f"测试论文 {paper_id}", "authors": ["测试作者"], "year": 2024,
        "source_pdf": f"raw/papers/{paper_id}/source.pdf", "source_sha256": "test-sha256",
    }, ensure_ascii=False), encoding="utf-8")
    (raw / "mineru" / "content_list.json").write_text(json.dumps([
        {"type": "title", "page_idx": 0, "text": "摘要", "text_level": 1},
        {"type": "text", "page_idx": 0, "text": "Abstract", "text_level": 2},
        {"type": "text", "page_idx": 0, "text": "本文提出一种可验证的研究方法。"},
        {"type": "text", "page_idx": 0, "text": "2 Method", "text_level": 2},
        {"type": "text", "page_idx": 0, "text": "该方法包含两个处理阶段。"},
        {"type": "text", "page_idx": 1, "text": "3 Experiments", "text_level": 2},
        {"type": "text", "page_idx": 1, "text": "论文在目标任务上评估该方法。"},
        {"type": "text", "page_idx": 1, "text": "4 Conclusion", "text_level": 2},
        {"type": "text", "page_idx": 1, "text": "结论受数据范围限制。"},
    ], ensure_ascii=False), encoding="utf-8")
    return raw


def _hash_tree(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*") if path.is_file()}


def _summary(evidence_id: str = f"{PAPER_ID}:s0001") -> str:
    return "\n\n".join([
        f"## 研究问题\n\n论文研究一个可验证的问题。\n\n[evidence:{evidence_id}]",
        f"## 核心思路\n\n论文提出一个方法框架。\n\n[evidence:{evidence_id}]",
        f"## 方法\n\n方法由论文原文描述。\n\n[evidence:{evidence_id}]",
        f"## 实验概况\n\n论文报告了实验评估。\n\n[evidence:{evidence_id}]",
        f"## 结论与局限\n\n结论限于论文报告的范围。\n\n[evidence:{evidence_id}]",
    ])


class SequenceProvider:
    def __init__(self, outputs: list[str], mutate: callable | None = None):
        self.outputs = outputs
        self.calls = 0
        self.messages: list[list[dict]] = []
        self.mutate = mutate

    def generate_raw_text(self, messages: list[dict]) -> str:
        self.messages.append(deepcopy(messages))
        if self.mutate is not None and self.calls == 0:
            self.mutate()
        value = self.outputs[self.calls]
        self.calls += 1
        return value


def test_raw_to_wiki_renders_five_sections_and_preserves_raw(tmp_path: Path) -> None:
    raw = _raw_tree(tmp_path)
    before = _hash_tree(raw)
    result = run_ingest_from_raw(tmp_path, PAPER_ID, llm_mode="mock")
    assert result["status"] == "published"
    assert _hash_tree(raw) == before
    summary = (tmp_path / "wiki" / "papers" / f"{PAPER_ID}.md").read_text(encoding="utf-8")
    for heading in ("研究问题", "核心思路", "方法", "实验概况", "结论与局限"):
        assert f"## {heading}" in summary
    assert "关键主张" not in summary
    assert (tmp_path / "wiki" / "indexes" / "overview.md").is_file()
    evidence_dir = tmp_path / "wiki" / "evidence" / PAPER_ID
    assert (evidence_dir / "s0001.md").is_file()
    assert (evidence_dir / "s0001.json").is_file()
    assert not (tmp_path / "wiki" / "summaries").exists()
    assert not (tmp_path / "wiki" / "concepts").exists()
    assert not (tmp_path / "wiki" / "indexes" / "concepts.json").exists()
    assert not (tmp_path / "wiki" / "indexes" / "chunks.jsonl").exists()
    run_dir = tmp_path / "runs" / result["run_id"]
    events = [
        WorkflowEvent.model_validate_json(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event.sequence for event in events] == list(range(len(events)))
    assert events[0].event_type == EventKind.RUN_STARTED
    assert events[-1].event_type == EventKind.RUN_COMPLETED
    assert not (run_dir / "state.json").exists()


def test_agent_receives_inline_citable_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _raw_tree(tmp_path)
    provider = SequenceProvider([_summary()])
    monkeypatch.setattr("paperscout.workflow._provider", lambda mode: provider)
    assert run_ingest_from_raw(tmp_path, PAPER_ID)["status"] == "published"
    prompt = provider.messages[0][1]["content"]
    assert f"<!-- evidence:{PAPER_ID}:s0001" in prompt
    assert "mineru/full.md" not in prompt
    assert "read_raw" not in str(provider.messages[0])
    assert provider.calls == 1


def test_missing_level_two_sections_fails_before_model_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _raw_tree(tmp_path)
    (raw / "mineru" / "content_list.json").write_text(
        json.dumps([{"type": "text", "page_idx": 0, "text": "Unsectioned paper"}]),
        encoding="utf-8",
    )
    provider = SequenceProvider([_summary()])
    monkeypatch.setattr("paperscout.workflow._provider", lambda mode: provider)

    result = run_ingest_from_raw(tmp_path, PAPER_ID)

    assert result["status"] == "failed"
    assert "no usable type=text, text_level=2" in result["error"]
    assert provider.calls == 0
    assert not (tmp_path / "wiki").exists()


def test_invalid_first_markdown_repairs_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _raw_tree(tmp_path)
    provider = SequenceProvider(["## 方法\n\n缺少栏目", _summary()])
    monkeypatch.setattr("paperscout.workflow._provider", lambda mode: provider)
    assert run_ingest_from_raw(tmp_path, PAPER_ID)["status"] == "published"
    assert provider.calls == 2


@pytest.mark.parametrize("invalid", [
    _summary("other:s0001"),
    _summary().replace(f"[evidence:{PAPER_ID}:s0001]", f"[evidence:{PAPER_ID}:s0001] [evidence:{PAPER_ID}:s0001]"),
    _summary().replace(f"[evidence:{PAPER_ID}:s0001]", f"[evidence:{PAPER_ID}:s0000] [evidence:{PAPER_ID}:s0001] [evidence:{PAPER_ID}:s0002] [evidence:{PAPER_ID}:s0000]"),
])
def test_invalid_citations_never_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str) -> None:
    _raw_tree(tmp_path)
    provider = SequenceProvider([invalid, invalid])
    monkeypatch.setattr("paperscout.workflow._provider", lambda mode: provider)
    assert run_ingest_from_raw(tmp_path, PAPER_ID)["status"] == "failed"
    assert not (tmp_path / "wiki").exists()


def test_truncated_prefix_fails_before_calling_the_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _raw_tree(tmp_path)
    provider = SequenceProvider([_summary()])
    provider.settings = SimpleNamespace(ingest_context_window=10)
    monkeypatch.setattr("paperscout.workflow._provider", lambda mode: provider)
    result = run_ingest_from_raw(tmp_path, PAPER_ID)
    assert result["status"] == "failed"
    assert "refusing to publish an incomplete" in result["error"]
    assert provider.calls == 0
    assert not (tmp_path / "wiki").exists()


def test_raw_change_after_generation_prevents_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _raw_tree(tmp_path)
    def mutate() -> None:
        path = raw / "mineru" / "content_list.json"
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    provider = SequenceProvider([_summary()], mutate=mutate)
    monkeypatch.setattr("paperscout.workflow._provider", lambda mode: provider)
    assert run_ingest_from_raw(tmp_path, PAPER_ID)["status"] == "failed"
    assert not (tmp_path / "wiki").exists()


def test_ingest_runs_as_checkpointed_state_graph(tmp_path: Path) -> None:
    _raw_tree(tmp_path)

    result = run_ingest_from_raw(tmp_path, PAPER_ID, llm_mode="mock")

    assert result["status"] == "published"
    assert (tmp_path / "runtime" / "checkpoints.sqlite").is_file()
    config = graph_config(f"ingest:{result['run_id']}")
    with GraphRuntime.open(tmp_path) as runtime:
        graph = build_ingest_graph(runtime, provider=SequenceProvider([_summary()]), context_window=128_000)
        snapshot = graph.get_state(config)
        nodes = set(graph.get_graph().nodes)
    assert snapshot.values["status"] == "completed"
    assert snapshot.values["published"] is True
    assert snapshot.values["current_node"] == "publish_wiki"
    assert {
        "prepare_ingest_context",
        "ingest_agent",
        "validate_wiki",
        "repair_ingest",
        "verify_raw",
        "render_wiki",
        "wiki_rules",
        "wiki_review",
        "revise_ingest",
        "verify_publish",
        "publish_wiki",
        "fail_run",
    } <= nodes


def test_coverage_failure_is_checkpointed_and_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _raw_tree(tmp_path)
    provider = SequenceProvider([_summary()])
    provider.settings = SimpleNamespace(ingest_context_window=10)
    monkeypatch.setattr("paperscout.workflow._provider", lambda mode: provider)

    result = run_ingest_from_raw(tmp_path, PAPER_ID)

    assert result["status"] == "failed"
    assert result["coverage"]["truncated"] is True
    assert result["coverage"]["eligible_section_count"] == 4
    assert result["coverage"]["last_included_content_index"] is None
    assert provider.calls == 0
    assert not (tmp_path / "wiki").exists()
    with GraphRuntime.open(tmp_path) as runtime:
        graph = build_ingest_graph(runtime, provider=provider, context_window=10)
        snapshot = graph.get_state(graph_config(f"ingest:{result['run_id']}"))
    assert snapshot.values["status"] == "failed"
    assert snapshot.values["published"] is False
    assert snapshot.values["current_node"] == "fail_run"
    assert snapshot.values["input_coverage"]["truncated"] is True
    assert "refusing to publish an incomplete" in snapshot.values["last_error"]


def test_rule_review_rejection_is_checkpointed_and_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _raw_tree(tmp_path)
    rejected = ReviewDecision(verdict=ReviewVerdict.REJECT, feedback=["coverage is insufficient"])
    monkeypatch.setattr("paperscout.workflow.check_wiki_rules", lambda staging: rejected)

    result = run_ingest_from_raw(tmp_path, PAPER_ID, llm_mode="mock")

    assert result["status"] == "failed"
    assert result["rule_review"] == rejected.model_dump(mode="json")
    assert not (tmp_path / "wiki").exists()
    with GraphRuntime.open(tmp_path) as runtime:
        graph = build_ingest_graph(runtime, provider=SequenceProvider([_summary()]), context_window=128_000)
        snapshot = graph.get_state(graph_config(f"ingest:{result['run_id']}"))
    assert snapshot.values["status"] == "failed"
    assert snapshot.values["current_node"] == "fail_run"
    assert snapshot.values["rule_review"]["verdict"] == "REJECT"


def test_raw_change_during_review_prevents_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _raw_tree(tmp_path)

    def approve_after_mutation(staging: Path) -> ReviewDecision:
        path = raw / "mineru" / "content_list.json"
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        return ReviewDecision(verdict=ReviewVerdict.APPROVE)

    monkeypatch.setattr("paperscout.workflow.check_wiki_rules", approve_after_mutation)

    result = run_ingest_from_raw(tmp_path, PAPER_ID, llm_mode="mock")

    assert result["status"] == "failed"
    assert "Raw input changed during ingest" in result["error"]
    assert not (tmp_path / "wiki").exists()


def test_wiki_review_receives_only_cited_evidence_and_writes_audit_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _raw_tree(tmp_path)
    ingest_provider = SequenceProvider([_summary()])
    review_provider = SequenceProvider(["VERDICT: APPROVE\n\n未发现需要修改的问题。"])
    monkeypatch.setattr("paperscout.workflow._provider", lambda mode: ingest_provider)
    monkeypatch.setattr("paperscout.workflow._review_provider", lambda mode: review_provider)

    result = run_ingest_from_raw(tmp_path, PAPER_ID)

    assert result["status"] == "published"
    assert review_provider.calls == 1
    review_prompt = review_provider.messages[0][1]["content"]
    assert f"evidence:{PAPER_ID}:s0001" in review_prompt
    assert f"evidence:{PAPER_ID}:s0003" not in review_prompt
    audit = tmp_path / "runs" / result["run_id"] / "review" / "wiki" / "1"
    assert (audit / "request.md").read_text(encoding="utf-8") == review_prompt
    assert (audit / "response.md").is_file()
    assert json.loads((audit / "result.json").read_text(encoding="utf-8"))["verdict"] == "APPROVE"
    events = [
        WorkflowEvent.model_validate_json(line)
        for line in (tmp_path / "runs" / result["run_id"] / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    semantic_events = [event for event in events if event.agent == AgentKind.WIKI_REVIEW]
    assert [event.event_type for event in semantic_events] == [EventKind.REVIEW_STARTED, EventKind.REVIEW_COMPLETED]


def test_wiki_review_revise_regenerates_and_reviews_full_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _raw_tree(tmp_path)
    ingest_provider = SequenceProvider([_summary(), _summary()])
    review_provider = SequenceProvider(
        [
            "VERDICT: REVISE\n\n请缩小结论范围。",
            "VERDICT: APPROVE\n\n未发现需要修改的问题。",
        ]
    )
    monkeypatch.setattr("paperscout.workflow._provider", lambda mode: ingest_provider)
    monkeypatch.setattr("paperscout.workflow._review_provider", lambda mode: review_provider)

    result = run_ingest_from_raw(tmp_path, PAPER_ID)

    assert result["status"] == "published"
    assert ingest_provider.calls == 2
    assert review_provider.calls == 2
    assert "请缩小结论范围" in ingest_provider.messages[1][1]["content"]
    review_root = tmp_path / "runs" / result["run_id"] / "review" / "wiki"
    assert (review_root / "1" / "result.json").is_file()
    assert (review_root / "2" / "result.json").is_file()


@pytest.mark.parametrize(
    "review_outputs",
    [
        ["VERDICT: REJECT\n\n内容与原文不一致。", "VERDICT: REJECT\n\n仍与原文不一致。"],
        ["审核通过，但没有合法状态行。"],
    ],
)
def test_wiki_review_failure_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review_outputs: list[str]
) -> None:
    _raw_tree(tmp_path)
    ingest_provider = SequenceProvider([_summary(), _summary()])
    review_provider = SequenceProvider(review_outputs)
    monkeypatch.setattr("paperscout.workflow._provider", lambda mode: ingest_provider)
    monkeypatch.setattr("paperscout.workflow._review_provider", lambda mode: review_provider)

    result = run_ingest_from_raw(tmp_path, PAPER_ID)

    assert result["status"] == "failed"
    assert not (tmp_path / "wiki").exists()
    if len(review_outputs) == 2:
        assert ingest_provider.calls == 2
        assert review_provider.calls == 2
        assert result["review"]["verdict"] == "REJECT"
    else:
        assert ingest_provider.calls == 1
        assert review_provider.calls == 1
        assert "VERDICT" in result["error"]
