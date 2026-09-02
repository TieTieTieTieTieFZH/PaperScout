from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass
class LLMSettings:
    base_url: str = "https://deepsy.top/v1"
    api_key: str | None = None
    model: str = "gpt-5.4-mini"
    reasoning_effort: str = "high"
    disable_response_storage: bool = True
    wire_api: str = "responses"

    @classmethod
    def from_env(cls) -> "LLMSettings":
        return cls(
            base_url=os.getenv("PAPERSCOUT_LLM_BASE_URL", cls.base_url),
            api_key=os.getenv("PAPERSCOUT_LLM_API_KEY"),
            model=os.getenv("PAPERSCOUT_LLM_MODEL", cls.model),
            reasoning_effort=os.getenv("PAPERSCOUT_LLM_REASONING_EFFORT", cls.reasoning_effort),
            disable_response_storage=os.getenv("PAPERSCOUT_LLM_DISABLE_RESPONSE_STORAGE", "true").lower() == "true",
            wire_api=os.getenv("PAPERSCOUT_LLM_WIRE_API", cls.wire_api),
        )


class MockLLM:
    """No-network provider used by tests and local development."""

    def generate_ingest(self, paper: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
        abstract = next((item for item in evidence if item.get("section") == "Abstract"), evidence[0] if evidence else None)
        abstract_text = abstract["quote"] if abstract else "No abstract evidence was available."
        first_ids = [item["evidence_id"] for item in evidence[:3]]
        return {
            "summary_sections": {
                "Research Question": "How can multimodal large language models improve high-resolution image perception?",
                "Main Contribution": f"The paper proposes {paper['title']} and an adaptive visual-search framework.",
                "Method": "The method combines assessment, expert-assisted search, semantic-guided adaptive patching, and dynamic bottom-up search.",
                "Experimental Findings": "The paper reports improved accuracy and search efficiency on high-resolution benchmarks.",
                "Limitations": "The available evidence should be checked against the paper's explicit limitations and benchmark scope.",
                "Key Claims": abstract_text,
                "Related Concepts": "Multimodal LLMs; high-resolution perception; visual search.",
            },
            "claims": [
                {"claim": "The paper addresses high-resolution image perception in multimodal LLMs.", "citations": first_ids[:1]},
                {"claim": "The proposed workflow adaptively combines different visual search strategies.", "citations": first_ids[:2]},
            ],
            "concepts": ["multimodal LLM", "high-resolution image perception", "visual search"],
        }

    def generate_qa(self, question: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
        evidence = [chunk for chunk in chunks if chunk.get("kind") == "evidence"]
        selected = evidence[:2]
        citations = [
            {"evidence_id": item["evidence_id"], "page": item["page"], "quote": item.get("text", "")}
            for item in selected
        ]
        answer = "Based on the indexed evidence: " + " ".join(item.get("text", "") for item in selected)
        return {
            "answer": answer,
            "claims": [{"claim": answer, "citations": citations}],
            "citations": citations,
        }


class OpenAICompatibleResponsesLLM:
    """Future/opt-in provider for the configured OpenAI-compatible Responses endpoint."""

    def __init__(self, settings: LLMSettings | None = None):
        self.settings = settings or LLMSettings.from_env()
        if not self.settings.api_key:
            raise ValueError("PAPERSCOUT_LLM_API_KEY is required for real LLM mode")
        from openai import OpenAI

        self.client = OpenAI(api_key=self.settings.api_key, base_url=self.settings.base_url)

    def generate_json(self, instruction: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.client.responses.create(
            model=self.settings.model,
            input=[{"role": "user", "content": [{"type": "input_text", "text": instruction + "\n" + json.dumps(payload, ensure_ascii=False)}]}],
            reasoning={"effort": self.settings.reasoning_effort},
            store=not self.settings.disable_response_storage,
        )
        return json.loads(response.output_text)
