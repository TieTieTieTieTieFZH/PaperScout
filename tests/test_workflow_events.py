from __future__ import annotations

from pathlib import Path

import pytest

from paperscout import read_workflow_events, replay_workflow_events
from paperscout.models import (
    AgentKind,
    EventKind,
    WorkflowEvent,
    WorkflowEventStreamStatus,
)


def _write_events(workspace: Path, run_id: str, event_types: list[EventKind]) -> None:
    path = workspace / "runs" / run_id / "events.jsonl"
    path.parent.mkdir(parents=True)
    events = [
        WorkflowEvent(
            event_id=f"event-{sequence}",
            sequence=sequence,
            run_id=run_id,
            thread_id=f"ingest:{run_id}",
            agent=AgentKind.HOST,
            event_type=event_type,
        )
        for sequence, event_type in enumerate(event_types)
    ]
    path.write_text(
        "".join(f"{event.model_dump_json()}\n" for event in events),
        encoding="utf-8",
    )


def test_replay_workflow_events_restores_validated_order_and_status(tmp_path: Path) -> None:
    run_id = "run-1"
    _write_events(
        tmp_path,
        run_id,
        [
            EventKind.RUN_STARTED,
            EventKind.MODEL_STARTED,
            EventKind.MODEL_COMPLETED,
            EventKind.RUN_COMPLETED,
        ],
    )

    replay = replay_workflow_events(tmp_path, run_id)

    assert [event.sequence for event in replay.events] == [0, 1, 2, 3]
    assert replay.run_id == run_id
    assert replay.thread_id == f"ingest:{run_id}"
    assert replay.session_id is None
    assert replay.status is WorkflowEventStreamStatus.COMPLETED
    assert replay.terminal is True


def test_read_workflow_events_pages_after_exclusive_sequence(tmp_path: Path) -> None:
    run_id = "run-2"
    _write_events(
        tmp_path,
        run_id,
        [
            EventKind.RUN_STARTED,
            EventKind.MODEL_STARTED,
            EventKind.MODEL_COMPLETED,
            EventKind.RUN_INTERRUPTED,
        ],
    )

    first = read_workflow_events(tmp_path, run_id, limit=2)
    second = read_workflow_events(
        tmp_path,
        run_id,
        after_sequence=first.next_after_sequence,
        limit=2,
    )
    exhausted = read_workflow_events(
        tmp_path,
        run_id,
        after_sequence=second.next_after_sequence,
        limit=2,
    )

    assert [event.sequence for event in first.events] == [0, 1]
    assert first.next_after_sequence == 1
    assert first.has_more is True
    assert first.status is WorkflowEventStreamStatus.INTERRUPTED
    assert [event.sequence for event in second.events] == [2, 3]
    assert second.next_after_sequence == 3
    assert second.has_more is False
    assert second.status is WorkflowEventStreamStatus.INTERRUPTED
    assert exhausted.events == []
    assert exhausted.next_after_sequence == 3
    assert exhausted.has_more is False


@pytest.mark.parametrize(
    "event_types,status,terminal",
    [
        ([EventKind.RUN_STARTED], WorkflowEventStreamStatus.RUNNING, False),
        (
            [EventKind.RUN_STARTED, EventKind.RUN_INTERRUPTED, EventKind.RUN_RESUMED],
            WorkflowEventStreamStatus.RUNNING,
            False,
        ),
        (
            [EventKind.RUN_STARTED, EventKind.RUN_FAILED],
            WorkflowEventStreamStatus.FAILED,
            True,
        ),
    ],
)
def test_replay_workflow_events_derives_current_stream_status(
    tmp_path: Path,
    event_types: list[EventKind],
    status: WorkflowEventStreamStatus,
    terminal: bool,
) -> None:
    _write_events(tmp_path, "run-status", event_types)

    replay = replay_workflow_events(tmp_path, "run-status")

    assert replay.status is status
    assert replay.terminal is terminal


@pytest.mark.parametrize("after_sequence,limit", [(-2, 1), (-1, 0), (-1, 1001)])
def test_read_workflow_events_rejects_invalid_pagination(
    tmp_path: Path,
    after_sequence: int,
    limit: int,
) -> None:
    _write_events(tmp_path, "run-3", [EventKind.RUN_STARTED])

    with pytest.raises(ValueError):
        read_workflow_events(
            tmp_path,
            "run-3",
            after_sequence=after_sequence,
            limit=limit,
        )


@pytest.mark.parametrize("run_id", ["", " run-1", ".", "..", "../run-1", "a/b", "a:b"])
def test_replay_workflow_events_rejects_unsafe_run_id(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError, match="safe single path component"):
        replay_workflow_events(tmp_path, run_id)


@pytest.mark.parametrize(
    "lines,error",
    [
        (["not-json"], "line 1"),
        (
            [
                WorkflowEvent(
                    event_id="event-0",
                    sequence=1,
                    run_id="run-4",
                    thread_id="ingest:run-4",
                    agent=AgentKind.HOST,
                    event_type=EventKind.RUN_STARTED,
                ).model_dump_json()
            ],
            "expected sequence 0",
        ),
    ],
)
def test_replay_workflow_events_rejects_malformed_or_gapped_log(
    tmp_path: Path,
    lines: list[str],
    error: str,
) -> None:
    path = tmp_path / "runs" / "run-4" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        replay_workflow_events(tmp_path, "run-4")


def test_replay_workflow_events_rejects_mismatched_or_post_terminal_events(
    tmp_path: Path,
) -> None:
    run_id = "run-5"
    path = tmp_path / "runs" / run_id / "events.jsonl"
    path.parent.mkdir(parents=True)
    events = [
        WorkflowEvent(
            event_id="event-0",
            sequence=0,
            run_id=run_id,
            thread_id=f"ingest:{run_id}",
            agent=AgentKind.HOST,
            event_type=EventKind.RUN_STARTED,
        ),
        WorkflowEvent(
            event_id="event-1",
            sequence=1,
            run_id=run_id,
            thread_id=f"ingest:{run_id}",
            agent=AgentKind.HOST,
            event_type=EventKind.RUN_COMPLETED,
        ),
        WorkflowEvent(
            event_id="event-2",
            sequence=2,
            run_id="another-run",
            thread_id=f"ingest:{run_id}",
            agent=AgentKind.HOST,
            event_type=EventKind.MODEL_STARTED,
        ),
    ]
    path.write_text(
        "".join(f"{event.model_dump_json()}\n" for event in events),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not match requested run_id"):
        replay_workflow_events(tmp_path, run_id)

    events[2] = events[2].model_copy(update={"run_id": run_id})
    path.write_text(
        "".join(f"{event.model_dump_json()}\n" for event in events),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="after terminal event"):
        replay_workflow_events(tmp_path, run_id)
