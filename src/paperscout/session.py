from __future__ import annotations

import json
from pathlib import Path

from .models import ProjectMemory, ReadResourceRecord, SessionMessage, SessionState
from .read_tool import project_resource_sha256
from .storage import read_json, write_json, write_text_atomic


INVALID_SESSION_ID_CHARS = frozenset('<>:"/\\|?*')
RECENT_SESSION_TURNS = 4
MAX_SUMMARY_TURNS = 20
MAX_SUMMARY_MESSAGE_CHARS = 1_000


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


def load_session(
    workspace: Path,
    session_id: str,
    *,
    project_id: str = "default",
) -> tuple[SessionState, list[SessionMessage]]:
    path = session_dir(workspace, session_id)
    state_path = path / "state.json"
    messages_path = path / "messages.jsonl"
    summary_path = path / "summary.md"
    present = [candidate.exists() for candidate in (state_path, messages_path, summary_path)]
    if not any(present):
        return SessionState(session_id=session_id, project_id=project_id), []
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
    if state.project_id != project_id:
        raise ValueError(
            f"Session {session_id} belongs to project_id {state.project_id}, not {project_id}"
        )
    if state.messages:
        raise ValueError("Session state must not embed the durable message log")
    summary = summary_path.read_text(encoding="utf-8")
    if summary != state.summary:
        raise ValueError("Session summary.md does not match state.json")
    messages = _read_message_log(messages_path)
    turns = session_turns(messages)
    if state.compacted_turns > len(turns):
        raise ValueError("Session compacted_turns exceeds the durable message history")
    if state.summary and state.compacted_turns == 0:
        inferred_compacted_turns = max(0, len(turns) - RECENT_SESSION_TURNS)
        if inferred_compacted_turns == 0:
            raise ValueError("Session summary exists without compacted history")
        state = state.model_copy(update={"compacted_turns": inferred_compacted_turns})
    expected_summary = build_session_summary(
        messages,
        memory=state.memory,
        read_resources=state.read_resources,
        compacted_turns=state.compacted_turns,
    )
    if expected_summary != state.summary:
        raise ValueError("Session summary does not match compacted_turns")
    return state, messages


def _read_message_log(path: Path) -> list[SessionMessage]:
    if not path.exists():
        return []
    if not path.is_file() or path.is_symlink():
        raise ValueError("Session message log must be a normal file")
    return [
        SessionMessage.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def session_turns(messages: list[SessionMessage]) -> list[list[SessionMessage]]:
    turns: list[list[SessionMessage]] = []
    current: list[SessionMessage] = []
    for message in messages:
        if message.role == "user":
            if current:
                turns.append(current)
            current = [message]
        elif current:
            current.append(message)
        else:
            raise ValueError("Session message log must begin with a user message")
    if current:
        turns.append(current)
    return turns


def session_model_context(messages: list[SessionMessage]) -> list[SessionMessage]:
    """Project full audit turns into model history without rejected answer drafts."""
    projected: list[SessionMessage] = []
    for turn in session_turns(messages):
        final_indexes = [
            index
            for index, message in enumerate(turn)
            if message.role == "assistant"
            and isinstance(message.content, dict)
            and message.content.get("type") == "final"
        ]
        final_index = final_indexes[-1] if final_indexes else None
        for index, message in enumerate(turn):
            if (
                message.role == "system"
                and isinstance(message.content, dict)
                and message.content.get("type") == "answer_review"
            ):
                continue
            if index in final_indexes and index != final_index:
                continue
            projected.append(message)
    return projected


def recent_session_messages(
    messages: list[SessionMessage],
    *,
    keep_turns: int = RECENT_SESSION_TURNS,
) -> list[SessionMessage]:
    turns = session_turns(messages)
    return [message for turn in turns[-keep_turns:] for message in turn]


def invalidate_stale_session_resources(
    workspace: Path,
    state: SessionState,
    messages: list[SessionMessage],
) -> tuple[SessionState, list[SessionMessage], list[str]]:
    valid_resources: list[ReadResourceRecord] = []
    stale_resources: list[ReadResourceRecord] = []
    for record in state.read_resources:
        current_sha256 = project_resource_sha256(workspace, record.path)
        if current_sha256 == record.sha256:
            valid_resources.append(record)
        else:
            stale_resources.append(record)
    if not stale_resources:
        return state, messages, []

    valid_evidence = {
        evidence_id
        for record in valid_resources
        for evidence_id in record.evidence_ids
    }
    stale_evidence = {
        evidence_id
        for record in stale_resources
        for evidence_id in record.evidence_ids
    }
    invalid_evidence = stale_evidence - valid_evidence
    memory = state.memory.model_copy(
        update={
            "evidence_ids": [
                evidence_id
                for evidence_id in state.memory.evidence_ids
                if evidence_id not in invalid_evidence
            ]
        }
    )
    refreshed = state.model_copy(
        update={
            "memory": memory,
            "read_resources": valid_resources,
            "summary": build_session_summary(
                messages,
                memory=memory,
                read_resources=valid_resources,
                compacted_turns=state.compacted_turns,
            ),
        }
    )
    stale_paths = list(dict.fromkeys(record.path for record in stale_resources))
    context_messages: list[SessionMessage] = []
    for message in messages:
        content = message.content
        if (
            message.role == "tool"
            and isinstance(content, dict)
            and content.get("path") in stale_paths
        ):
            path = str(content["path"])
            message = message.model_copy(
                update={
                    "content": {
                        "ok": False,
                        "error": {
                            "code": "STALE_SESSION_RESOURCE",
                            "message": f"{path} changed or is unavailable; read it again",
                        },
                    }
                }
            )
        context_messages.append(message)
    return refreshed, context_messages, stale_paths


def _compact_text(value: str, limit: int = MAX_SUMMARY_MESSAGE_CHARS) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


def _assistant_answer(turn: list[SessionMessage]) -> str:
    for message in reversed(turn):
        if message.role != "assistant":
            continue
        if isinstance(message.content, dict):
            if message.content.get("type") == "final":
                return _compact_text(str(message.content.get("answer", "")))
            continue
        return _compact_text(message.content)
    return ""


def build_session_summary(
    messages: list[SessionMessage],
    *,
    memory: ProjectMemory,
    read_resources: list[ReadResourceRecord],
    compacted_turns: int,
) -> str:
    turns = session_turns(messages)
    if compacted_turns < 0 or compacted_turns > len(turns):
        raise ValueError("compacted_turns must identify a prefix of the session history")
    compacted = turns[:compacted_turns]
    if not compacted:
        return ""
    omitted = max(0, len(compacted) - MAX_SUMMARY_TURNS)
    compacted = compacted[-MAX_SUMMARY_TURNS:]
    aliases = json.dumps(memory.paper_aliases, ensure_ascii=False, sort_keys=True)
    lines = [
        "# Session Summary",
        "",
        "## Project memory",
        "",
        f"- Research goal: {_compact_text(memory.research_goal or 'none')}",
        f"- Paper aliases: {aliases}",
        f"- Confirmed decisions: {json.dumps(memory.confirmed_decisions, ensure_ascii=False)}",
        f"- Unresolved questions: {json.dumps(memory.unresolved_questions, ensure_ascii=False)}",
        f"- Research hypotheses: {json.dumps(memory.research_hypotheses, ensure_ascii=False)}",
        f"- Evidence IDs: {json.dumps(memory.evidence_ids, ensure_ascii=False)}",
        "",
        "## Read resources",
        "",
    ]
    if read_resources:
        for record in read_resources:
            evidence = ", ".join(record.evidence_ids) or "none"
            lines.append(
                f"- `{record.path}` offset={record.offset_chars} chars={record.returned_chars} "
                f"sha256={record.sha256} evidence={evidence}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Compacted turns", ""])
    if omitted:
        lines.append(f"- {omitted} older compacted turns remain only in messages.jsonl.")
        lines.append("")
    first_turn_number = omitted + 1
    for index, turn in enumerate(compacted, start=first_turn_number):
        user = _compact_text(str(turn[0].content))
        answer = _assistant_answer(turn)
        lines.extend(
            [
                f"### Turn {index}",
                "",
                f"- User: {user}",
                f"- Assistant: {answer or 'no final answer'}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


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
    project_id: str = "default",
    new_messages: list[SessionMessage],
    expected_message_count: int,
    memory: ProjectMemory,
    read_resources: list[ReadResourceRecord],
    compacted_turns: int = 0,
) -> SessionState:
    path = session_dir(workspace, session_id)
    path.mkdir(parents=True, exist_ok=True)
    messages_path = path / "messages.jsonl"
    persisted_messages = _read_message_log(messages_path)
    new_message_ids = [message.message_id for message in new_messages]
    if len(persisted_messages) == expected_message_count:
        messages = [*persisted_messages, *new_messages]
    elif (
        len(persisted_messages) == expected_message_count + len(new_messages)
        and [message.message_id for message in persisted_messages[-len(new_messages) :]]
        == new_message_ids
    ):
        messages = persisted_messages
    else:
        raise RuntimeError("Session message log changed since this QA run started")
    summary = build_session_summary(
        messages,
        memory=memory,
        read_resources=read_resources,
        compacted_turns=compacted_turns,
    )
    state = SessionState(
        session_id=session_id,
        project_id=project_id,
        summary=summary,
        compacted_turns=compacted_turns,
        memory=memory,
        read_resources=read_resources,
    )
    message_log = "".join(f"{message.model_dump_json()}\n" for message in messages)
    write_text_atomic(messages_path, message_log)
    write_text_atomic(path / "summary.md", summary)
    write_json(path / "state.json", state.model_dump(mode="json", exclude={"messages"}))
    return state
