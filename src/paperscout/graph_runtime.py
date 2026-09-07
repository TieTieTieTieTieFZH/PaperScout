from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import StateGraph


class RetryableNodeError(RuntimeError):
    """A transient node-side failure whose pre-node checkpoint can be resumed."""


def graph_config(thread_id: str) -> dict[str, dict[str, str]]:
    if not thread_id.strip():
        raise ValueError("thread_id must not be empty")
    return {"configurable": {"thread_id": thread_id}}


@dataclass
class GraphRuntime:
    """Owned SQLite lifecycle for locally compiled, checkpointed graphs."""

    checkpoint_path: Path
    connection: sqlite3.Connection
    checkpointer: SqliteSaver

    @classmethod
    def open(cls, workspace: Path) -> "GraphRuntime":
        runtime_dir = workspace.resolve() / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = runtime_dir / "checkpoints.sqlite"
        connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
        checkpointer = SqliteSaver(connection)
        checkpointer.setup()
        return cls(checkpoint_path=checkpoint_path, connection=connection, checkpointer=checkpointer)

    def compile(self, builder: StateGraph, *, interrupt_before: list[str] | None = None) -> Any:
        return builder.compile(checkpointer=self.checkpointer, interrupt_before=interrupt_before)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "GraphRuntime":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
