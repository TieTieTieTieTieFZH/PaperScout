import os
from pathlib import Path

import pytest

from paperscout.workflow import run_ingest


DEFAULT_FIXTURE = Path(r"C:\Users\19322\MinerU\2605.23655v1 (1).pdf-bd73f0a3-a490-4d78-be2c-973727ccf8fd")


@pytest.mark.integration
def test_local_cvsearch_fixture_runs_offline(tmp_path: Path) -> None:
    fixture = Path(os.getenv("PAPERSCOUT_MINERU_FIXTURE", str(DEFAULT_FIXTURE)))
    if not fixture.is_dir():
        pytest.skip("Local MinerU fixture is not available")
    source_pdf = next(fixture.glob("*_origin.pdf"), None)
    if source_pdf is None:
        pytest.skip("Fixture has no origin PDF")

    result = run_ingest(
        workspace=tmp_path / "workspace",
        mineru_dir=fixture,
        source_pdf=source_pdf,
        paper_id="2605.23655v1",
        llm_mode="mock",
    )

    assert result["status"] == "published"
    assert (tmp_path / "workspace/wiki/summaries/2605.23655v1.md").exists()
    assert (tmp_path / "workspace/wiki/evidence/2605.23655v1.jsonl").exists()
