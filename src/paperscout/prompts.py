from __future__ import annotations

from typing import Any


INGEST_SYSTEM_PROMPT = """你是 PaperScout 的 Ingest Agent。

你只负责把当前论文的可引用原文编写成简洁、可追溯的 Markdown 摘要。

规则：
- 只能使用宿主提供的当前论文材料；不得写文件、选择路径、访问其他论文或调用未声明工具。
- 原文块前的 evidence ID、页码和章节由宿主生成。不得编造、修改或跨论文引用 ID。
- 最终输出必须且只能是 Markdown：研究问题、主要贡献、方法、实验发现、局限性五个二级标题，顺序固定。
- 每个栏目写一段简洁综合，并在段末写 1–3 个 [evidence:<ID>] 标记；优先 1–2 个最直接的证据。
- 不输出 concepts、claims、method_components、JSON、代码围栏、标题元数据或解释文字。
- 若作者未明确报告局限性，说明“论文未明确报告局限性”，并引用结论或讨论证据。
- 仅在宿主未内联全文时，可通过 read_raw 读取 mineru/citable-evidence.md；工具调用必须是 assistant JSON 消息。"""


def build_ingest_user_prompt(
    *, paper: dict[str, Any], citable_document: str | None, per_read_chars: int, total_chars: int, max_raw_reads: int
) -> str:
    if citable_document is not None:
        return (
            f"请为论文《{paper.get('title', '未知标题')}》生成最终摘要。以下是唯一可引用的论文全文。\n\n"
            f"--- 可引用论文全文开始 ---\n{citable_document}\n--- 可引用论文全文结束 ---"
        )
    return (
        f"请为论文《{paper.get('title', '未知标题')}》生成最终摘要。全文未内联；请先读取 "
        "mineru/citable-evidence.md。\n\n"
        f"单次最多读取 {per_read_chars} 字符，总计最多 {total_chars} 字符，最多 {max_raw_reads} 次读取。"
    )


def build_ingest_repair_prompt(*, raw_output: str, validation_error: str, citable_document: str | None) -> str:
    return (
        "以下 Markdown 摘要未通过本地校验。只输出修正后的最终 Markdown，不要解释。\n\n"
        f"校验错误：{validation_error}\n\n--- 待修复摘要 ---\n{raw_output}\n"
        + (f"--- 可引用论文全文 ---\n{citable_document}" if citable_document is not None
           else "全文未内联；如需核对 evidence ID，请读取 mineru/citable-evidence.md。")
    )


INGEST_BUDGET_EXHAUSTED_PROMPT = "读取额度已耗尽。现在必须仅输出符合固定五栏格式的最终 Markdown 摘要。"
