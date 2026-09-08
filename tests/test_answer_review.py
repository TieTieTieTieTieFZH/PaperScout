from __future__ import annotations

from pathlib import Path

import pytest

from paperscout.answer_review import (
    collect_answer_review_evidence,
    validate_answer_rules,
    verify_answer_review_evidence,
)
from paperscout.models import QAAnswer, ReadResourceRecord, ReviewVerdict


def _evidence_file(workspace: Path, evidence_id: str, text: str = "原文方法描述。") -> Path:
    paper_id, section_id = evidence_id.rsplit(":", 1)
    path = workspace / "wiki" / "evidence" / paper_id / f"{section_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# Method\n\n- Evidence ID: `{evidence_id}`\n\n{text}\n",
        encoding="utf-8",
    )
    return path


def _answer(evidence_id: str = "paper-1:s0001") -> QAAnswer:
    return QAAnswer(
        answer=f"论文描述了方法。 [evidence:{evidence_id}]",
        claims=[
            {
                "text": "论文描述了方法。",
                "type": "paper_fact",
                "paper_ids": [evidence_id.rsplit(":s", 1)[0]],
                "evidence_ids": [evidence_id],
            }
        ],
        cited_evidence_ids=[evidence_id],
        status="answered",
    )


def _observed(evidence_id: str = "paper-1:s0001") -> list[ReadResourceRecord]:
    return [
        ReadResourceRecord(
            path="wiki/papers/paper-1.md",
            offset_chars=0,
            returned_chars=100,
            sha256="paper-wiki-hash",
            evidence_ids=[evidence_id],
        )
    ]


def test_answer_rules_collect_only_cited_current_evidence(tmp_path: Path) -> None:
    _evidence_file(tmp_path, "paper-1:s0001", "被引用的方法证据。")
    _evidence_file(tmp_path, "paper-1:s0002", "不应发送给 Review。")

    decision, evidence = validate_answer_rules(
        workspace=tmp_path,
        question="这篇论文的方法是什么？",
        answer=_answer(),
        read_resources=_observed(),
    )

    assert decision.verdict == ReviewVerdict.APPROVE
    assert [item.evidence_id for item in evidence] == ["paper-1:s0001"]
    assert evidence[0].paper_id == "paper-1"
    assert evidence[0].path == "wiki/evidence/paper-1/s0001.md"
    assert "被引用的方法证据" in evidence[0].content
    assert "不应发送给 Review" not in evidence[0].content
    assert len(evidence[0].sha256) == 64


def test_answer_rules_fail_closed_when_evidence_file_is_missing_or_mismatched(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        validate_answer_rules(
            workspace=tmp_path,
            question="方法是什么？",
            answer=_answer(),
            read_resources=_observed(),
        )

    path = _evidence_file(tmp_path, "paper-1:s0001")
    path.write_text(
        "# Method\n\n- Evidence ID: `paper-1:s9999`\n\n错误内容。\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not declare"):
        collect_answer_review_evidence(tmp_path, ["paper-1:s0001"])


def test_answer_rules_require_citations_for_explicit_source_request(tmp_path: Path) -> None:
    answer = QAAnswer(
        answer="没有给出依据。",
        claims=[],
        cited_evidence_ids=[],
        status="answered",
    )

    with pytest.raises(ValueError, match="explicitly requests evidence"):
        validate_answer_rules(
            workspace=tmp_path,
            question="请给出原文依据。",
            answer=answer,
            read_resources=[],
        )


def test_answer_evidence_hash_verification_detects_change(tmp_path: Path) -> None:
    path = _evidence_file(tmp_path, "paper-1:s0001")
    evidence = collect_answer_review_evidence(tmp_path, ["paper-1:s0001"])
    expected_hashes = {item.evidence_id: item.sha256 for item in evidence}

    verify_answer_review_evidence(tmp_path, expected_hashes)
    path.write_text(path.read_text(encoding="utf-8") + "已变化。\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed after deterministic review"):
        verify_answer_review_evidence(tmp_path, expected_hashes)
