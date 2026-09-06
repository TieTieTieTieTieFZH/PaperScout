"""Run ten independent real-Agent raw-to-Wiki evaluations without altering the fixture."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

from paperscout.workflow import run_ingest_from_raw


PAPER_ID = "2409.18839v1"
PROJECT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT / "llmwiki" / "test" / "raw" / "papers" / PAPER_ID
OUTPUT_ROOT = PROJECT / "llmwiki" / "evaluations" / f"ingest-agent-{datetime.now():%Y%m%d-%H%M%S}"


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def main() -> None:
    if not (FIXTURE / "metadata.json").is_file() or not (FIXTURE / "mineru" / "content_list.json").is_file():
        raise FileNotFoundError(f"Missing raw fixture: {FIXTURE}")
    before = tree_hashes(FIXTURE)
    records: list[dict[str, object]] = []
    for number in range(1, 11):
        workspace = OUTPUT_ROOT / f"run-{number:02d}"
        destination = workspace / "raw" / "papers" / PAPER_ID
        shutil.copytree(FIXTURE, destination)
        try:
            result = run_ingest_from_raw(workspace, PAPER_ID, llm_mode="real")
        except Exception as exc:  # Preserve one record even for provider-level failures.
            result = {"status": "exception", "error": str(exc)}
        record = {"attempt": number, "workspace": str(workspace), **result}
        records.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
    summary = {
        "fixture": str(FIXTURE),
        "fixture_unchanged": tree_hashes(FIXTURE) == before,
        "attempts": len(records),
        "published": sum(record.get("status") == "published" for record in records),
        "results": records,
    }
    summary["pass_rate"] = summary["published"] / summary["attempts"]
    (OUTPUT_ROOT / "summary.json").parent.mkdir(parents=True, exist_ok=True)
    (OUTPUT_ROOT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
