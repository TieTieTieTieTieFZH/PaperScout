# PaperScout

PaperScout 是一个基于文件系统的学术论文知识流水线。它接收论文 PDF 和 MinerU 解析结果，将原始资料保存到不可变的 `raw/` 层，生成可审阅的 `wiki/` 知识库，并支持基于证据引用的问答。

## 当前功能

- 支持 Python 3.11、`uv`、LangGraph 和 Pydantic。
- 包含三个流程节点：`wiki_ingest`、`retrieval_qa` 和 `review`。
- `run_ingest` 支持两种 MinerU 输入方式：
  - 传入 `mineru_dir` 或 `mineru_path`：使用本地 MinerU 解析结果；
  - 不传入 MinerU 目录：上传 `source_pdf` 到 MinerU 精准解析 API，轮询任务并导入返回的 ZIP 结果。
- 通过 JSONL 索引进行关键词检索，暂不依赖数据库或向量数据库。
- 测试默认使用确定性的 Mock LLM。
- 提供 OpenAI 兼容 Responses 接口适配器，但当前测试不会调用真实 LLM。
- 暂无命令行接口，Python API 是当前主要集成入口。

## 安装与测试

```powershell
uv python install 3.11
uv sync
uv run pytest
```

如果 `uv` 缓存目录存在本机权限或路径问题，也可以直接使用项目虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

真实论文集成测试在设置 `PAPERSCOUT_MINERU_FIXTURE` 后使用该路径；未设置时会检查开发环境中的默认路径，不存在则自动跳过。

## MinerU 精准解析配置

使用精准解析 API 前，需要设置 MinerU API Token：

```powershell
$env:MINERU_TOKEN = "<你的 MinerU API Token>"
```

Token 也可以通过 `mineru_token` 参数传入。项目不会将 Token 写入源码或解析产物。

未提供 `mineru_path` 和 `mineru_dir` 时，必须提供 `source_pdf`，程序会自动完成以下流程：

1. 向 MinerU 申请文件上传地址；
2. 使用签名地址上传本地 PDF；
3. 轮询精准解析任务状态；
4. 下载并解压解析结果 ZIP；
5. 将 `full.md`、`content_list.json` 及其他解析文件导入 `raw/` 层。

## Python API

### 使用本地 MinerU 结果

```python
from pathlib import Path
from paperscout.workflow import run_ingest, run_qa

workspace = Path("./paper-workspace")
run_ingest(
    workspace=workspace,
    source_pdf=Path("paper.pdf"),
    mineru_path=Path("mineru-output"),
    paper_id="paper_001",
    llm_mode="mock",
)

answer = run_qa(
    workspace=workspace,
    question="论文的主要贡献是什么？",
    llm_mode="mock",
)
```

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

其中 `mineru_dir` 仍然保留，用于兼容旧版本调用方式。`mineru_path` 和 `mineru_dir` 不能同时传入。

## 目录结构

```text
workspace/
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
│   ├── indexes/
│   ├── summaries/
│   └── concepts/
└── runs/
    └── {run_id}/
        ├── state.json
        └── events.jsonl
```
