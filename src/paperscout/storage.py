from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .models import Artifact, RunEvent


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


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


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

    def append_event(self, run_id: str, event: RunEvent) -> None:
        path = self.run_dir(run_id) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")

    def checkpoint(self, run_id: str, state: dict[str, Any]) -> None:
        write_json(self.run_dir(run_id) / "state.json", state)

    def register_artifact(self, run_id: str, artifact_type: str, path: Path) -> Artifact:
        artifact = Artifact(artifact_type=artifact_type, path=str(path), sha256=sha256_file(path))
        artifacts_path = self.run_dir(run_id) / "artifacts.json"
        current = read_json(artifacts_path) if artifacts_path.exists() else []
        current.append(artifact.model_dump(mode="json"))
        write_json(artifacts_path, current)
        return artifact

    def publish_staged_wiki(self, run_id: str) -> None:
        staged = self.staging_wiki_dir(run_id)
        if not staged.exists():
            raise FileNotFoundError(f"No staged wiki found for run {run_id}")
        self.wiki.mkdir(parents=True, exist_ok=True)
        for source in staged.rglob("*"):
            if source.is_file():
                destination = self.wiki / source.relative_to(staged)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

    def write_result(self, run_id: str, result: dict[str, Any]) -> None:
        write_json(self.run_dir(run_id) / "result.json", result)


def safe_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
