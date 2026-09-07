from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from paperscout.evidence import EvidenceExtractionError, build_section_evidence
from paperscout.models import ReadProjectFileArguments, ReviewDecision, SectionEvidence


FIXTURE = Path(__file__).parent / "fixtures" / "mineru_micro" / "content_list.json"


def _blocks() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _build():
    return build_section_evidence(
        _blocks(),
        "paper-1",
        raw_file="raw/papers/paper-1/source.pdf",
        mineru_file="raw/papers/paper-1/mineru/content_list.json",
        source_sha256="source-hash",
        parser_version="fixture-v1",
    )


def test_section_evidence_uses_level_two_source_indices_as_stable_ids() -> None:
    first = _build()
    second = _build()

    assert first.model_dump_json() == second.model_dump_json()
    assert [item.evidence_id for item in first.evidence] == [
        "paper-1:s0002",
        "paper-1:s0007",
        "paper-1:s0011",
    ]
    assert first.evidence[0].content_indices == [2, 3, 4, 5, 6]
    assert first.evidence[0].pages == [1, 2]
    assert first.evidence[1].content_indices == [7, 8, 10]
    assert first.evidence[1].blocks[-1].text == "Figure 1. Method overview"
    assert first.evidence[2].eligible_for_ingest is False
    assert first.report.skipped_before_first_section == 2
    assert first.report.skipped_noise_blocks == 2


def test_every_retained_block_belongs_to_exactly_one_section() -> None:
    bundle = _build()
    retained = [index for item in bundle.evidence for index in item.content_indices]

    assert len(retained) == len(set(retained))
    assert retained == [2, 3, 4, 5, 6, 7, 8, 10, 11, 12]


def test_missing_level_two_heading_fails_with_quality_report() -> None:
    with pytest.raises(EvidenceExtractionError) as caught:
        build_section_evidence(
            [{"type": "text", "page_idx": 0, "text": "Unsectioned body"}],
            "paper-1",
            raw_file="raw/papers/paper-1/source.pdf",
            mineru_file="raw/papers/paper-1/mineru/content_list.json",
        )

    assert caught.value.code == "missing_level_2_sections"
    assert caught.value.report.total_blocks == 1
    assert caught.value.report.section_count == 0


def test_contracts_reject_unknown_fields_and_invalid_review_verdicts() -> None:
    evidence = _build().evidence[0].model_dump()
    evidence["unexpected"] = True
    with pytest.raises(ValidationError):
        SectionEvidence.model_validate(evidence)
    with pytest.raises(ValidationError):
        ReviewDecision.model_validate({"verdict": "supported", "feedback": []})
    with pytest.raises(ValidationError):
        ReadProjectFileArguments.model_validate({"path": "wiki/papers/paper-1.md", "extra": 1})


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": ""},
        {"path": "wiki/papers/paper-1.md", "offset_chars": -1},
        {"path": "wiki/papers/paper-1.md", "max_chars": 0},
        {"path": "wiki/papers/paper-1.md", "max_chars": 50_001},
    ],
)
def test_read_project_file_argument_bounds(arguments: dict) -> None:
    with pytest.raises(ValidationError):
        ReadProjectFileArguments.model_validate(arguments)
