from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from paperscout.models import ReadBudget, ReadProjectFileArguments
from paperscout.read_tool import read_project_file


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "wiki" / "papers").mkdir(parents=True)
    (tmp_path / "wiki" / "indexes").mkdir(parents=True)
    (tmp_path / "raw" / "papers" / "paper-1").mkdir(parents=True)
    (tmp_path / "wiki" / "papers" / "paper-1.md").write_text("0123456789", encoding="utf-8")
    (tmp_path / "wiki" / "indexes" / "overview.md").write_text("overview", encoding="utf-8")
    return tmp_path


def test_text_read_is_truncated_and_updates_budget_and_resource_record(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    outcome = read_project_file(
        workspace,
        ReadProjectFileArguments(path="wiki/papers/paper-1.md", max_chars=4),
        ReadBudget(max_calls=2, max_chars_per_call=5, max_chars_total=8),
    )

    assert outcome.result.model_dump(mode="json") == {
        "status": "success",
        "path": "wiki/papers/paper-1.md",
        "kind": "text",
        "content": "0123",
        "entries": [],
        "offset_chars": 0,
        "next_offset_chars": 4,
        "returned_chars": 4,
        "truncated": True,
        "sha256": hashlib.sha256(b"0123456789").hexdigest(),
        "media_type": "text/markdown",
        "error_code": None,
        "error": None,
    }
    assert outcome.budget.calls_used == 1
    assert outcome.budget.chars_used == 4
    assert outcome.record is not None
    assert outcome.record.path == "wiki/papers/paper-1.md"
    assert outcome.record.offset_chars == 0
    assert outcome.record.returned_chars == 4


def test_total_budget_caps_the_text_result_without_exceeding_limit(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    budget = ReadBudget(
        max_calls=3,
        max_chars_per_call=10,
        max_chars_total=6,
        calls_used=1,
        chars_used=4,
    )

    outcome = read_project_file(
        workspace,
        {"path": "wiki/papers/paper-1.md", "offset_chars": 4, "max_chars": 6},
        budget,
    )

    assert outcome.result.content == "45"
    assert outcome.result.truncated is True
    assert outcome.result.next_offset_chars == 6
    assert outcome.budget.calls_used == 2
    assert outcome.budget.chars_used == 6


def test_directory_listing_is_sorted_and_budgeted_as_text(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "wiki" / "papers" / "zeta").mkdir()
    (workspace / "wiki" / "papers" / "alpha.md").write_text("a", encoding="utf-8")

    outcome = read_project_file(workspace, {"path": "wiki/papers"}, ReadBudget())

    assert outcome.result.status == "success"
    assert outcome.result.kind == "directory"
    assert outcome.result.content == "alpha.md\npaper-1.md\nzeta/"
    assert [entry.path for entry in outcome.result.entries] == [
        "wiki/papers/alpha.md",
        "wiki/papers/paper-1.md",
        "wiki/papers/zeta",
    ]
    assert outcome.budget.chars_used == len(outcome.result.content)


def test_truncated_directory_does_not_leak_unbudgeted_entries(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    outcome = read_project_file(
        workspace,
        {"path": "wiki/papers", "max_chars": 5},
        ReadBudget(max_chars_per_call=5),
    )

    assert outcome.result.truncated is True
    assert len(outcome.result.content) == 5
    assert outcome.result.entries == []


@pytest.mark.parametrize(
    ("path", "error_code"),
    [
        ("../outside.txt", "INVALID_PATH"),
        ("wiki/../raw/papers/paper-1", "INVALID_PATH"),
        ("raw/metadata.json", "PATH_OUTSIDE_ALLOWED_ROOTS"),
        ("runs/run-1/events.jsonl", "PATH_OUTSIDE_ALLOWED_ROOTS"),
        ("C:/Windows/win.ini", "INVALID_PATH"),
        ("wiki/missing.md", "NOT_FOUND"),
    ],
)
def test_invalid_or_out_of_scope_paths_return_structured_errors(
    tmp_path: Path, path: str, error_code: str
) -> None:
    workspace = _workspace(tmp_path)

    outcome = read_project_file(workspace, {"path": path}, ReadBudget(max_calls=10))

    assert outcome.result.status == "error"
    assert outcome.result.error_code == error_code
    assert outcome.result.content is None
    assert outcome.record is None
    assert outcome.budget.calls_used == 1
    assert outcome.budget.chars_used == 0


def test_symlink_cannot_escape_allowed_roots(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = workspace / "wiki" / "papers" / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError as exc:
        if sys.platform != "win32":
            pytest.skip(f"symbolic links unavailable: {exc}")
        junction = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if junction.returncode != 0:
            pytest.skip(f"symbolic links and junctions unavailable: {junction.stderr or junction.stdout}")

    outcome = read_project_file(workspace, {"path": "wiki/papers/escape/secret.txt"}, ReadBudget())

    assert outcome.result.status == "error"
    assert outcome.result.error_code == "PATH_OUTSIDE_ALLOWED_ROOTS"
    assert outcome.result.content is None


def test_pdf_and_image_are_returned_as_resources_without_binary_content(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    pdf = workspace / "raw" / "papers" / "paper-1" / "source.pdf"
    image = workspace / "raw" / "papers" / "paper-1" / "figure.png"
    pdf.write_bytes(b"%PDF-test")
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    pdf_outcome = read_project_file(workspace, {"path": "raw/papers/paper-1/source.pdf"}, ReadBudget())
    image_outcome = read_project_file(workspace, {"path": "raw/papers/paper-1/figure.png"}, ReadBudget())

    assert pdf_outcome.result.kind == "pdf"
    assert pdf_outcome.result.content is None
    assert pdf_outcome.result.sha256 == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert pdf_outcome.result.media_type == "application/pdf"
    assert image_outcome.result.kind == "image"
    assert image_outcome.result.content is None
    assert image_outcome.result.media_type == "image/png"


def test_unsupported_file_and_exhausted_budget_fail_before_content_is_returned(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "wiki" / "papers" / "data.bin").write_bytes(b"binary")

    unsupported = read_project_file(workspace, {"path": "wiki/papers/data.bin"}, ReadBudget())
    exhausted = read_project_file(
        workspace,
        {"path": "wiki/papers/paper-1.md"},
        ReadBudget(max_calls=1, calls_used=1),
    )

    assert unsupported.result.error_code == "UNSUPPORTED_FILE_TYPE"
    assert unsupported.result.content is None
    assert exhausted.result.error_code == "BUDGET_EXCEEDED"
    assert exhausted.result.content is None
    assert exhausted.budget.calls_used == 1


def test_invalid_schema_returns_error_without_consuming_budget(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    budget = ReadBudget()

    outcome = read_project_file(
        workspace,
        {"path": "wiki/papers/paper-1.md", "max_chars": 50_001, "unknown": True},
        budget,
    )

    assert outcome.result.error_code == "INVALID_ARGUMENTS"
    assert outcome.budget == budget
