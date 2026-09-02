from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

from .models import PaperMetadata, utc_now
from .storage import FileSystemStore, safe_copy, sha256_file, write_json


class ImportedPaper:
    def __init__(self, paper_id: str, raw_dir: Path, metadata: PaperMetadata):
        self.paper_id = paper_id
        self.raw_dir = raw_dir
        self.metadata = metadata


def _first_file(directory: Path, patterns: list[str]) -> Path | None:
    for pattern in patterns:
        match = next(iter(directory.glob(pattern)), None)
        if match and match.is_file():
            return match
    return None


def _infer_paper_id(text: str, directory: Path) -> str:
    match = re.search(r"arXiv\s*:\s*([0-9]{4}\.\d{4,5}(?:v\d+)?)", text, re.IGNORECASE)
    if match:
        return match.group(1)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", directory.name).strip("_") or "paper"


def _infer_title(full_text: str) -> str:
    for line in full_text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "Unknown title"


def _infer_authors(full_text: str) -> list[str]:
    lines = [line.strip() for line in full_text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if line.startswith("# ") and index + 1 < len(lines):
            candidate = lines[index + 1]
            if not candidate.startswith("#") and len(candidate) < 500:
                return [part.strip() for part in re.split(r",|\s{2,}", candidate) if part.strip()]
    return []


def import_preparsed(
    workspace: Path,
    mineru_path: Path,
    source_pdf: Path | None = None,
    paper_id: str | None = None,
    title: str | None = None,
    authors: list[str] | None = None,
    year: int | None = None,
    task_metadata: dict | None = None,
) -> ImportedPaper:
    """Normalize a user-supplied PDF and MinerU output into the immutable raw layer."""
    mineru_path = mineru_path.expanduser().resolve()
    if not mineru_path.is_dir():
        raise FileNotFoundError(f"MinerU directory does not exist: {mineru_path}")

    full_md = _first_file(mineru_path, ["full.md", "*.md"])
    content_list = _first_file(mineru_path, ["content_list.json", "*_content_list.json"])
    if not full_md or not content_list:
        raise FileNotFoundError("Preparsed input requires full.md and content_list.json")

    if source_pdf is None:
        source_pdf = _first_file(mineru_path, ["source.pdf", "*_origin.pdf", "*.pdf"])
    if not source_pdf or not source_pdf.is_file():
        raise FileNotFoundError("A source PDF is required for the immutable raw layer")
    source_pdf = source_pdf.expanduser().resolve()

    full_text = full_md.read_text(encoding="utf-8")
    resolved_id = paper_id or _infer_paper_id(full_text, mineru_path)
    raw_dir = FileSystemStore(workspace).paper_raw_dir(resolved_id)
    if raw_dir.exists():
        raise FileExistsError(f"Raw paper already exists: {raw_dir}")
    raw_mineru = raw_dir / "mineru"
    raw_mineru.mkdir(parents=True, exist_ok=True)

    safe_copy(source_pdf, raw_dir / "source.pdf")
    source_hash = sha256_file(source_pdf)

    canonical = {
        full_md: raw_mineru / "full.md",
        content_list: raw_mineru / "content_list.json",
    }
    optional_patterns = {
        "*_content_list_v2.json": "content_list_v2.json",
        "layout.json": "layout.json",
        "block_list.json": "block_list.json",
        "*_model.json": "model.json",
        "middle.json": "middle.json",
    }
    for source, destination in canonical.items():
        safe_copy(source, destination)
    for pattern, destination_name in optional_patterns.items():
        source = _first_file(mineru_path, [pattern])
        if source:
            safe_copy(source, raw_mineru / destination_name)

    images = mineru_path / "images"
    if images.is_dir():
        shutil.copytree(images, raw_mineru / "images")

    # Preserve other non-PDF MinerU artifacts without making them part of the required contract.
    extras = raw_mineru / "extra"
    known = {full_md.resolve(), content_list.resolve()}
    known.update(path.resolve() for path in [mineru_path / "images"] if path.exists())
    for source in mineru_path.iterdir():
        if source.is_file() and source.suffix.lower() != ".pdf" and source.resolve() not in known:
            if source.name in {destination.name for destination in canonical.values()}:
                continue
            if any(source.match(pattern) for pattern in optional_patterns):
                continue
            safe_copy(source, extras / source.name)

    metadata = PaperMetadata(
        paper_id=resolved_id,
        title=title or _infer_title(full_text),
        authors=authors or _infer_authors(full_text),
        year=year or _infer_year(full_text),
        source_pdf="raw/papers/%s/source.pdf" % resolved_id,
        source_sha256=source_hash,
        imported_at=utc_now(),
    )
    write_json(raw_dir / "metadata.json", metadata.model_dump(mode="json"))
    write_json(
        raw_mineru / "task.json",
        task_metadata
        or {
            "provider": "manual",
            "status": "provided",
            "source": "user_supplied_mineru_output",
            "submitted_at": metadata.imported_at.isoformat(),
        },
    )
    write_json(
        raw_mineru / "import_manifest.json",
        {
            "source_directory": str(mineru_path),
            "source_pdf": str(source_pdf),
            "files": sorted(str(path.relative_to(raw_mineru)) for path in raw_mineru.rglob("*") if path.is_file()),
            "source_pdf_sha256": source_hash,
        },
    )
    return ImportedPaper(resolved_id, raw_dir, metadata)


def _infer_year(text: str) -> int | None:
    match = re.search(r"(?:19|20)\d{2}", text)
    return int(match.group(0)) if match else None
