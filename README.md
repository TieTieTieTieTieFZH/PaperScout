# PaperScout

PaperScout 是一个基于文件系统的学术论文知识流水线。它接收论文 PDF 和 MinerU 解析结果，将原始资料保存到不可变的 `raw/` 层，并生成可审阅的 `wiki/` 知识库。基于证据引用的 QA 正在按 LangGraph 重构计划开发，当前尚无可运行 QA Graph。

## 当前功能

- 支持 Python 3.11、`uv` 和 Pydantic。
- 当前 Wiki Ingest 已由 LangGraph `StateGraph` 编排并使用 SQLite Checkpointer，包含确定性规则与独立 Wiki Review Chat Client；Retrieval QA、Session、Answer Review 和中断恢复仍在开发。
- 已实现 QA 将使用的宿主只读 `read_project_file`：只允许 `wiki/` 与 `raw/papers/`，执行严格参数、路径和读取预算校验；目前尚未接入 QA Agent Loop。
- `run_ingest` 支持两种 MinerU 输入方式：
  - 传入 `mineru_path`：使用本地 MinerU 解析结果；
  - 不传入 `mineru_path`：上传 `source_pdf` 到 MinerU 精准解析 API，轮询任务并导入返回的 ZIP 结果。
- 以论文 Wiki 入口和逐章节 Evidence 建立可追溯资料层，暂不依赖数据库或向量数据库。
- 测试默认使用确定性的 Mock LLM。
- 提供 OpenAI 兼容 Responses 接口适配器，但当前测试不会调用真实 LLM。
- 暂无命令行接口，Python API 是当前主要集成入口。

当前逐文件架构与真实完成边界见 [架构手册](docs/architecture-manual.md)，阶段状态和验证证据见 [重构进度台账](docs/refactor-progress.md)。

## 安装与测试

```powershell
uv python install 3.11
uv sync
uv run pytest
```

## `.env` 配置

项目会自动读取项目根目录下的 `.env` 文件。可以复制 `.env.example` 为 `.env`，然后填写真实配置：

```powershell
Copy-Item .env.example .env
```

`.env` 中可以配置 MinerU 和真实 LLM：

```dotenv
MINERU_TOKEN=<你的 MinerU API Token>
PAPERSCOUT_LLM_API_KEY=<你的 LLM API Key>
PAPERSCOUT_LLM_BASE_URL=https://ai.input.im/v1
PAPERSCOUT_LLM_MODEL=gpt-5.4-mini
PAPERSCOUT_LLM_REASONING_EFFORT=high
PAPERSCOUT_LLM_DISABLE_RESPONSE_STORAGE=true
PAPERSCOUT_LLM_WIRE_API=responses
INGEST_LLM_CONTEXT_WINDOW=128000
```

## MinerU 精准解析配置

使用精准解析 API 前，需要设置 MinerU API Token：

```powershell
$env:MINERU_TOKEN = "<你的 MinerU API Token>"
```

Token 也可以通过 `mineru_token` 参数传入。项目不会将 Token 写入源码或解析产物。

未提供 `mineru_path` 时，必须提供 `source_pdf`，程序会自动完成以下流程：

1. 向 MinerU 申请文件上传地址；
2. 使用签名地址上传本地 PDF；
3. 轮询精准解析任务状态；
4. 下载并解压解析结果 ZIP；
5. 将 `full.md`、`content_list.json` 及其他解析文件导入 `raw/` 层。

## 三种工作流

- **PDF → raw**：调用 `run_ingest(..., llm_mode="mock" | "real")` 并提供 PDF/MinerU 输入；它先将 PDF 与 MinerU 结果导入不可变的 `raw/`。
- **raw → Wiki**：调用 `run_ingest_from_raw()`；它只读取已有的 `raw/papers/{paper_id}/metadata.json` 与 `mineru/content_list.json`，不会调用 MinerU、复制 PDF、修改或删除 raw。
- **PDF → raw → Wiki**：`run_ingest()` 完成导入后复用与 `run_ingest_from_raw()` 相同的校验、渲染和发布流程。

宿主只使用当前论文的 `mineru/content_list.json`，按 `type: text`、`text_level: 2` 聚合 section evidence。Evidence ID 使用二级标题在原始数组中的下标，例如 `<paper_id>:s0042`。Ingest 是无工具 Chat Client；输入必须一次性完整装入预算，若简单前缀会发生截断则失败关闭，在失败结果和 Checkpoint 中记录结构化覆盖信息，不生成不完整 Wiki。

## Python API

### 使用本地 MinerU 结果

```python
from pathlib import Path
from paperscout.workflow import run_ingest

workspace = Path("./paper-workspace")
run_ingest(
    workspace=workspace,
    source_pdf=Path("paper.pdf"),
    mineru_path=Path("mineru-output"),
    paper_id="paper_001",
    llm_mode="mock",
)

```

### 从已存在 raw 生成 Wiki

```python
from pathlib import Path
from paperscout.workflow import run_ingest_from_raw

run_ingest_from_raw(
    workspace=Path("./paper-workspace"),
    paper_id="paper_001",
    llm_mode="real",
)
```

如果同一 `paper_id` 已有 Wiki、evidence 或 source index，调用会在请求 LLM 前拒绝执行，避免覆盖已发布 Wiki。每次运行都会保存最终 `ingest-summary.md`，或失败时的原始输出、校验错误和事件记录。候选先通过本地结构规则，再由独立、无工具、无 Session 的 Wiki Review 审核；`REVISE/REJECT` 最多驱动一次完整重生成，非法 verdict 或超过最大次数均失败关闭。每次语义审核的 request、response 和 result 保存在 `runs/{run_id}/review/wiki/{attempt}/`。最后还会对完整 staging Wiki 执行确定性审核，任一审核失败都不会向 `wiki/` 发布文件。

旧 Wiki 不提供兼容迁移；保留不可变 `raw/`，使用当前 Ingest 重新生成 Wiki。

### 自动调用 MinerU 精准解析

```python
from pathlib import Path
from paperscout.workflow import run_ingest

run_ingest(
    workspace=Path("./paper-workspace"),
    source_pdf=Path("paper.pdf"),
    mineru_path=None,
    paper_id="paper_001",
    llm_mode="mock",
)
```


## llmwiki 目录结构

```text
llmwiki/
├── raw/
│   └── papers/{paper_id}/
│       ├── source.pdf
│       ├── metadata.json
│       └── mineru/
│           ├── full.md
│           ├── content_list.json
│           └── task.json
├── wiki/
│   ├── evidence/
│   │   └── {paper_id}/
│   │       ├── {section_id}.md
│   │       └── {section_id}.json
│   ├── indexes/
│   │   ├── overview.md
│   │   └── sources.json
│   ├── papers/
│   │   └── {paper_id}.md
│   └── health/
├── runtime/
│   └── checkpoints.sqlite
└── runs/
    └── {run_id}/
        ├── events.jsonl
        ├── result.json
        ├── review/wiki/{attempt}/
        └── staging/wiki/
```
