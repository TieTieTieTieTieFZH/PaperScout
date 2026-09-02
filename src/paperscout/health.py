from __future__ import annotations

from pathlib import Path

from .storage import read_json
from .wiki import read_evidence


def write_health_report(workspace: Path) -> Path:
    wiki = workspace / "wiki"
    issues: list[str] = []
    sources_path = wiki / "indexes" / "sources.json"
    sources = read_json(sources_path) if sources_path.exists() else []
    if not sources:
        issues.append("No source index entries found.")
    evidence_count = 0
    for source in sources:
        evidence_path = wiki / source["evidence_path"]
        if not evidence_path.exists():
            issues.append(f"Missing evidence file: {source['evidence_path']}")
            continue
        evidence = read_evidence(evidence_path)
        evidence_count += len(evidence)
        seen: set[str] = set()
        for item in evidence:
            if item.evidence_id in seen:
                issues.append(f"Duplicate evidence_id: {item.evidence_id}")
            seen.add(item.evidence_id)
    status = "healthy" if not issues else "needs_attention"
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
            *([f"- {issue}" for issue in issues] or ["- None"]),
            "",
        ]),
        encoding="utf-8",
    )
    return report
