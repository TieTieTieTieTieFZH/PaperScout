from __future__ import annotations

from typing import Any


INGEST_SYSTEM_PROMPT = """你是 PaperScout 的 Ingest Chat Client。

你只负责把当前论文的可引用原文编写成简洁、可追溯的 Markdown 摘要。

规则：
- 只能使用宿主提供的当前论文材料；不得写文件、选择路径、访问其他论文或调用未声明工具。
- 原文块前的 evidence ID、页码和章节由宿主生成。不得编造、修改或跨论文引用 ID。
- 最终输出必须且只能是 Markdown：研究问题、核心思路、方法、实验概况、结论与局限五个二级标题，顺序固定。
- 每个栏目写一段简洁综合，并在段末写 1–3 个 [evidence:<ID>] 标记；优先 1–2 个最直接的证据。
- 不输出 concepts、claims、method_components、JSON、代码围栏、标题元数据或解释文字。
- 若作者未明确报告局限性，说明“论文未明确报告局限性”，并引用结论或讨论证据。
- 不调用工具。输入不足时不得补写未提供章节的事实。"""


def build_ingest_user_prompt(*, paper: dict[str, Any], citable_document: str) -> str:
    return (
        f"请为论文《{paper.get('title', '未知标题')}》生成最终摘要。以下是唯一可引用的论文原文。\n\n"
        f"--- 可引用论文原文开始 ---\n{citable_document}\n--- 可引用论文原文结束 ---"
    )


def build_ingest_repair_prompt(*, raw_output: str, validation_error: str, citable_document: str) -> str:
    return (
        "以下 Markdown 摘要未通过本地校验。只输出修正后的最终 Markdown，不要解释。\n\n"
        f"校验错误：{validation_error}\n\n--- 待修复摘要 ---\n{raw_output}\n"
        + f"--- 可引用论文原文 ---\n{citable_document}"
    )
