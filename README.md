# PaperScout

PaperScout is a filesystem-first academic paper knowledge pipeline. The MVP accepts a source PDF plus a manually prepared MinerU output directory, builds an immutable `raw/` layer, compiles a reviewable `wiki/`, and answers questions with evidence citations.

## Current MVP boundary

- Python 3.11+, `uv`, LangGraph, Pydantic.
- Three graph agent nodes: `wiki_ingest`, `retrieval_qa`, and `review`.
- `run_ingest` accepts local MinerU output through `mineru_dir`/`mineru_path`; when omitted, it uploads `source_pdf` to MinerU's precise API and imports the returned ZIP.
- Keyword retrieval over JSONL indexes; no database or vector store.
- Deterministic mock LLM for all tests. The OpenAI-compatible Responses adapter is configuration-ready but never called by tests.
- No CLI yet. The public Python API is the initial integration surface.

## Setup and tests

```powershell
uv python install 3.11
uv sync
uv run pytest
```

The real-paper integration test uses `PAPERSCOUT_MINERU_FIXTURE` when set. Otherwise it checks the local path used during development and skips cleanly when that path is unavailable.

For precise MinerU parsing, set the API token in the environment before calling `run_ingest`:

```powershell
$env:MINERU_TOKEN = "<your MinerU API token>"
```

## Python API

```python
from pathlib import Path
from paperscout.workflow import run_ingest, run_qa

workspace = Path("./paper-workspace")
paper = run_ingest(
    workspace=workspace,
    source_pdf=Path("paper.pdf"),
    mineru_dir=Path("mineru-output"),
    paper_id="paper_001",
    llm_mode="mock",
)
# Omit mineru_dir/mineru_path to use the precise MinerU API instead.
answer = run_qa(workspace=workspace, question="What is the main contribution?", llm_mode="mock")
```
