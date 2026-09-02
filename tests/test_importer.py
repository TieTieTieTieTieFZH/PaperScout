from pathlib import Path

from paperscout.importer import import_preparsed


def test_importer_normalizes_prefixed_mineru_files(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"pdf")
    mineru = tmp_path / "output"
    mineru.mkdir()
    (mineru / "paper.md").write_text("# arXiv:2605.23655v1\n\nA title", encoding="utf-8")
    (mineru / "paper_content_list.json").write_text("[]", encoding="utf-8")
    (mineru / "paper_content_list_v2.json").write_text("[]", encoding="utf-8")
    (mineru / "paper_origin.pdf").write_bytes(b"pdf")

    imported = import_preparsed(tmp_path / "workspace", mineru, source_pdf=source)

    assert imported.paper_id == "2605.23655v1"
    raw = tmp_path / "workspace/raw/papers/2605.23655v1"
    assert (raw / "source.pdf").exists()
    assert (raw / "mineru/full.md").exists()
    assert (raw / "mineru/content_list.json").exists()
    assert (raw / "mineru/content_list_v2.json").exists()
    assert (raw / "metadata.json").exists()
    assert (raw / "mineru/task.json").exists()
