from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from .models import WorkflowEvent


def llmwiki_workspace(project_root: Path) -> Path:
    """Return the persistent LLM Wiki root for a PaperScout project."""
    return project_root.expanduser().resolve() / "llmwiki"


def reset_test_workspace(project_root: Path) -> Path:
    """Clear and recreate the project-local ``llmwiki/test`` workspace."""
    project_root = project_root.expanduser().resolve()
    if not project_root.is_dir():
        raise FileNotFoundError(f"Project root does not exist: {project_root}")
    workspace = llmwiki_workspace(project_root) / "test"
    if workspace.exists():
        if not workspace.is_dir() or workspace.is_symlink():
            raise ValueError(f"Test workspace is not a normal directory: {workspace}")
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def directory_hashes(root: Path) -> dict[str, str]:
    requested_root = root
    if requested_root.is_symlink():
        raise ValueError(f"Hash root must be a normal directory: {requested_root}")
    root = requested_root.resolve()
    if not root.is_dir():
        raise ValueError(f"Hash root must be a normal directory: {root}")
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def write_json(path: Path, value: Any) -> None:
    write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def write_text_atomic(path: Path, value: str) -> None:
    """Durably replace one text file without exposing a partial target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class FileSystemStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.raw = workspace / "raw"
        self.wiki = workspace / "wiki"
        self.runs = workspace / "runs"

    def paper_raw_dir(self, paper_id: str) -> Path:
        return self.raw / "papers" / paper_id

    def run_dir(self, run_id: str) -> Path:
        return self.runs / run_id

    def staging_wiki_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "staging" / "wiki"

    def append_event(self, event: WorkflowEvent) -> None:
        path = self.run_dir(event.run_id) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")

    def publish_staged_wiki(
        self,
        run_id: str,
        *,
        expected_hashes: dict[str, str] | None = None,
    ) -> None:
        """Replace the complete Wiki snapshot and recover an interrupted atomic move."""
        staged = self.staging_wiki_dir(run_id)
        backup = self.run_dir(run_id) / "published-wiki-backup"
        if not staged.exists():
            if (
                expected_hashes is not None
                and self.wiki.is_dir()
                and not self.wiki.is_symlink()
                and directory_hashes(self.wiki) == expected_hashes
            ):
                if backup.exists():
                    if not backup.is_dir() or backup.is_symlink():
                        raise ValueError("Published Wiki backup must be a normal directory")
                    shutil.rmtree(backup)
                return
            raise FileNotFoundError(f"No staged wiki found for run {run_id}")
        if not staged.is_dir() or staged.is_symlink():
            raise ValueError("Staged wiki must be a normal directory")
        if backup.exists() and self.wiki.exists():
            raise FileExistsError(f"Wiki and backup both exist for interrupted run {run_id}")
        if not backup.exists() and self.wiki.exists():
            os.replace(self.wiki, backup)
        try:
            os.replace(staged, self.wiki)
            if expected_hashes is not None and directory_hashes(self.wiki) != expected_hashes:
                raise RuntimeError("Published Wiki does not match the reviewed staging manifest")
        except Exception:
            if self.wiki.exists() and not staged.exists():
                os.replace(self.wiki, staged)
            if backup.exists() and not self.wiki.exists():
                os.replace(backup, self.wiki)
            raise
        if backup.exists():
            shutil.rmtree(backup)

    def discard_staging_wiki(self, run_id: str) -> None:
        """Discard only the rebuildable staging tree for one run."""
        staged = self.staging_wiki_dir(run_id)
        if not staged.exists():
            return
        run_dir = self.run_dir(run_id).resolve()
        resolved = staged.resolve()
        if not resolved.is_relative_to(run_dir) or not staged.is_dir() or staged.is_symlink():
            raise ValueError("Staged wiki must be a normal directory inside its run")
        shutil.rmtree(staged)

    def prepare_staging_wiki(self, run_id: str) -> Path:
        """Create a complete candidate snapshot, preserving published index entries."""
        staged = self.staging_wiki_dir(run_id)
        if staged.exists():
            raise FileExistsError(f"Staging wiki already exists for run {run_id}")
        staged.parent.mkdir(parents=True, exist_ok=True)
        if self.wiki.exists():
            shutil.copytree(self.wiki, staged)
        else:
            staged.mkdir(parents=True)
        return staged

    def write_result(self, run_id: str, result: dict[str, Any]) -> None:
        write_json(self.run_dir(run_id) / "result.json", result)


def safe_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
