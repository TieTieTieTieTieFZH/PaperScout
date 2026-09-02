from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MINERU_API_BASE = "https://mineru.net/api/v4"
MINERU_TOKEN_ENV = "MINERU_TOKEN"


@dataclass(frozen=True)
class MinerUParseResult:
    output_dir: Path
    task_metadata: dict[str, Any]


class MinerUAPIError(RuntimeError):
    """Raised when MinerU rejects a request or returns an invalid response."""


def _request_json(method: str, url: str, payload: dict[str, Any] | None, token: str, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            response_body = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise MinerUAPIError(f"MinerU HTTP {exc.code} for {method} {url}: {detail[:500]}") from exc
    except URLError as exc:
        raise MinerUAPIError(f"MinerU request failed for {method} {url}: {exc.reason}") from exc

    try:
        result = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinerUAPIError(f"MinerU returned invalid JSON for {method} {url}") from exc
    if not isinstance(result, dict):
        raise MinerUAPIError(f"MinerU returned an unexpected response for {method} {url}")
    if result.get("code") not in (None, 0):
        raise MinerUAPIError(f"MinerU error {result.get('code')}: {result.get('msg', 'unknown error')}")
    return result


def _upload_file(file_path: Path, upload_url: str, timeout: float) -> None:
    # MinerU explicitly asks clients not to set Content-Type on this signed PUT.
    parsed_url = urlsplit(upload_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise MinerUAPIError("MinerU returned an invalid signed upload URL")
    connection_type = HTTPSConnection if parsed_url.scheme == "https" else HTTPConnection
    connection = connection_type(parsed_url.hostname, parsed_url.port, timeout=timeout)
    request_target = parsed_url.path or "/"
    if parsed_url.query:
        request_target += f"?{parsed_url.query}"
    try:
        connection.putrequest("PUT", request_target, skip_accept_encoding=True)
        connection.putheader("Content-Length", str(file_path.stat().st_size))
        connection.endheaders()
        with file_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                connection.send(chunk)
        response = connection.getresponse()
        detail = response.read().decode("utf-8", errors="replace")
        if response.status not in (200, 201, 204):
            raise MinerUAPIError(f"MinerU upload returned HTTP {response.status}: {detail[:500]}")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise MinerUAPIError(f"MinerU upload failed with HTTP {exc.code}: {detail[:500]}") from exc
    except (HTTPException, OSError) as exc:
        raise MinerUAPIError(f"MinerU upload failed: {exc}") from exc
    finally:
        connection.close()


def _download_file(download_url: str, destination: Path, timeout: float) -> None:
    request = Request(download_url, headers={"Accept": "application/zip"})
    try:
        with urlopen(request, timeout=timeout) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise MinerUAPIError(f"MinerU result download failed with HTTP {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise MinerUAPIError(f"MinerU result download failed: {exc.reason}") from exc


def _extract_zip(zip_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise MinerUAPIError(f"Unsafe path in MinerU result ZIP: {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def _find_output_dir(extracted_dir: Path) -> Path:
    for full_md in extracted_dir.rglob("full.md"):
        siblings = {path.name for path in full_md.parent.iterdir() if path.is_file()}
        if "content_list.json" in siblings or any(name.endswith("_content_list.json") for name in siblings):
            return full_md.parent
    raise MinerUAPIError("MinerU result ZIP does not contain full.md and content_list.json")


def _result_item(data: dict[str, Any], file_name: str) -> dict[str, Any]:
    raw_items = data.get("extract_result") or data.get("extract_results") or []
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        raw_items = []
    items = [item for item in raw_items if isinstance(item, dict)]
    for item in items:
        if item.get("file_name") == file_name:
            return item
    if len(items) == 1:
        return items[0]
    if data.get("state"):
        return data
    raise MinerUAPIError("MinerU result response did not contain the uploaded file")


def _safe_data_id(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return result[:128] or "paper"


def parse_with_mineru_api(
    source_pdf: Path,
    destination: Path,
    *,
    token: str | None = None,
    model_version: str = "vlm",
    language: str = "en",
    is_ocr: bool = False,
    enable_formula: bool = True,
    enable_table: bool = True,
    poll_interval: float = 3.0,
    timeout: float = 600.0,
) -> MinerUParseResult:
    """Upload one local PDF to MinerU's precise API and download its result ZIP."""
    source_pdf = source_pdf.expanduser().resolve()
    if not source_pdf.is_file():
        raise FileNotFoundError(f"Source PDF does not exist: {source_pdf}")
    if source_pdf.suffix.lower() != ".pdf":
        raise ValueError(f"MinerU precise parsing requires a PDF: {source_pdf}")

    resolved_token = token or os.environ.get(MINERU_TOKEN_ENV)
    if not resolved_token:
        raise ValueError(f"MinerU token is required; pass token=... or set {MINERU_TOKEN_ENV}")

    submitted_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "files": [{"name": source_pdf.name, "data_id": _safe_data_id(source_pdf.stem)}],
        "model_version": model_version,
        "language": language,
        "is_ocr": is_ocr,
        "enable_formula": enable_formula,
        "enable_table": enable_table,
    }
    submitted = _request_json("POST", f"{MINERU_API_BASE}/file-urls/batch", payload, resolved_token, timeout)
    data = submitted.get("data") or {}
    batch_id = data.get("batch_id")
    upload_urls = data.get("file_urls") or []
    if not batch_id or not upload_urls:
        raise MinerUAPIError("MinerU upload-link response did not contain batch_id and file_urls")
    _upload_file(source_pdf, str(upload_urls[0]), timeout)

    deadline = time.monotonic() + timeout
    item: dict[str, Any] = {}
    while True:
        result_response = _request_json(
            "GET",
            f"{MINERU_API_BASE}/extract-results/batch/{batch_id}",
            None,
            resolved_token,
            timeout,
        )
        item = _result_item(result_response.get("data") or {}, source_pdf.name)
        state = item.get("state")
        if state == "done":
            break
        if state == "failed":
            raise MinerUAPIError(f"MinerU parsing failed: {item.get('err_msg', 'unknown error')}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"MinerU parsing timed out after {timeout:g} seconds; batch_id={batch_id}")
        time.sleep(poll_interval)

    zip_url = item.get("full_zip_url")
    if not zip_url:
        raise MinerUAPIError("MinerU completed without full_zip_url")
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="paperscout-mineru-") as temporary_dir:
        zip_path = Path(temporary_dir) / "result.zip"
        extracted_dir = Path(temporary_dir) / "extracted"
        _download_file(str(zip_url), zip_path, timeout)
        _extract_zip(zip_path, extracted_dir)
        output_dir = _find_output_dir(extracted_dir)
        shutil.copytree(output_dir, destination, dirs_exist_ok=True)

    completed_at = datetime.now(timezone.utc).isoformat()
    return MinerUParseResult(
        output_dir=destination,
        task_metadata={
            "provider": "mineru",
            "status": "done",
            "source": "mineru_precise_api",
            "batch_id": str(batch_id),
            "file_name": source_pdf.name,
            "model_version": model_version,
            "submitted_at": submitted_at,
            "completed_at": completed_at,
            "trace_id": submitted.get("trace_id"),
        },
    )
