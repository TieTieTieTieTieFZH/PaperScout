from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .models import EvidenceBlock, EvidenceExtractionReport, SectionEvidence, SectionEvidenceBundle
from .storage import read_json


NOISE_TYPES = {"aside_text", "footer", "header", "page_footnote", "page_number"}
TEXT_FIELDS = (
    "text",
    "equation",
    "table_caption",
    "table_body",
    "table_footnote",
    "image_caption",
    "image_footnote",
    "chart_caption",
    "code_body",
)
REFERENCE_TITLE = re.compile(r"^(references|bibliography|参考文献)\b", flags=re.IGNORECASE)


class EvidenceExtractionError(ValueError):
    def __init__(self, code: str, message: str, report: EvidenceExtractionReport):
        super().__init__(message)
        self.code = code
        self.report = report


def _flatten_text(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_flatten_text(item))
        return result
    return []


def _block_text(block: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in TEXT_FIELDS:
        parts.extend(_flatten_text(block.get(field)))
    return "\n".join(parts)


def _text_level(block: dict[str, Any]) -> int | None:
    value = block.get("text_level")
    if value is None or isinstance(value, bool):
        return None
    try:
        level = int(value)
    except (TypeError, ValueError):
        return None
    return level if level >= 1 else None


def _is_level_two_heading(block: dict[str, Any]) -> bool:
    return block.get("type") == "text" and _text_level(block) == 2 and bool(_block_text(block))


def _bbox(block: dict[str, Any]) -> tuple[float, float, float, float] | None:
    value = block.get("bbox")
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        return tuple(float(item) for item in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def build_section_evidence(
    blocks: list[dict[str, Any]],
    paper_id: str,
    *,
    raw_file: str,
    mineru_file: str,
    source_sha256: str | None = None,
    parser_version: str | None = None,
) -> SectionEvidenceBundle:
    """Build canonical evidence using original level-2 heading indices as stable IDs."""
    starts = [index for index, block in enumerate(blocks) if _is_level_two_heading(block)]
    skipped_before_first = starts[0] if starts else len(blocks)
    skipped_noise = sum(1 for block in blocks if str(block.get("type", "unknown")) in NOISE_TYPES)
    skipped_empty = sum(
        1
        for block in blocks
        if str(block.get("type", "unknown")) not in NOISE_TYPES and not _block_text(block)
    )
    empty_report = EvidenceExtractionReport(
        total_blocks=len(blocks),
        section_count=0,
        eligible_section_count=0,
        skipped_before_first_section=skipped_before_first,
        skipped_noise_blocks=skipped_noise,
        skipped_empty_blocks=skipped_empty,
    )
    if not starts:
        raise EvidenceExtractionError(
            "missing_level_2_sections",
            "MinerU content_list.json contains no usable type=text, text_level=2 section headings",
            empty_report,
        )

    evidence: list[SectionEvidence] = []
    for position, start in enumerate(starts):
        stop = starts[position + 1] if position + 1 < len(starts) else len(blocks)
        source_blocks: list[EvidenceBlock] = []
        for content_index in range(start, stop):
            source = blocks[content_index]
            content_type = str(source.get("type", "unknown"))
            text = _block_text(source)
            if content_type in NOISE_TYPES or not text:
                continue
            page_idx = max(0, int(source.get("page_idx", 0)))
            source_blocks.append(
                EvidenceBlock(
                    content_index=content_index,
                    page=page_idx + 1,
                    page_idx=page_idx,
                    content_type=content_type,
                    text=text,
                    text_level=_text_level(source),
                    bbox=_bbox(source),
                    block_id=source.get("id") if isinstance(source.get("id"), str) else None,
                )
            )
        title = _block_text(blocks[start])
        indices = [block.content_index for block in source_blocks]
        evidence.append(
            SectionEvidence(
                evidence_id=f"{paper_id}:s{start:04d}",
                paper_id=paper_id,
                title=title,
                start_content_index=indices[0],
                end_content_index=indices[-1],
                content_indices=indices,
                pages=sorted({block.page for block in source_blocks}),
                blocks=source_blocks,
                text="\n\n".join(block.text for block in source_blocks),
                raw_file=raw_file,
                mineru_file=mineru_file,
                source_sha256=source_sha256,
                parser_version=parser_version,
                eligible_for_ingest=REFERENCE_TITLE.match(title) is None,
            )
        )

    report = EvidenceExtractionReport(
        total_blocks=len(blocks),
        section_count=len(evidence),
        eligible_section_count=sum(item.eligible_for_ingest for item in evidence),
        skipped_before_first_section=skipped_before_first,
        skipped_noise_blocks=skipped_noise,
        skipped_empty_blocks=skipped_empty,
        last_included_content_index=evidence[-1].end_content_index,
    )
    return SectionEvidenceBundle(evidence=evidence, report=report)


def extract_section_evidence(raw_dir: Path, paper_id: str) -> SectionEvidenceBundle:
    """Read canonical MinerU input without modifying the immutable raw tree."""
    metadata = read_json(raw_dir / "metadata.json")
    if metadata.get("paper_id") != paper_id:
        raise ValueError("metadata.json paper_id does not match the requested paper_id")
    relative_raw = f"raw/papers/{paper_id}/source.pdf"
    relative_mineru = f"raw/papers/{paper_id}/mineru/content_list.json"
    blocks = read_json(raw_dir / "mineru" / "content_list.json")
    if not isinstance(blocks, list) or not all(isinstance(block, dict) for block in blocks):
        raise ValueError("MinerU content_list.json must be an array of objects")
    task_metadata_path = raw_dir / "mineru" / "task-metadata.json"
    task_metadata = read_json(task_metadata_path) if task_metadata_path.exists() else {}
    parser_version = task_metadata.get("version") or task_metadata.get("parser_version")
    return build_section_evidence(
        blocks,
        paper_id,
        raw_file=relative_raw,
        mineru_file=relative_mineru,
        source_sha256=metadata.get("source_sha256"),
        parser_version=str(parser_version) if parser_version is not None else None,
    )
