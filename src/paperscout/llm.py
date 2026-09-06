from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class LLMSettings:
    base_url: str = "https://ai.input.im/v1"
    api_key: str | None = None
    model: str = "gpt-5.4-mini"
    reasoning_effort: str = "high"
    disable_response_storage: bool = True
    wire_api: str = "responses"
    ingest_context_window: int = 128_000

    @classmethod
    def from_env(cls) -> "LLMSettings":
        return cls(
            base_url=os.getenv("PAPERSCOUT_LLM_BASE_URL", cls.base_url),
            api_key=os.getenv("PAPERSCOUT_LLM_API_KEY"),
            model=os.getenv("PAPERSCOUT_LLM_MODEL", cls.model),
            reasoning_effort=os.getenv("PAPERSCOUT_LLM_REASONING_EFFORT", cls.reasoning_effort),
            disable_response_storage=os.getenv("PAPERSCOUT_LLM_DISABLE_RESPONSE_STORAGE", "true").lower() == "true",
            wire_api=os.getenv("PAPERSCOUT_LLM_WIRE_API", cls.wire_api),
            ingest_context_window=int(os.getenv("INGEST_LLM_CONTEXT_WINDOW", str(cls.ingest_context_window))),
        )


class MockLLM:
    """No-network provider used by tests and local development."""

    def generate_raw_text(self, messages: list[dict[str, Any]]) -> str:
        """Return a deterministic five-section Markdown summary for local tests."""
        text = "\n".join(str(message.get("content", "")) for message in messages)
        evidence_ids = re.findall(r"<!-- evidence:([^\s|]+)", text)
        if not evidence_ids:
            return ""
        citation = f"[evidence:{evidence_ids[0]}]"
        return "\n\n".join([
            "## 研究问题\n\n论文研究一个可由原文证据核查的问题。\n\n" + citation,
            "## 主要贡献\n\n论文提出了可验证的方法或系统贡献。\n\n" + citation,
            "## 方法\n\n方法依据论文原文组织处理流程。\n\n" + citation,
            "## 实验发现\n\n论文报告了与研究问题相关的评估结果。\n\n" + citation,
            "## 局限性\n\n结论应限于论文报告的范围。\n\n" + citation,
        ])

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

    def generate_raw_text(self, messages: list[dict[str, Any]]) -> str:
        """Send the complete local role history without requiring native tool support."""
        response = self.client.responses.create(
            model=self.settings.model,
            input=[{"role": "user", "content": [{"type": "input_text", "text": json.dumps({"messages": messages}, ensure_ascii=False)}]}],
            reasoning={"effort": self.settings.reasoning_effort},
            store=not self.settings.disable_response_storage,
        )
        return response.output_text

    def generate_json(self, instruction: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Compatibility API retained for the existing QA pipeline."""
        return json.loads(self.generate_raw_text([{"role": "user", "content": {"instruction": instruction, **payload}}]))
