"""Run the existing local MinerU fixture through the public raw-to-Wiki API."""

from pathlib import Path

from paperscout.workflow import run_ingest_from_raw


WORKSPACE = Path(r"D:\Complie\PaperScout\llmwiki\test")
PAPER_ID = "2409.18839v1"


if __name__ == "__main__":
    raw = WORKSPACE / "raw" / "papers" / PAPER_ID
    if not raw.exists():
        raise FileNotFoundError(f"Expected local raw fixture at {raw}")
    print(run_ingest_from_raw(WORKSPACE, PAPER_ID, llm_mode="real"))
