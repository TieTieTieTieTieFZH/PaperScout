from pathlib import Path

from paperscout.mineru import MinerUParseResult
from paperscout.workflow import run_ingest, run_qa


def make_input(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"test pdf bytes")
    mineru = tmp_path / "mineru"
    mineru.mkdir()
    (mineru / "full.md").write_text(
        "# Demo Paper\n\nAuthor One  Author Two\n\n## Abstract\n\nThis paper studies robust retrieval.\n\n## Method\n\nWe use evidence-grounded search.\n",
        encoding="utf-8",
    )
    (mineru / "content_list.json").write_text(
        '[{"type":"text","text":"Demo Paper","text_level":1,"bbox":[0,0,1,1],"page_idx":0},'
        '{"type":"text","text":"Abstract","text_level":2,"bbox":[0,0,1,1],"page_idx":0},'
        '{"type":"text","text":"This paper studies robust retrieval.","bbox":[0,0,1,1],"page_idx":0},'
        '{"type":"text","text":"We use evidence-grounded search.","bbox":[0,0,1,1],"page_idx":1}]',
        encoding="utf-8",
    )
    return source, mineru


def test_ingest_builds_raw_wiki_and_checkpoint(tmp_path: Path) -> None:
    source, mineru = make_input(tmp_path)
    workspace = tmp_path / "workspace"
    result = run_ingest(workspace, mineru, source, paper_id="demo", llm_mode="mock")

    assert result["status"] == "published"
    assert (workspace / "raw/papers/demo/source.pdf").exists()
    assert (workspace / "raw/papers/demo/metadata.json").exists()
    assert (workspace / "raw/papers/demo/mineru/task.json").exists()
    assert (workspace / "wiki/summaries/demo.md").exists()
    assert (workspace / "wiki/evidence/demo.jsonl").exists()
    assert (workspace / "wiki/indexes/chunks.jsonl").exists()
    assert (workspace / "wiki/health/latest-report.md").exists()
    assert list((workspace / "runs").glob("*/state.json"))
    assert list((workspace / "runs").glob("*/events.jsonl"))


def test_qa_returns_reviewed_citations(tmp_path: Path) -> None:
    source, mineru = make_input(tmp_path)
    workspace = tmp_path / "workspace"
    run_ingest(workspace, mineru, source, paper_id="demo", llm_mode="mock")
    result = run_qa(workspace, "What does the paper study?", llm_mode="mock")

    assert result["status"] == "completed"
    assert result["review"]["status"] == "supported"
    assert result["qa"]["citations"]


def test_ingest_uses_precise_mineru_api_when_path_is_omitted(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"pdf")

    def fake_parse(source_pdf: Path, destination: Path, *, token: str | None = None) -> MinerUParseResult:
        destination.mkdir(exist_ok=True)
        (destination / "full.md").write_text("# API Paper\n\nAuthor\n\n## Abstract\n\nParsed by API.\n", encoding="utf-8")
        (destination / "content_list.json").write_text(
            '[{"type":"text","text":"API Paper","text_level":1,"bbox":[0,0,1,1],"page_idx":0},'
            '{"type":"text","text":"Parsed by API.","bbox":[0,0,1,1],"page_idx":0}]',
            encoding="utf-8",
        )
        return MinerUParseResult(destination, {"provider": "mineru", "status": "done"})

    monkeypatch.setattr("paperscout.workflow.parse_with_mineru_api", fake_parse)
    workspace = tmp_path / "workspace"
    result = run_ingest(workspace, source_pdf=source, paper_id="api-paper", llm_mode="mock")

    assert result["status"] == "published"
    assert (workspace / "raw/papers/api-paper/mineru/full.md").exists()
    assert (workspace / "raw/papers/api-paper/mineru/task.json").read_text(encoding="utf-8").find('"provider": "mineru"') >= 0
