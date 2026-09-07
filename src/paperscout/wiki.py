from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .models import (
    SectionEvidence,
    WikiCandidate,
    WikiSection,
    WikiSectionKind,
)
from .storage import read_json, write_json


EVIDENCE_MARK = re.compile(r"\[evidence:([^\]]+)\]")
CANONICAL_WIKI_HEADINGS = (
    (WikiSectionKind.RESEARCH_QUESTION, "研究问题"),
    (WikiSectionKind.CORE_IDEA, "核心思路"),
    (WikiSectionKind.METHOD, "方法"),
    (WikiSectionKind.EXPERIMENT_OVERVIEW, "实验概况"),
    (WikiSectionKind.CONCLUSION_AND_LIMITATIONS, "结论与局限"),
)


def render_citable_sections(evidence: list[SectionEvidence]) -> str:
    return "\n\n".join(
        "\n".join(
            [
                f"<!-- evidence:{item.evidence_id} | pages:{','.join(map(str, item.pages))} | section:{item.title} -->",
                item.text,
            ]
        )
        for item in evidence
    )


def validate_wiki_markdown(
    markdown: str,
    *,
    paper: dict[str, Any],
    evidence: list[SectionEvidence],
) -> WikiCandidate:
    body = markdown.strip()
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", body, flags=re.MULTILINE))
    expected = [title for _, title in CANONICAL_WIKI_HEADINGS]
    if [match.group(1) for match in matches] != expected:
        raise ValueError(f"Wiki 必须且只能按顺序包含五个栏目: {', '.join(expected)}")
    allowed = {item.evidence_id for item in evidence}
    sections: list[WikiSection] = []
    for index, (kind, title) in enumerate(CANONICAL_WIKI_HEADINGS):
        match = matches[index]
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        section_text = body[match.end():end].strip()
        citations = EVIDENCE_MARK.findall(section_text)
        prose = EVIDENCE_MARK.sub("", section_text).strip()
        if not prose:
            raise ValueError(f"栏目“{title}”缺少正文")
        if not 1 <= len(citations) <= 3:
            raise ValueError(f"栏目“{title}”必须引用 1–3 条 evidence")
        if len(citations) != len(set(citations)):
            raise ValueError(f"栏目“{title}”包含重复 evidence ID")
        unknown = sorted(set(citations) - allowed)
        if unknown:
            raise ValueError(f"栏目“{title}”引用了未提供的 evidence ID: {', '.join(unknown)}")
        sections.append(WikiSection(kind=kind, content=prose, evidence_ids=citations))
    return WikiCandidate(
        paper_id=paper["paper_id"],
        title=paper["title"],
        sections=sections,
        input_evidence_ids=[item.evidence_id for item in evidence],
        source_sha256=paper["source_sha256"],
    )


def render_paper_wiki(paper: dict[str, Any], candidate: WikiCandidate) -> str:
    lines = [
        f"# {paper['title']}",
        "",
        f"- Paper ID: `{paper['paper_id']}`",
        f"- Authors: {', '.join(paper.get('authors', [])) or 'Unknown'}",
        f"- Year: {paper.get('year') or 'Unknown'}",
        f"- Source SHA256: `{paper['source_sha256']}`",
        "",
    ]
    titles = dict(CANONICAL_WIKI_HEADINGS)
    for section in candidate.sections:
        lines.extend(
            [
                f"## {titles[section.kind]}",
                "",
                section.content,
                "",
                " ".join(f"[evidence:{evidence_id}]" for evidence_id in section.evidence_ids),
                "",
            ]
        )
    return "\n".join(lines)


def write_section_evidence(wiki_root: Path, evidence: list[SectionEvidence]) -> None:
    for item in evidence:
        section_id = item.evidence_id.rsplit(":", 1)[-1]
        directory = wiki_root / "evidence" / item.paper_id
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / f"{section_id}.json", item.model_dump(mode="json"))
        lines = [
            f"# {item.title}",
            "",
            f"- Evidence ID: `{item.evidence_id}`",
            f"- Pages: {', '.join(map(str, item.pages))}",
            f"- Content indices: {', '.join(map(str, item.content_indices))}",
            f"- Eligible for Ingest: {'yes' if item.eligible_for_ingest else 'no'}",
            "",
        ]
        for block in item.blocks:
            bbox = ",".join(map(str, block.bbox)) if block.bbox else "unknown"
            lines.extend(
                [
                    f"<!-- block:{block.content_index} | page:{block.page} | type:{block.content_type} | bbox:{bbox} -->",
                    block.text,
                    "",
                ]
            )
        (directory / f"{section_id}.md").write_text("\n".join(lines), encoding="utf-8")


def read_section_evidence(directory: Path) -> list[SectionEvidence]:
    return [SectionEvidence.model_validate(read_json(path)) for path in sorted(directory.glob("s*.json"))]


def write_canonical_indexes(wiki_root: Path, paper: dict[str, Any], candidate: WikiCandidate) -> None:
    indexes = wiki_root / "indexes"
    indexes.mkdir(parents=True, exist_ok=True)
    sources_path = indexes / "sources.json"
    sources = read_json(sources_path) if sources_path.exists() else []
    sources = [source for source in sources if source.get("paper_id") != paper["paper_id"]]
    by_kind = {section.kind: section for section in candidate.sections}
    sources.append(
        {
            "paper_id": paper["paper_id"],
            "title": paper["title"],
            "authors": paper.get("authors", []),
            "year": paper.get("year"),
            "paper_path": f"papers/{paper['paper_id']}.md",
            "evidence_dir": f"evidence/{paper['paper_id']}",
            "source_pdf": paper["source_pdf"],
            "source_sha256": paper["source_sha256"],
            "research_question": by_kind[WikiSectionKind.RESEARCH_QUESTION].content,
            "core_idea": by_kind[WikiSectionKind.CORE_IDEA].content,
        }
    )
    sources.sort(key=lambda source: source["paper_id"])
    write_json(sources_path, sources)
    overview = ["# PaperScout Wiki", ""]
    for source in sources:
        overview.extend(
            [
                f"## {source['title']}",
                "",
                f"- Paper ID: `{source['paper_id']}`",
                f"- 研究问题：{source['research_question']}",
                f"- 核心思路：{source['core_idea']}",
                f"- Wiki: [{source['paper_path']}](../{source['paper_path']})",
                "",
            ]
        )
    (indexes / "overview.md").write_text("\n".join(overview), encoding="utf-8")
