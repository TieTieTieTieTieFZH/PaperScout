from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import (
    ProjectFileEntry,
    ReadBudget,
    ReadProjectFileArguments,
    ReadProjectFileOutcome,
    ReadProjectFileResult,
    ReadResourceRecord,
)
from .storage import sha256_file


TEXT_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".txt": "text/plain",
}
IMAGE_MEDIA_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
EVIDENCE_ID = re.compile(r"\[?evidence:([^\]\s|]+)")


def _error(
    *,
    path: str,
    code: str,
    message: str,
    budget: ReadBudget,
) -> ReadProjectFileOutcome:
    return ReadProjectFileOutcome(
        result=ReadProjectFileResult(status="error", path=path, error_code=code, error=message),
        budget=budget,
    )


def _kind(path: Path) -> str:
    if path.is_dir():
        return "directory"
    suffix = path.suffix.lower()
    if suffix in TEXT_MEDIA_TYPES:
        return "text"
    if suffix == ".pdf":
        return "pdf"
    if suffix in IMAGE_MEDIA_TYPES:
        return "image"
    return "unsupported"


def _resolve_allowed_path(workspace: Path, relative_path: str) -> tuple[Path | None, str | None, str | None]:
    requested = Path(relative_path)
    if requested.is_absolute() or ".." in requested.parts:
        return None, "INVALID_PATH", "path must be project-relative and must not contain '..'"
    workspace = workspace.resolve()
    candidate = workspace / requested
    allowed_roots = (workspace / "wiki", workspace / "raw" / "papers")
    resolved = candidate.resolve(strict=False)
    if not any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots):
        return None, "PATH_OUTSIDE_ALLOWED_ROOTS", "path must remain inside wiki/ or raw/papers/"
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        return None, "NOT_FOUND", "requested project path does not exist"
    if not any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots):
        return None, "PATH_OUTSIDE_ALLOWED_ROOTS", "symbolic link escapes wiki/ or raw/papers/"
    return resolved, None, None


def _slice_content(content: str, offset: int, limit: int) -> tuple[str, int, bool]:
    selected = content[offset : offset + limit]
    next_offset = offset + len(selected)
    return selected, next_offset, next_offset < len(content)


def _directory_content(directory: Path, workspace: Path) -> tuple[str, list[ProjectFileEntry]]:
    entries: list[ProjectFileEntry] = []
    names: list[str] = []
    for child in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        child_kind = _kind(child)
        names.append(child.name + ("/" if child_kind == "directory" else ""))
        entries.append(
            ProjectFileEntry(
                path=child.relative_to(workspace).as_posix(),
                kind=child_kind,
            )
        )
    return "\n".join(names), entries


def read_project_file(
    workspace: Path,
    arguments: ReadProjectFileArguments | dict[str, Any],
    budget: ReadBudget,
) -> ReadProjectFileOutcome:
    """Execute the QA Agent's only host-controlled, read-only project tool."""
    try:
        parsed = (
            arguments
            if isinstance(arguments, ReadProjectFileArguments)
            else ReadProjectFileArguments.model_validate(arguments)
        )
    except ValidationError as exc:
        raw_path = arguments.get("path", "") if isinstance(arguments, dict) else ""
        return _error(
            path=str(raw_path),
            code="INVALID_ARGUMENTS",
            message=str(exc),
            budget=budget,
        )

    if budget.calls_used >= budget.max_calls:
        return _error(
            path=parsed.path,
            code="BUDGET_EXCEEDED",
            message="read call budget is exhausted",
            budget=budget,
        )
    called_budget = budget.model_copy(update={"calls_used": budget.calls_used + 1})
    if parsed.max_chars > budget.max_chars_per_call:
        return _error(
            path=parsed.path,
            code="BUDGET_EXCEEDED",
            message="requested max_chars exceeds the per-call budget",
            budget=called_budget,
        )
    remaining = budget.max_chars_total - budget.chars_used
    if remaining <= 0:
        return _error(
            path=parsed.path,
            code="BUDGET_EXCEEDED",
            message="read character budget is exhausted",
            budget=called_budget,
        )

    resolved, error_code, error_message = _resolve_allowed_path(workspace, parsed.path)
    if resolved is None:
        return _error(
            path=parsed.path,
            code=error_code or "INVALID_PATH",
            message=error_message or "invalid project path",
            budget=called_budget,
        )

    kind = _kind(resolved)
    if kind == "unsupported":
        return _error(
            path=parsed.path,
            code="UNSUPPORTED_FILE_TYPE",
            message=f"unsupported file type: {resolved.suffix.lower() or '<none>'}",
            budget=called_budget,
        )
    if kind in {"pdf", "image"}:
        if parsed.offset_chars:
            return _error(
                path=parsed.path,
                code="INVALID_ARGUMENTS",
                message="offset_chars is only valid for text and directory reads",
                budget=called_budget,
            )
        media_type = "application/pdf" if kind == "pdf" else IMAGE_MEDIA_TYPES[resolved.suffix.lower()]
        digest = sha256_file(resolved)
        result = ReadProjectFileResult(
            status="success",
            path=parsed.path.replace("\\", "/"),
            kind=kind,
            sha256=digest,
            media_type=media_type,
        )
        record = ReadResourceRecord(
            path=result.path,
            offset_chars=0,
            returned_chars=0,
            sha256=digest,
        )
        return ReadProjectFileOutcome(result=result, budget=called_budget, record=record)

    try:
        if kind == "directory":
            full_content, entries = _directory_content(resolved, workspace.resolve())
            media_type = "text/x-directory-listing"
        else:
            full_content = resolved.read_text(encoding="utf-8")
            entries = []
            media_type = TEXT_MEDIA_TYPES[resolved.suffix.lower()]
    except UnicodeDecodeError as exc:
        return _error(
            path=parsed.path,
            code="DECODE_ERROR",
            message=str(exc),
            budget=called_budget,
        )
    if parsed.offset_chars > len(full_content):
        return _error(
            path=parsed.path,
            code="INVALID_ARGUMENTS",
            message="offset_chars exceeds the available content length",
            budget=called_budget,
        )

    limit = min(parsed.max_chars, remaining)
    content, next_offset, truncated = _slice_content(full_content, parsed.offset_chars, limit)
    if kind == "directory" and (parsed.offset_chars != 0 or truncated):
        entries = []
    digest = (
        hashlib.sha256(full_content.encode("utf-8")).hexdigest()
        if kind == "directory"
        else sha256_file(resolved)
    )
    normalized_path = parsed.path.replace("\\", "/")
    result = ReadProjectFileResult(
        status="success",
        path=normalized_path,
        kind=kind,
        content=content,
        entries=entries,
        offset_chars=parsed.offset_chars,
        next_offset_chars=next_offset,
        returned_chars=len(content),
        truncated=truncated,
        sha256=digest,
        media_type=media_type,
    )
    updated_budget = called_budget.model_copy(update={"chars_used": budget.chars_used + len(content)})
    evidence_ids = sorted(set(EVIDENCE_ID.findall(content)))
    record = ReadResourceRecord(
        path=normalized_path,
        offset_chars=parsed.offset_chars,
        returned_chars=len(content),
        sha256=digest,
        evidence_ids=evidence_ids,
    )
    return ReadProjectFileOutcome(result=result, budget=updated_budget, record=record)
