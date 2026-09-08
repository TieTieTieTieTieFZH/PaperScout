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
    qa_context_window: int = 128_000

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
            qa_context_window=int(os.getenv("QA_LLM_CONTEXT_WINDOW", str(cls.qa_context_window))),
        )


class MockLLM:
    """No-network provider used by tests and local development."""

    def generate_raw_text(self, messages: list[dict[str, Any]]) -> str:
        """Return a deterministic five-section Markdown summary for local tests."""
        text = "\n".join(str(message.get("content", "")) for message in messages)
        if "PaperScout 的只读 QA Agent" in text:
            tool_messages = [message for message in messages if message.get("role") == "tool"]
            if not tool_messages:
                return json.dumps(
                    {
                        "type": "tool_call",
                        "id": "qa-read-overview",
                        "name": "read_project_file",
                        "arguments": {"path": "wiki/indexes/overview.md"},
                    },
                    ensure_ascii=False,
                )
            latest = tool_messages[-1].get("content", {})
            if not isinstance(latest, dict) or not latest.get("ok"):
                return self._qa_insufficient()
            path = str(latest.get("path", ""))
            if path == "wiki/indexes/overview.md":
                match = re.search(r"(?:\.\./)?(papers/[^)\]\s]+\.md)", str(latest.get("content", "")))
                if not match:
                    return self._qa_insufficient()
                return json.dumps(
                    {
                        "type": "tool_call",
                        "id": "qa-read-paper",
                        "name": "read_project_file",
                        "arguments": {"path": f"wiki/{match.group(1)}"},
                    },
                    ensure_ascii=False,
                )
            evidence_ids = latest.get("evidence_ids", [])
            if not evidence_ids:
                return self._qa_insufficient()
            evidence_id = str(evidence_ids[0])
            paper_id = evidence_id.rsplit(":s", 1)[0]
            return json.dumps(
                {
                    "type": "final",
                    "answer": (
                        "论文 Wiki 中记录了可由原文证据核查的方法信息。 "
                        f"[paper:{paper_id}] [evidence:{evidence_id}]"
                    ),
                    "claims": [
                        {
                            "text": "论文 Wiki 中记录了可由原文证据核查的方法信息。",
                            "type": "paper_fact",
                            "paper_ids": [paper_id],
                            "evidence_ids": [evidence_id],
                        }
                    ],
                    "cited_evidence_ids": [evidence_id],
                    "status": "answered",
                    "memory_patch": {"evidence_ids": [evidence_id]},
                },
                ensure_ascii=False,
            )
        if "Wiki Review Chat Client" in text:
            return "VERDICT: APPROVE\n\n未发现需要修改的问题。"
        evidence_ids = re.findall(r"<!-- evidence:([^\s|]+)", text)
        if not evidence_ids:
            return ""
        citation = f"[evidence:{evidence_ids[0]}]"
        return "\n\n".join([
            "## 研究问题\n\n论文研究一个可由原文证据核查的问题。\n\n" + citation,
            "## 核心思路\n\n论文提出了可验证的方法或系统思路。\n\n" + citation,
            "## 方法\n\n方法依据论文原文组织处理流程。\n\n" + citation,
            "## 实验概况\n\n论文报告了与研究问题相关的评估设计。\n\n" + citation,
            "## 结论与局限\n\n结论应限于论文报告的范围。\n\n" + citation,
        ])

    @staticmethod
    def _qa_insufficient() -> str:
        return json.dumps(
            {
                "type": "final",
                "answer": "当前可读取资料不足，无法形成有 Evidence 支持的回答。",
                "claims": [],
                "cited_evidence_ids": [],
                "status": "insufficient_evidence",
                "memory_patch": {},
            },
            ensure_ascii=False,
        )

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
