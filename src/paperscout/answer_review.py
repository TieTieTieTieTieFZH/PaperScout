from __future__ import annotations

import re
from pathlib import Path

from .models import (
    AnswerReviewEvidence,
    QAAnswer,
    ReadResourceRecord,
    ReviewDecision,
    ReviewVerdict,
)
from .storage import sha256_file


ANSWER_EVIDENCE_MARK = re.compile(r"\[evidence:([^\]]+)\]")
SECTION_EVIDENCE_ID = re.compile(
    r'^(?P<paper>[^<>:"/\\|?*\x00-\x1f]+):(?P<section>s\d{4,})$'
)
EXPLICIT_EVIDENCE_REQUEST = re.compile(
    r"原文|依据|证据|引用|出处|\bsource\b|\bevidence\b|\bcitation\b|\bquote\b",
    flags=re.IGNORECASE,
)


def _answer_evidence_path(workspace: Path, evidence_id: str) -> tuple[str, Path]:
    match = SECTION_EVIDENCE_ID.fullmatch(evidence_id)
    if match is None or match.group("paper") in {".", ".."}:
        raise ValueError(f"invalid section evidence ID: {evidence_id}")
    workspace_root = workspace.resolve()
    evidence_root = (workspace_root / "wiki" / "evidence").resolve()
    if not evidence_root.is_relative_to(workspace_root):
        raise ValueError("Answer Review evidence root must remain inside the workspace")
    path = evidence_root / match.group("paper") / f"{match.group('section')}.md"
    resolved = path.resolve()
    if not resolved.is_relative_to(evidence_root):
        raise ValueError(f"Answer Review evidence path escapes the evidence root: {evidence_id}")
    if not path.exists():
        raise ValueError(f"Answer Review evidence does not exist: {evidence_id}")
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Answer Review evidence must be a normal file: {evidence_id}")
    return match.group("paper"), path


def collect_answer_review_evidence(
    workspace: Path,
    evidence_ids: list[str],
) -> list[AnswerReviewEvidence]:
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("Answer Review evidence IDs must be unique")
    collected: list[AnswerReviewEvidence] = []
    for evidence_id in evidence_ids:
        paper_id, path = _answer_evidence_path(workspace, evidence_id)
        content = path.read_text(encoding="utf-8")
        if f"Evidence ID: `{evidence_id}`" not in content:
            raise ValueError(
                f"Answer Review evidence file does not declare {evidence_id}"
            )
        collected.append(
            AnswerReviewEvidence(
                evidence_id=evidence_id,
                paper_id=paper_id,
                path=path.relative_to(workspace.resolve()).as_posix(),
                sha256=sha256_file(path),
                content=content,
            )
        )
    return collected


def validate_answer_rules(
    *,
    workspace: Path,
    question: str,
    answer: QAAnswer,
    read_resources: list[ReadResourceRecord],
) -> tuple[ReviewDecision, list[AnswerReviewEvidence]]:
    observed = {
        evidence_id
        for record in read_resources
        for evidence_id in record.evidence_ids
    }
    cited = set(answer.cited_evidence_ids)
    unknown = sorted(cited - observed)
    if unknown:
        raise ValueError(f"answer cites evidence not observed in tool results: {unknown}")
    memory_unknown = sorted(set(answer.memory_patch.evidence_ids) - observed)
    if memory_unknown:
        raise ValueError(
            f"memory patch cites evidence not observed in tool results: {memory_unknown}"
        )
    body_citations = set(ANSWER_EVIDENCE_MARK.findall(answer.answer))
    if body_citations != cited:
        raise ValueError("answer body evidence markers must match cited_evidence_ids")
    if EXPLICIT_EVIDENCE_REQUEST.search(question) and answer.status.value != "insufficient_evidence":
        if not cited:
            raise ValueError("user explicitly requests evidence but the answer has no citations")
    evidence = collect_answer_review_evidence(workspace, answer.cited_evidence_ids)
    return ReviewDecision(verdict=ReviewVerdict.APPROVE), evidence


def verify_answer_review_evidence(
    workspace: Path,
    expected_hashes: dict[str, str],
) -> None:
    try:
        evidence = collect_answer_review_evidence(workspace, list(expected_hashes))
    except Exception as exc:
        raise RuntimeError(
            "Answer Review evidence changed or became unavailable after deterministic review"
        ) from exc
    current_hashes = {item.evidence_id: item.sha256 for item in evidence}
    if current_hashes != expected_hashes:
        changed = sorted(
            evidence_id
            for evidence_id in set(expected_hashes) | set(current_hashes)
            if expected_hashes.get(evidence_id) != current_hashes.get(evidence_id)
        )
        raise RuntimeError(
            f"Answer Review evidence changed after deterministic review: {changed}"
        )
