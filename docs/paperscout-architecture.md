# PaperScout 架构设计

## 1. 项目目标

PaperScout 面向学术调研场景，使用 MinerU 将论文 PDF 解析为结构化内容，再由 LLM 编译为可持续维护的 LLM Wiki，并提供可溯源知识问答。

系统的核心约束：

- 只有三个 Agent：`Wiki Ingest`、`Retrieval QA`、`Review`。
- `Retrieval QA` 同时负责检索、上下文组织和答案生成，不再单独设置 Answer Composer。
- PDF 由 MinerU 解析；Agent 不直接读取 PDF 二进制。
- 只使用文件系统，不引入向量数据库、关系数据库或图数据库。
- 首版以 LangGraph 节点实现，后续可将节点替换为独立 Agent。

## 2. 总体流程

### 2.1 论文摄入

```text
PDF
  -> MinerU 提交与轮询
  -> raw/papers/{paper_id}/mineru/
  -> Wiki Ingest Agent
  -> Review Agent
  -> 通过：发布 wiki
  -> 不通过：退回 Wiki Ingest，最多重试一次
```

### 2.2 用户问答

```text
用户问题
  -> Retrieval QA Agent
       |- 检索 wiki
       |- 选择证据
       |- 生成带引用的答案
       `- 输出 claims + citations
  -> Review Agent
  -> 通过：返回答案
  -> 不通过：携带反馈重新调用 Retrieval QA，最多重试一次
```

MinerU 的解析任务是异步的，因此 `task_id` 和任务状态必须进入运行状态，以便程序重启后继续轮询而不是重复提交。

## 3. LLM Wiki 文件结构

```text
llmwiki/
├── raw/
│   └── papers/
│       └── {paper_id}/
│           ├── source.pdf
│           ├── metadata.json
│           └── mineru/
│               ├── task.json
│               ├── full.md
│               ├── content_list.json
│               └── layout.json
│
└── wiki/
    ├── summaries/
    │   └── {paper_id}.md
    ├── concepts/
    │   └── {concept_id}.md
    ├── evidence/
    │   └── {paper_id}.jsonl
    ├── indexes/
    │   ├── sources.json
    │   ├── concepts.json
    │   └── chunks.jsonl
    └── health/
        └── latest-report.md
```

### 3.1 `raw/papers/{paper_id}/source.pdf`

未经修改的原始论文。它是证据链的根节点，LLM 不得覆盖或修改。

### 3.2 `raw/papers/{paper_id}/metadata.json`

保存 `paper_id`、标题、作者、年份、DOI、来源 URL、文件 hash 和导入时间，用于论文身份识别和版本变化检测。

### 3.3 `raw/papers/{paper_id}/mineru/task.json`

保存 MinerU 的 `task_id`、`trace_id`、模型版本、任务状态、提交/完成时间和结果 URL。它支持异步解析任务的恢复。

### 3.4 `raw/papers/{paper_id}/mineru/full.md`

MinerU 生成的完整 Markdown，供人工阅读和 Wiki Ingest Agent 使用。

### 3.5 `raw/papers/{paper_id}/mineru/content_list.json`

MinerU 生成的内容块列表。每个块应保留块编号、页码、章节、类型和文本，用于生成稳定的 `evidence_id`。

### 3.6 `raw/papers/{paper_id}/mineru/layout.json`

保存文本块、表格、图片和公式的版面信息。首版主要用于保留解析结果，后续可支持图表和公式定位。

### 3.7 `wiki/summaries/{paper_id}.md`

单篇论文的 LLM 编译卡片，固定包含 Research Question、Main Contribution、Method、Experimental Findings、Limitations、Key Claims 和 Related Concepts。它是人类可读的论文知识摘要，但不能替代原始证据。

### 3.8 `wiki/concepts/{concept_id}.md`

跨论文概念卡片，记录概念定义、关联论文、不同观点、共同方法、论文冲突、开放问题和来源证据，支持跨论文比较和概念问答。

### 3.9 `wiki/evidence/{paper_id}.jsonl`

论文级原子证据记录，每行一个证据：

```json
{
  "evidence_id": "paper_001:e12",
  "paper_id": "paper_001",
  "page": 3,
  "content_index": 42,
  "section": "Method",
  "quote": "原始论文中的精确文本",
  "raw_file": "raw/papers/paper_001/source.pdf",
  "mineru_file": "raw/papers/paper_001/mineru/content_list.json"
}
```

它将 Wiki 中的理解绑定到 MinerU 原始解析结果，并作为问答引用的最终目标。

### 3.10 `wiki/indexes/sources.json`

论文来源目录，记录论文 ID、标题、作者、年份、摘要路径、证据路径和处理状态。它是可重建的派生索引。

### 3.11 `wiki/indexes/concepts.json`

概念目录，记录概念名称、别名、相关论文和概念卡片路径。它也是可重建的派生索引。

### 3.12 `wiki/indexes/chunks.jsonl`

检索用文本块索引。每个块指向摘要、概念或证据文件。首版使用关键词匹配，未来可替换为 BM25 或向量检索而不改变 Wiki 文件格式。

### 3.13 `wiki/health/latest-report.md`

知识库质量报告，检查失效的 `evidence_id`、缺失引用、重复论文、失效链接、孤立概念和缺失解析结果。

## 4. 三个 Agent

### 4.1 Wiki Ingest Agent

输入 MinerU 输出，生成论文摘要、原子 claims、证据记录、跨论文概念和检索索引。它不负责用户问答。

### 4.2 Retrieval QA Agent

读取 summaries、concepts、evidence 和 indexes，检索相关内容，生成答案，并将答案拆成带引用的原子 claims。它可以与用户多轮交互，但不能修改 `raw/` 和已发布的 `wiki/`。

答案必须使用结构化的 `answer`、`claims` 和 `citations` 字段，其中每个 citation 都指向 `evidence_id`、页码和原文 quote。

### 4.3 Review Agent

既审核 Wiki Ingest 的产物，也审核 Retrieval QA 的答案。它检查引用 ID、论文 ID、页码、quote 原文一致性以及 claim 是否被证据支持，返回 `supported`、`partially_supported` 或 `unsupported`，失败时生成返工意见。

## 5. LangGraph 与轻量 Harness

LangGraph 节点只有三个 Agent 节点：`wiki_ingest`、`retrieval_qa` 和 `review`。MinerU 提交、轮询、文件读写和索引更新是确定性工具，不称为 Agent。

轻量 Harness 定义 `AgentTask`、`RunContext`、`StepResult`、`Artifact`、`RunEvent` 和 `Hook`。LangGraph 负责节点执行、状态传递、条件路由和重试；Harness 负责任务输入输出、产物登记、事件、预算、错误和运行状态。

面试表述：借鉴 Pi 的事件流、任务边界、artifact 和 hook 思想，在 LangGraph 之上实现领域级轻量 Harness；没有重复实现底层图调度和 checkpoint 引擎。

## 6. 运行状态与 checkpoint

```text
runs/{run_id}/
├── state.json
├── events.jsonl
├── artifacts.json
└── result.json
```

- `state.json`：当前可恢复状态，包括当前节点、下一节点、MinerU task、答案、证据、审核结果、重试次数和产物路径。
- `events.jsonl`：追加式运行历史，包括节点开始/结束、MinerU 状态变化、文件写入、审核失败和重试事件。
- `artifacts.json`：记录产物类型、路径和 hash。
- `result.json`：最终运行状态、审核结论和答案路径。

不保存 API Token、完整 PDF 二进制或每个 LLM token；这些内容保存在原始文件或环境变量中。

## 7. MVP 与演进

6 小时 MVP 只实现一到两篇论文、MinerU 接入、raw/wiki 分层、文件系统关键词检索、三 Agent 节点、证据引用、一次返工、checkpoint 和事件日志。

后续可以增加 BM25/向量索引、OCR、批量论文发现、图表解析、人工审批、独立 Agent 进程和分布式运行，但不改变核心数据契约。

## 8. 核心设计原则

1. `raw/` 是不可变事实，`wiki/` 是可重建理解。
2. Wiki 摘要不能代替原始证据。
3. Retrieval QA 负责回答，Review 负责判定依据是否充分。
4. 所有重要 claim 都必须绑定 `paper_id + page + quote`。
5. 索引可以重建，原始论文和证据记录不能丢失。
6. 先统一 Agent 输入输出协议，再将 LangGraph 节点演进为独立 Agent。
