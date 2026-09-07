from __future__ import annotations

from pathlib import Path

from .models import ProjectMemory, ReadResourceRecord, SessionMessage, SessionState
from .storage import read_json, write_json, write_text_atomic


INVALID_SESSION_ID_CHARS = frozenset('<>:"/\\|?*')


def session_dir(workspace: Path, session_id: str) -> Path:
    """Return one session directory without allowing path traversal or Windows ADS."""
    if (
        not session_id
        or session_id != session_id.strip()
        or session_id in {".", ".."}
        or any(
            character in INVALID_SESSION_ID_CHARS or ord(character) < 32
            for character in session_id
        )
    ):
        raise ValueError("session_id must be a safe single path component")
    workspace_root = workspace.resolve()
    sessions_root = (workspace_root / "memory" / "sessions").resolve()
    if not sessions_root.is_relative_to(workspace_root):
        raise ValueError("Session root must remain inside the workspace")
    path = sessions_root / session_id
    if not path.resolve().is_relative_to(sessions_root):
        raise ValueError("session_id must remain inside the session root")
    if path.exists() and (not path.is_dir() or path.is_symlink()):
        raise ValueError("session_id must resolve to a normal session directory")
    return path


def load_session(workspace: Path, session_id: str) -> tuple[SessionState, list[SessionMessage]]:
    path = session_dir(workspace, session_id)
    state_path = path / "state.json"
    messages_path = path / "messages.jsonl"
    summary_path = path / "summary.md"
    present = [candidate.exists() for candidate in (state_path, messages_path, summary_path)]
    if not any(present):
        return SessionState(session_id=session_id), []
    if not all(present):
        raise ValueError(f"Session {session_id} has an incomplete file set")
    if any(
        not candidate.is_file() or candidate.is_symlink()
        for candidate in (state_path, messages_path, summary_path)
    ):
        raise ValueError(f"Session {session_id} files must be normal files")

    state = SessionState.model_validate(read_json(state_path))
    if state.session_id != session_id:
        raise ValueError("Session state does not match session_id")
    if state.messages:
        raise ValueError("Session state must not embed the durable message log")
    summary = summary_path.read_text(encoding="utf-8")
    if summary != state.summary:
        raise ValueError("Session summary.md does not match state.json")
    messages = [
        SessionMessage.model_validate_json(line)
        for line in messages_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    return state, messages


def merge_project_memory(current: ProjectMemory, patch: ProjectMemory) -> ProjectMemory:
    def unique(values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    aliases = dict(current.paper_aliases)
    aliases.update(patch.paper_aliases)
    return ProjectMemory(
        research_goal=(
            patch.research_goal if patch.research_goal is not None else current.research_goal
        ),
        paper_aliases=aliases,
        confirmed_decisions=unique([*current.confirmed_decisions, *patch.confirmed_decisions]),
        unresolved_questions=unique([*current.unresolved_questions, *patch.unresolved_questions]),
        research_hypotheses=unique([*current.research_hypotheses, *patch.research_hypotheses]),
        evidence_ids=unique([*current.evidence_ids, *patch.evidence_ids]),
    )


def merge_read_resources(
    current: list[ReadResourceRecord],
    added: list[ReadResourceRecord],
) -> list[ReadResourceRecord]:
    merged: list[ReadResourceRecord] = []
    seen: set[tuple[str, int, int, str, tuple[str, ...]]] = set()
    for record in [*current, *added]:
        identity = (
            record.path,
            record.offset_chars,
            record.returned_chars,
            record.sha256,
            tuple(record.evidence_ids),
        )
        if identity not in seen:
            seen.add(identity)
            merged.append(record)
    return merged


def persist_session(
    workspace: Path,
    *,
    session_id: str,
    messages: list[SessionMessage],
    summary: str,
    memory: ProjectMemory,
    read_resources: list[ReadResourceRecord],
) -> SessionState:
    path = session_dir(workspace, session_id)
    path.mkdir(parents=True, exist_ok=True)
    state = SessionState(
        session_id=session_id,
        summary=summary,
        memory=memory,
        read_resources=read_resources,
    )
    message_log = "".join(f"{message.model_dump_json()}\n" for message in messages)
    write_text_atomic(path / "messages.jsonl", message_log)
    write_text_atomic(path / "summary.md", summary)
    write_json(path / "state.json", state.model_dump(mode="json", exclude={"messages"}))
    return state
