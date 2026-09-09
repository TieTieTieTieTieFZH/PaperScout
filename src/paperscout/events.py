from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from .models import (
    EventKind,
    WorkflowEvent,
    WorkflowEventPage,
    WorkflowEventReplay,
    WorkflowEventStreamStatus,
)


INVALID_RUN_ID_CHARS = frozenset('<>:"/\\|?*')
MAX_EVENT_PAGE_SIZE = 1_000
TERMINAL_EVENT_TYPES = frozenset({EventKind.RUN_COMPLETED, EventKind.RUN_FAILED})


def _events_path(workspace: Path, run_id: str) -> Path:
    if (
        not run_id
        or run_id != run_id.strip()
        or run_id in {".", ".."}
        or any(character in INVALID_RUN_ID_CHARS or ord(character) < 32 for character in run_id)
    ):
        raise ValueError("run_id must be a safe single path component")

    workspace_root = workspace.expanduser().resolve()
    runs_root = workspace_root / "runs"
    if runs_root.exists() and (not runs_root.is_dir() or runs_root.is_symlink()):
        raise ValueError("Runs root must be a normal directory")
    run_dir = runs_root / run_id
    if not run_dir.resolve().is_relative_to(runs_root.resolve()):
        raise ValueError("run_id must remain inside the runs root")
    if not run_dir.exists():
        raise FileNotFoundError(f"Workflow run does not exist: {run_id}")
    if not run_dir.is_dir() or run_dir.is_symlink():
        raise ValueError("run_id must resolve to a normal run directory")

    path = run_dir / "events.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"Workflow event log does not exist: {run_id}")
    if not path.is_file() or path.is_symlink():
        raise ValueError("Workflow event log must be a normal file")
    return path


def _validated_events(workspace: Path, run_id: str) -> list[WorkflowEvent]:
    path = _events_path(workspace, run_id)
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ValueError(f"Workflow event log is empty: {run_id}")

    events: list[WorkflowEvent] = []
    event_ids: set[str] = set()
    expected_thread_id: str | None = None
    expected_session_id: str | None = None
    terminal_seen = False
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise ValueError(f"Invalid workflow event at line {line_number}: blank line")
        try:
            event = WorkflowEvent.model_validate_json(line)
        except ValidationError as exc:
            raise ValueError(f"Invalid workflow event at line {line_number}") from exc

        expected_sequence = line_number - 1
        if event.sequence != expected_sequence:
            raise ValueError(
                f"Invalid workflow event at line {line_number}: "
                f"expected sequence {expected_sequence}, got {event.sequence}"
            )
        if event.run_id != run_id:
            raise ValueError(
                f"Workflow event at line {line_number} does not match requested run_id"
            )
        if expected_thread_id is None:
            expected_thread_id = event.thread_id
            expected_session_id = event.session_id
        elif event.thread_id != expected_thread_id or event.session_id != expected_session_id:
            raise ValueError(
                f"Workflow event at line {line_number} has inconsistent correlation fields"
            )
        if event.event_id in event_ids:
            raise ValueError(f"Workflow event at line {line_number} has duplicate event_id")
        if terminal_seen:
            raise ValueError(f"Workflow event at line {line_number} appears after terminal event")

        event_ids.add(event.event_id)
        events.append(event)
        terminal_seen = event.event_type in TERMINAL_EVENT_TYPES

    if events[0].event_type is not EventKind.RUN_STARTED:
        raise ValueError("Workflow event log must begin with run.started")
    return events


def _stream_status(events: list[WorkflowEvent]) -> WorkflowEventStreamStatus:
    last_type = events[-1].event_type
    if last_type is EventKind.RUN_COMPLETED:
        return WorkflowEventStreamStatus.COMPLETED
    if last_type is EventKind.RUN_FAILED:
        return WorkflowEventStreamStatus.FAILED
    if last_type is EventKind.RUN_INTERRUPTED:
        return WorkflowEventStreamStatus.INTERRUPTED
    return WorkflowEventStreamStatus.RUNNING


def replay_workflow_events(workspace: Path, run_id: str) -> WorkflowEventReplay:
    """Load and validate the complete durable event history for one workflow run."""
    events = _validated_events(workspace, run_id)
    status = _stream_status(events)
    first = events[0]
    return WorkflowEventReplay(
        run_id=run_id,
        thread_id=first.thread_id,
        session_id=first.session_id,
        status=status,
        terminal=status in {WorkflowEventStreamStatus.COMPLETED, WorkflowEventStreamStatus.FAILED},
        events=events,
    )


def read_workflow_events(
    workspace: Path,
    run_id: str,
    *,
    after_sequence: int = -1,
    limit: int = 100,
) -> WorkflowEventPage:
    """Read a validated page of events after an exclusive sequence cursor."""
    if after_sequence < -1:
        raise ValueError("after_sequence must be at least -1")
    if limit < 1 or limit > MAX_EVENT_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_EVENT_PAGE_SIZE}")

    replay = replay_workflow_events(workspace, run_id)
    remaining = [event for event in replay.events if event.sequence > after_sequence]
    events = remaining[:limit]
    next_after_sequence = events[-1].sequence if events else after_sequence
    return WorkflowEventPage(
        run_id=replay.run_id,
        thread_id=replay.thread_id,
        session_id=replay.session_id,
        status=replay.status,
        terminal=replay.terminal,
        events=events,
        after_sequence=after_sequence,
        next_after_sequence=next_after_sequence,
        has_more=len(remaining) > len(events),
    )
