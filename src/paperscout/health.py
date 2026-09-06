from __future__ import annotations

from pathlib import Path

from .models import ReviewResult
from .storage import read_json
from .wiki import read_evidence, validate_summary_markdown


def review_wiki(wiki: Path) -> ReviewResult:
    """Rule-only audit for all generated ingest artifacts and their citations."""
    issues: list[str] = []
    sources_path = wiki / "indexes" / "sources.json"
    sources = read_json(sources_path) if sources_path.exists() else []
    if not sources:
        issues.append("No source index entries found.")
    all_evidence: set[str] = set()
    evidence_count = 0
    for source in sources:
        paper_id = source.get("paper_id", "")
        evidence_path = wiki / source.get("evidence_path", "")
        summary_path = wiki / source.get("summary_path", "")
        if not evidence_path.is_file():
            issues.append(f"Missing evidence file: {source.get('evidence_path')}")
            continue
        if not summary_path.is_file():
            issues.append(f"Missing summary file: {source.get('summary_path')}")
        evidence = read_evidence(evidence_path)
        if summary_path.is_file():
            try:
                validate_summary_markdown(summary_path.read_text(encoding="utf-8"), evidence)
            except ValueError as exc:
                issues.append(f"Invalid summary {source.get('summary_path')}: {exc}")
        evidence_count += len(evidence)
        for item in evidence:
            if item.evidence_id in all_evidence:
                issues.append(f"Duplicate evidence_id: {item.evidence_id}")
            all_evidence.add(item.evidence_id)
        if not paper_id:
            issues.append("Source index entry has no paper_id")

    return ReviewResult(
        status="supported" if not issues else "unsupported",
        feedback=issues,
        checked_evidence=evidence_count,
    )


def write_health_report_for_wiki(wiki: Path) -> Path:
    review = review_wiki(wiki)
    sources_path = wiki / "indexes" / "sources.json"
    sources = read_json(sources_path) if sources_path.exists() else []
    status = "healthy" if review.status == "supported" else "needs_attention"
    report = wiki / "health" / "latest-report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        "\n".join([
            "# Wiki Health Report",
            "",
            f"- Status: `{status}`",
            f"- Sources: {len(sources)}",
            f"- Evidence records: {review.checked_evidence}",
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
