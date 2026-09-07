from __future__ import annotations

from pathlib import Path

from .models import UserProfile
from .storage import read_json, write_json


def user_profile_path(workspace: Path) -> Path:
    """Return the global profile path while keeping it inside the workspace."""
    workspace_root = workspace.resolve()
    memory_root = (workspace_root / "memory").resolve()
    if not memory_root.is_relative_to(workspace_root):
        raise ValueError("User profile root must remain inside the workspace")
    path = memory_root / "profile.json"
    if not path.resolve().is_relative_to(memory_root):
        raise ValueError("User profile must remain inside the memory root")
    if path.exists() and (not path.is_file() or path.is_symlink()):
        raise ValueError("User profile must be a normal file")
    return path


def load_user_profile(workspace: Path) -> UserProfile:
    path = user_profile_path(workspace)
    if not path.exists():
        return UserProfile()
    return UserProfile.model_validate(read_json(path))


def save_user_profile(workspace: Path, profile: UserProfile) -> UserProfile:
    """Explicitly persist a user/host-provided profile; QA never calls this function."""
    path = user_profile_path(workspace)
    write_json(path, profile.model_dump(mode="json"))
    return profile
