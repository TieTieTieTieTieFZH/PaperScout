from __future__ import annotations

from pathlib import Path

from .models import ProjectMemory, ProjectState
from .storage import read_json, write_json


INVALID_PROJECT_ID_CHARS = frozenset('<>:"/\\|?*')


def project_memory_dir(workspace: Path, project_id: str) -> Path:
    """Return one project-memory directory without allowing path traversal or ADS."""
    if (
        not project_id
        or project_id != project_id.strip()
        or project_id in {".", ".."}
        or any(
            character in INVALID_PROJECT_ID_CHARS or ord(character) < 32
            for character in project_id
        )
    ):
        raise ValueError("project_id must be a safe single path component")
    workspace_root = workspace.resolve()
    projects_root = (workspace_root / "memory" / "projects").resolve()
    if not projects_root.is_relative_to(workspace_root):
        raise ValueError("Project memory root must remain inside the workspace")
    path = projects_root / project_id
    if not path.resolve().is_relative_to(projects_root):
        raise ValueError("project_id must remain inside the project memory root")
    if path.exists() and (not path.is_dir() or path.is_symlink()):
        raise ValueError("project_id must resolve to a normal project memory directory")
    return path


def load_project_memory(workspace: Path, project_id: str) -> ProjectState:
    path = project_memory_dir(workspace, project_id)
    state_path = path / "state.json"
    if not state_path.exists():
        return ProjectState(project_id=project_id)
    if not state_path.is_file() or state_path.is_symlink():
        raise ValueError(f"Project memory {project_id} state must be a normal file")
    state = ProjectState.model_validate(read_json(state_path))
    if state.project_id != project_id:
        raise ValueError("Project memory state does not match project_id")
    return state


def persist_project_memory(
    workspace: Path,
    *,
    project_id: str,
    memory: ProjectMemory,
) -> ProjectState:
    path = project_memory_dir(workspace, project_id)
    path.mkdir(parents=True, exist_ok=True)
    state_path = path / "state.json"
    if state_path.exists() and (not state_path.is_file() or state_path.is_symlink()):
        raise ValueError(f"Project memory {project_id} state must be a normal file")
    state = ProjectState(project_id=project_id, memory=memory)
    write_json(state_path, state.model_dump(mode="json"))
    return state
