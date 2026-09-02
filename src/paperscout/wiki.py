from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import Citation, Claim, Evidence, QAResult
from .storage import FileSystemStore, read_json, write_json


NOISE_TYPES = {"aside_text", "header", "footer", "page_number", "page_footnote"}


def _block_quote(block: dict[str, Any]) -> str:
    if isinstance(block.get("text"), str):
        return block["text"].strip()
    for key in ("table_body", "equation", "code_body", "image_caption", "chart_caption"):
        value = block.get(key)
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return " ".join(str(item) for item in value).strip()
    return ""


def extract_evidence(raw_dir: Path, paper_id: str) -> list[Evidence]:
    mineru = raw_dir / "mineru"
    content_path = mineru / "content_list.json"
    blocks = read_json(content_path)
    block_ids: dict[tuple[int, str], str] = {}
    block_list_path = mineru / "block_list.json"
    if block_list_path.exists():
        block_data = read_json(block_list_path)
        for page in block_data.get("pdfData", []):
            for block in page:
                text = _block_quote(block)
                if text and isinstance(block.get("id"), str):
                    block_ids[(int(block.get("page_idx", 0)), text)] = block["id"]

    result: list[Evidence] = []
    section = "Unknown"
    for index, block in enumerate(blocks):
        content_type = str(block.get("type", "unknown"))
        quote = _block_quote(block)
        if not quote or content_type in NOISE_TYPES:
            continue
        page_idx = int(block.get("page_idx", 0))
        level = block.get("text_level")
        if content_type in {"text", "title"} and level and int(level) > 0:
            section = re.sub(r"^#+\s*", "", quote).strip()
        bbox = block.get("bbox")
        result.append(
            Evidence(
                evidence_id=f"{paper_id}:e{index:04d}",
                paper_id=paper_id,
                page=page_idx + 1,
                page_idx=page_idx,
                content_index=index,
                block_id=block_ids.get((page_idx, quote)),
                section=section,
                content_type=content_type,
                quote=quote,
                bbox=[float(value) for value in bbox] if isinstance(bbox, list) and len(bbox) == 4 else None,
                raw_file=f"raw/papers/{paper_id}/source.pdf",
                mineru_file=f"raw/papers/{paper_id}/mineru/content_list.json",
            )
        )
    return result


def write_evidence(path: Path, evidence: list[Evidence]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(item.model_dump_json() + "\n" for item in evidence), encoding="utf-8")


def read_evidence(path: Path) -> list[Evidence]:
    return [Evidence.model_validate(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def render_summary(paper: dict[str, Any], generated: dict[str, Any], evidence: list[Evidence]) -> str:
    sections = generated["summary_sections"]
    claim_lines = []
    for claim in generated.get("claims", []):
        citations = " ".join(f"[evidence:{value}]" for value in claim.get("citations", []))
        claim_lines.append(f"- {claim['claim']} {citations}".strip())
    return "\n".join(
        [
            f"# {paper['title']}",
            "",
            f"- Paper ID: `{paper['paper_id']}`",
            f"- Authors: {', '.join(paper.get('authors', [])) or 'Unknown'}",
            f"- Year: {paper.get('year') or 'Unknown'}",
            "",
            *[f"## {name}\n\n{value}" for name, value in sections.items()],
            "",
            "## Evidence-linked Claims",
            "",
            "\n".join(claim_lines) or "- No claims generated.",
            "",
        ]
    )


def render_concept(concept: str, paper_id: str, evidence: list[Evidence]) -> str:
    refs = " ".join(f"[evidence:{item.evidence_id}]" for item in evidence[:3])
    return f"# {concept}\n\n## Definition\n\nConcept extracted from `{paper_id}` for cross-paper retrieval.\n\n## Related Papers\n\n- `{paper_id}`\n\n## Evidence\n\n{refs}\n"


def build_chunks(summary_path: Path, concept_paths: list[tuple[str, Path]], evidence: list[Evidence]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    summary_text = summary_path.read_text(encoding="utf-8")
    chunks.append({"chunk_id": "summary:" + summary_path.stem, "kind": "summary", "path": str(summary_path), "paper_id": evidence[0].paper_id if evidence else None, "text": summary_text})
    for evidence_item in evidence:
        chunks.append({"chunk_id": evidence_item.evidence_id, "kind": "evidence", "path": str(evidence_item.mineru_file), "paper_id": evidence_item.paper_id, "section": evidence_item.section, "text": evidence_item.quote, "evidence_id": evidence_item.evidence_id, "page": evidence_item.page})
    for concept, path in concept_paths:
        chunks.append({"chunk_id": "concept:" + path.stem, "kind": "concept", "path": str(path), "paper_id": evidence[0].paper_id if evidence else None, "text": path.read_text(encoding="utf-8"), "concept": concept})
    return chunks


def write_indexes(staging: Path, paper: dict[str, Any], summary_path: Path, concept_paths: list[tuple[str, Path]], evidence: list[Evidence]) -> None:
    indexes = staging / "indexes"
    indexes.mkdir(parents=True, exist_ok=True)
    sources = [{
        "paper_id": paper["paper_id"],
        "title": paper["title"],
        "authors": paper.get("authors", []),
        "year": paper.get("year"),
        "summary_path": str(summary_path.relative_to(staging)),
        "evidence_path": str((staging / "evidence" / f"{paper['paper_id']}.jsonl").relative_to(staging)),
        "source_pdf": paper["source_pdf"],
        "mineru_path": f"raw/papers/{paper['paper_id']}/mineru",
        "status": "published_candidate",
        "source_sha256": paper["source_sha256"],
    }]
    write_json(indexes / "sources.json", sources)
    concepts = [{"concept_id": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"), "name": name, "aliases": [], "papers": [paper["paper_id"]], "path": str(path.relative_to(staging))} for name, path in concept_paths]
    write_json(indexes / "concepts.json", concepts)
    chunks = build_chunks(summary_path, concept_paths, evidence)
    (indexes / "chunks.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in chunks), encoding="utf-8")


def retrieve(workspace: Path, question: str, limit: int = 8) -> list[dict[str, Any]]:
    path = workspace / "wiki" / "indexes" / "chunks.jsonl"
    if not path.exists():
        return []
    terms = {term.lower() for term in re.findall(r"\w+", question) if len(term) > 1}
    chunks = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ranked = []
    for chunk in chunks:
        text = str(chunk.get("text", "")).lower()
        score = sum(text.count(term) for term in terms)
        if chunk.get("kind") == "evidence" and score == 0:
            score = 1
        ranked.append((score, chunk))
    return [chunk for _, chunk in sorted(ranked, key=lambda item: item[0], reverse=True)[:limit]]


def validate_qa(result: QAResult, evidence_path: Path) -> tuple[str, list[str]]:
    evidence = {item.evidence_id: item for item in read_evidence(evidence_path)}
    feedback: list[str] = []
    citations = result.citations + [citation for claim in result.claims for citation in claim.citations]
    for citation in citations:
        source = evidence.get(citation.evidence_id)
        if not source:
            feedback.append(f"Unknown evidence_id: {citation.evidence_id}")
            continue
        if citation.page != source.page:
            feedback.append(f"Page mismatch for {citation.evidence_id}")
        if citation.quote and citation.quote not in source.quote:
            feedback.append(f"Quote mismatch for {citation.evidence_id}")
    if feedback:
        return "unsupported", feedback
    return "supported", []
