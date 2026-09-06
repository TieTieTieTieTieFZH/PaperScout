from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .models import Evidence, QAResult
from .storage import read_json, write_json


NOISE_TYPES = {"aside_text", "header", "footer", "page_number", "page_footnote"}
SUMMARY_HEADINGS = (
    ("research_question", "研究问题"),
    ("main_contribution", "主要贡献"),
    ("method", "方法"),
    ("experimental_findings", "实验发现"),
    ("limitations", "局限性"),
)
EVIDENCE_MARK = re.compile(r"\[evidence:([^\]]+)\]")


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
    """Extract stable, source-addressable records from one content_list.json."""
    mineru = raw_dir / "mineru"
    blocks = read_json(mineru / "content_list.json")
    block_ids: dict[tuple[int, str], str] = {}
    block_list_path = mineru / "block_list.json"
    if block_list_path.exists():
        for page in read_json(block_list_path).get("pdfData", []):
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
        result.append(Evidence(
            evidence_id=f"{paper_id}:e{index:04d}", paper_id=paper_id,
            page=page_idx + 1, page_idx=page_idx, content_index=index,
            block_id=block_ids.get((page_idx, quote)), section=section,
            content_type=content_type, quote=quote,
            bbox=[float(value) for value in bbox] if isinstance(bbox, list) and len(bbox) == 4 else None,
            raw_file=f"raw/papers/{paper_id}/source.pdf",
            mineru_file=f"raw/papers/{paper_id}/mineru/content_list.json",
        ))
    return result


def render_citable_document(evidence: list[Evidence]) -> str:
    """Render the only citable reading input; it is never persisted."""
    return "\n\n".join(
        f"<!-- evidence:{item.evidence_id} | page:{item.page} | section:{item.section or 'Unknown'} | type:{item.content_type} -->\n{item.quote}"
        for item in evidence
    )


def write_evidence(path: Path, evidence: list[Evidence]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(item.model_dump_json() + "\n" for item in evidence), encoding="utf-8")


def read_evidence(path: Path) -> list[Evidence]:
    return [Evidence.model_validate(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def summary_evidence_ids(summary: str) -> list[str]:
    return EVIDENCE_MARK.findall(summary)


def validate_summary_markdown(markdown: str, evidence: list[Evidence]) -> str:
    """Validate an agent-authored five-section summary without rewriting its prose."""
    body = markdown.strip()
    expected = [title for _, title in SUMMARY_HEADINGS]
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", body, flags=re.MULTILINE))
    headings = [match.group(1) for match in matches]
    if headings != expected:
        raise ValueError(f"摘要必须且只能按顺序包含五个栏目: {', '.join(expected)}")
    allowed = {item.evidence_id for item in evidence}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        section_text = body[match.end():end].strip()
        citations = EVIDENCE_MARK.findall(section_text)
        prose = EVIDENCE_MARK.sub("", section_text).strip()
        if not prose:
            raise ValueError(f"栏目“{match.group(1)}”缺少正文")
        if not 1 <= len(citations) <= 3:
            raise ValueError(f"栏目“{match.group(1)}”必须引用 1–3 条 evidence")
        if len(set(citations)) != len(citations):
            raise ValueError(f"栏目“{match.group(1)}”包含重复 evidence ID")
        unknown = sorted(set(citations) - allowed)
        if unknown:
            raise ValueError(f"栏目“{match.group(1)}”引用了当前论文不存在的 evidence ID: {', '.join(unknown)}")
    return body + "\n"


def render_summary(paper: dict[str, Any], body: str) -> str:
    return "\n".join([
        f"# {paper['title']}", "", f"- Paper ID: `{paper['paper_id']}`",
        f"- Authors: {', '.join(paper.get('authors', [])) or 'Unknown'}",
        f"- Year: {paper.get('year') or 'Unknown'}", "", body.strip(), "",
    ])


def write_indexes(wiki_root: Path, paper: dict[str, Any], summary_path: Path) -> None:
    """Maintain the sole Wiki index: one source record per paper."""
    indexes = wiki_root / "indexes"
    indexes.mkdir(parents=True, exist_ok=True)
    sources_path = indexes / "sources.json"
    sources = read_json(sources_path) if sources_path.exists() else []
    sources = [source for source in sources if source.get("paper_id") != paper["paper_id"]]
    sources.append({
        "paper_id": paper["paper_id"], "title": paper["title"], "authors": paper.get("authors", []),
        "year": paper.get("year"), "summary_path": str(summary_path.relative_to(wiki_root)),
        "evidence_path": f"evidence/{paper['paper_id']}.jsonl", "source_pdf": paper["source_pdf"],
        "mineru_path": f"raw/papers/{paper['paper_id']}/mineru", "status": "published_candidate",
        "source_sha256": paper["source_sha256"],
    })
    write_json(sources_path, sorted(sources, key=lambda source: source["paper_id"]))


def migrate_legacy_wiki(wiki_root: Path) -> None:
    """Remove obsolete concept/chunk artifacts only from a staging snapshot."""
    concepts = wiki_root / "concepts"
    if concepts.exists():
        shutil.rmtree(concepts)
    for name in ("concepts.json", "chunks.jsonl"):
        path = wiki_root / "indexes" / name
        if path.exists():
            path.unlink()
    for summary_path in (wiki_root / "summaries").glob("*.md"):
        text = summary_path.read_text(encoding="utf-8")
        migrated = re.sub(r"\n## 关键主张\s*\n.*\Z", "\n", text, flags=re.DOTALL)
        def cap_section_citations(match: re.Match[str]) -> str:
            seen = 0
            def cap_marker(marker: re.Match[str]) -> str:
                nonlocal seen
                seen += 1
                return marker.group(0) if seen <= 3 else ""
            return match.group(1) + EVIDENCE_MARK.sub(cap_marker, match.group(2))
        migrated = re.sub(r"(?ms)(^## [^\n]+\n)(.*?)(?=^## |\Z)", cap_section_citations, migrated)
        if migrated != text:
            summary_path.write_text(migrated, encoding="utf-8")


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]{2,}", text.lower())) | set(re.findall(r"[\u4e00-\u9fff]", text))


def retrieve(workspace: Path, question: str, paper_id: str | None = None, limit: int = 3) -> tuple[list[dict[str, Any]], dict[str, Evidence]]:
    """Retrieve summaries first, then only their explicitly linked evidence."""
    wiki = workspace / "wiki"
    sources_path = wiki / "indexes" / "sources.json"
    if not sources_path.exists():
        return [], {}
    sources = read_json(sources_path)
    if paper_id is not None:
        sources = [source for source in sources if source.get("paper_id") == paper_id]
    question_terms = _terms(question)
    ranked: list[tuple[int, dict[str, Any], str]] = []
    for source in sources:
        summary_path = wiki / source.get("summary_path", "")
        if not summary_path.is_file():
            continue
        text = summary_path.read_text(encoding="utf-8")
        ranked.append((sum(text.lower().count(term) for term in question_terms), source, text))
    ranked.sort(key=lambda item: (-item[0], item[1].get("paper_id", "")))
    chunks: list[dict[str, Any]] = []
    evidence_by_id: dict[str, Evidence] = {}
    for _, source, text in ranked[:limit]:
        chunks.append({"kind": "summary", "paper_id": source["paper_id"], "path": source["summary_path"], "text": text})
        available = {item.evidence_id: item for item in read_evidence(wiki / source["evidence_path"])}
        for evidence_id in summary_evidence_ids(text):
            item = available.get(evidence_id)
            if item is None or evidence_id in evidence_by_id:
                continue
            evidence_by_id[evidence_id] = item
            chunks.append({"kind": "evidence", "paper_id": item.paper_id, "evidence_id": item.evidence_id,
                           "page": item.page, "section": item.section, "text": item.quote})
    return chunks, evidence_by_id


def validate_qa(result: QAResult, evidence_by_id: dict[str, Evidence]) -> tuple[str, list[str]]:
    feedback: list[str] = []
    citations = result.citations + [citation for claim in result.claims for citation in claim.citations]
    for citation in citations:
        source = evidence_by_id.get(citation.evidence_id)
        if source is None:
            feedback.append(f"Citation was not loaded from a matched summary: {citation.evidence_id}")
        elif citation.page != source.page:
            feedback.append(f"Page mismatch for {citation.evidence_id}")
        elif citation.quote and citation.quote not in source.quote:
            feedback.append(f"Quote mismatch for {citation.evidence_id}")
    return ("supported", []) if not feedback else ("unsupported", feedback)
