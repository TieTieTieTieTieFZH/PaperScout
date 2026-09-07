from __future__ import annotations

from pathlib import Path

from .models import ReviewDecision, ReviewVerdict
from .storage import read_json
from .wiki import read_section_evidence, validate_wiki_markdown


def check_wiki_rules(wiki: Path) -> ReviewDecision:
    """Rule-only audit for all generated ingest artifacts and their citations."""
    issues: list[str] = []
    sources_path = wiki / "indexes" / "sources.json"
    sources = read_json(sources_path) if sources_path.exists() else []
    if not sources:
        issues.append("No source index entries found.")
    all_evidence: set[str] = set()
    for source in sources:
        paper_id = source.get("paper_id", "")
        evidence_dir = wiki / source.get("evidence_dir", "")
        paper_path = wiki / source.get("paper_path", "")
        if not evidence_dir.is_dir():
            issues.append(f"Missing evidence directory: {source.get('evidence_dir')}")
            continue
        if not paper_path.is_file():
            issues.append(f"Missing Wiki paper: {source.get('paper_path')}")
        evidence = read_section_evidence(evidence_dir)
        if paper_path.is_file():
            try:
                validate_wiki_markdown(paper_path.read_text(encoding="utf-8"), paper=source, evidence=evidence)
            except ValueError as exc:
                issues.append(f"Invalid Wiki {source.get('paper_path')}: {exc}")
        for item in evidence:
            if item.evidence_id in all_evidence:
                issues.append(f"Duplicate evidence_id: {item.evidence_id}")
            all_evidence.add(item.evidence_id)
        if not paper_id:
            issues.append("Source index entry has no paper_id")

    return ReviewDecision(
        verdict=ReviewVerdict.APPROVE if not issues else ReviewVerdict.REJECT,
        feedback=issues,
    )


def write_health_report_for_wiki(wiki: Path) -> Path:
    review = check_wiki_rules(wiki)
    sources_path = wiki / "indexes" / "sources.json"
    sources = read_json(sources_path) if sources_path.exists() else []
    evidence_count = sum(
        len(read_section_evidence(wiki / source["evidence_dir"]))
        for source in sources
        if (wiki / source.get("evidence_dir", "")).is_dir()
    )
    status = "healthy" if review.verdict == ReviewVerdict.APPROVE else "needs_attention"
    report = wiki / "health" / "latest-report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        "\n".join([
            "# Wiki Health Report",
            "",
            f"- Status: `{status}`",
            f"- Sources: {len(sources)}",
            f"- Evidence records: {evidence_count}",
            "",
            "## Issues",
            "",
            *([f"- {issue}" for issue in review.feedback] or ["- None"]),
            "",
        ]),
        encoding="utf-8",
    )
    return report


def write_health_report(workspace: Path) -> Path:
    return write_health_report_for_wiki(workspace / "wiki")
