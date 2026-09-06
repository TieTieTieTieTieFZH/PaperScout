from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from paperscout.workflow import CITABLE_DOCUMENT_PATH, migrate_wiki, run_ingest_from_raw, run_qa
from paperscout.wiki import retrieve


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
        {"type": "text", "page_idx": 0, "text": "本文提出一种可验证的研究方法，并在实验中评估其效果。"},
        {"type": "text", "page_idx": 1, "text": "实验结果显示该方法在目标任务上有效，但结论受数据范围限制。"},
    ], ensure_ascii=False), encoding="utf-8")
    return raw


def _hash_tree(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*") if path.is_file()}


def _summary(evidence_id: str = f"{PAPER_ID}:e0001") -> str:
    return "\n\n".join([
        f"## 研究问题\n\n论文研究一个可验证的问题。\n\n[evidence:{evidence_id}]",
        f"## 主要贡献\n\n论文提出一个方法框架。\n\n[evidence:{evidence_id}]",
        f"## 方法\n\n方法由论文原文描述。\n\n[evidence:{evidence_id}]",
        f"## 实验发现\n\n论文报告了实验评估。\n\n[evidence:{evidence_id}]",
        f"## 局限性\n\n结论限于论文报告的范围。\n\n[evidence:{evidence_id}]",
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
    summary = (tmp_path / "wiki" / "summaries" / f"{PAPER_ID}.md").read_text(encoding="utf-8")
    for heading in ("研究问题", "主要贡献", "方法", "实验发现", "局限性"):
        assert f"## {heading}" in summary
    assert "关键主张" not in summary
    assert not (tmp_path / "wiki" / "concepts").exists()
    assert not (tmp_path / "wiki" / "indexes" / "concepts.json").exists()
    assert not (tmp_path / "wiki" / "indexes" / "chunks.jsonl").exists()


def test_agent_receives_inline_citable_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _raw_tree(tmp_path)
    provider = SequenceProvider([_summary()])
    monkeypatch.setattr("paperscout.workflow._provider", lambda mode: provider)
    assert run_ingest_from_raw(tmp_path, PAPER_ID)["status"] == "published"
    prompt = provider.messages[0][1]["content"]
    assert f"<!-- evidence:{PAPER_ID}:e0001" in prompt
    assert "mineru/full.md" not in prompt


def test_invalid_first_markdown_repairs_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _raw_tree(tmp_path)
    provider = SequenceProvider(["## 方法\n\n缺少栏目", _summary()])
    monkeypatch.setattr("paperscout.workflow._provider", lambda mode: provider)
    assert run_ingest_from_raw(tmp_path, PAPER_ID)["status"] == "published"
    assert provider.calls == 2


@pytest.mark.parametrize("invalid", [
    _summary("other:e0001"),
    _summary().replace(f"[evidence:{PAPER_ID}:e0001]", f"[evidence:{PAPER_ID}:e0001] [evidence:{PAPER_ID}:e0001]"),
    _summary().replace(f"[evidence:{PAPER_ID}:e0001]", f"[evidence:{PAPER_ID}:e0000] [evidence:{PAPER_ID}:e0001] [evidence:{PAPER_ID}:e0002] [evidence:{PAPER_ID}:e0000]"),
])
def test_invalid_citations_never_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str) -> None:
    _raw_tree(tmp_path)
    provider = SequenceProvider([invalid, invalid])
    monkeypatch.setattr("paperscout.workflow._provider", lambda mode: provider)
    assert run_ingest_from_raw(tmp_path, PAPER_ID)["status"] == "failed"
    assert not (tmp_path / "wiki").exists()


def test_long_document_uses_virtual_citable_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _raw_tree(tmp_path)
    tool_call = json.dumps({"role": "assistant", "tool_calls": [{"id": "read-1", "name": "read_raw", "arguments": {"path": CITABLE_DOCUMENT_PATH, "offset_chars": 0, "max_chars": 8}}]})
    provider = SequenceProvider([tool_call, _summary()])
    provider.settings = SimpleNamespace(ingest_context_window=10)
    monkeypatch.setattr("paperscout.workflow._provider", lambda mode: provider)
    assert run_ingest_from_raw(tmp_path, PAPER_ID)["status"] == "published"
    assert provider.messages[1][-1]["content"]["path"] == CITABLE_DOCUMENT_PATH


def test_raw_change_after_generation_prevents_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _raw_tree(tmp_path)
    def mutate() -> None:
        path = raw / "mineru" / "content_list.json"
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    provider = SequenceProvider([_summary()], mutate=mutate)
    monkeypatch.setattr("paperscout.workflow._provider", lambda mode: provider)
    assert run_ingest_from_raw(tmp_path, PAPER_ID)["status"] == "failed"
    assert not (tmp_path / "wiki").exists()


def test_staging_migrates_legacy_artifacts(tmp_path: Path) -> None:
    _raw_tree(tmp_path)
    legacy = tmp_path / "wiki"
    (legacy / "concepts").mkdir(parents=True)
    (legacy / "concepts" / "old.md").write_text("旧概念", encoding="utf-8")
    (legacy / "indexes").mkdir()
    (legacy / "indexes" / "concepts.json").write_text("[]", encoding="utf-8")
    (legacy / "indexes" / "chunks.jsonl").write_text("{}\n", encoding="utf-8")
    assert run_ingest_from_raw(tmp_path, PAPER_ID, llm_mode="mock")["status"] == "published"
    assert not (tmp_path / "wiki" / "concepts").exists()
    assert not (tmp_path / "wiki" / "indexes" / "concepts.json").exists()
    assert not (tmp_path / "wiki" / "indexes" / "chunks.jsonl").exists()


def test_explicit_migration_cleans_published_legacy_wiki(tmp_path: Path) -> None:
    _raw_tree(tmp_path)
    assert run_ingest_from_raw(tmp_path, PAPER_ID, llm_mode="mock")["status"] == "published"
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir()
    (wiki / "concepts" / "old.md").write_text("旧概念", encoding="utf-8")
    (wiki / "indexes" / "concepts.json").write_text("[]", encoding="utf-8")
    (wiki / "indexes" / "chunks.jsonl").write_text("{}\n", encoding="utf-8")
    summary_path = wiki / "summaries" / f"{PAPER_ID}.md"
    summary_path.write_text(summary_path.read_text(encoding="utf-8") + "\n## 关键主张\n\n旧主张\n", encoding="utf-8")
    assert migrate_wiki(tmp_path)["status"] == "published"
    assert not (wiki / "concepts").exists()
    assert "关键主张" not in summary_path.read_text(encoding="utf-8")


def test_qa_loads_only_evidence_linked_from_selected_summaries(tmp_path: Path) -> None:
    _raw_tree(tmp_path, PAPER_ID)
    _raw_tree(tmp_path, "paper-2")
    assert run_ingest_from_raw(tmp_path, PAPER_ID, llm_mode="mock")["status"] == "published"
    assert run_ingest_from_raw(tmp_path, "paper-2", llm_mode="mock")["status"] == "published"
    chunks, evidence = retrieve(tmp_path, "方法")
    assert {chunk["kind"] for chunk in chunks} == {"summary", "evidence"}
    assert evidence
    result = run_qa(tmp_path, "方法", llm_mode="mock")
    assert result["status"] == "completed"
    cited = result["qa"]["citations"]
    assert cited and all(item["evidence_id"] in evidence for item in cited)
