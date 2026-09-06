# PaperScout 架构设计

## 1. 目标

PaperScout 将论文 PDF 经 MinerU 解析后，发布为可检索摘要和可回查原文证据。系统只使用文件系统；`raw/` 是不可变事实，`wiki/` 是可重建产物。

系统有两个 Agent：

- Wiki Ingest Agent：生成单篇、带证据标记的五栏摘要。
- Retrieval QA Agent：检索摘要，并只依据关联 evidence 回答。

## 2. 摄入流程

```text
PDF
  -> MinerU
  -> raw/papers/{paper_id}/mineru/content_list.json
  -> 宿主生成带 evidence ID 的可引用 Markdown
  -> Wiki Ingest Agent 输出五栏摘要 Markdown
  -> 本地校验、staging 审核与原子发布
```

`content_list.json` 是 Ingest 的唯一权威来源。宿主按原始数组下标生成 `<paper_id>:eNNNN`；跳过页眉或页脚等噪声块不会改变后续 ID。

宿主将内容块转换为仅在内存中存在的可引用 Markdown。短论文在首个模型请求中内联；长论文通过受限的 `mineru/citable-evidence.md` 虚拟路径分段读取。`full.md` 仅供人工阅读，不作为 citation 来源。

## 3. Wiki 文件

```text
wiki/
├── evidence/
│   └── {paper_id}.jsonl
├── summaries/
│   └── {paper_id}.md
├── indexes/
│   └── sources.json
└── health/
    └── latest-report.md
```

- `evidence/{paper_id}.jsonl`：每行一个非噪声 MinerU 内容块，包含 ID、页码、章节、类型、quote、原始块索引和原始文件定位。
- `summaries/{paper_id}.md`：固定五栏：研究问题、主要贡献、方法、实验发现、局限性。每栏含 1–3 个 `[evidence:<id>]` 标记。
- `indexes/sources.json`：唯一索引，保存论文元数据、摘要路径与 evidence 路径。
- 不生成 `concepts/`、`concepts.json`、`chunks.jsonl`、claims 或 method components。

## 4. Ingest 校验与发布

Ingest Agent 直接输出 Markdown；宿主添加标题和元数据后写入 Wiki。发布前本地校验：

- 五个栏目按固定顺序且正文非空；
- 每栏有 1–3 个不重复的当前论文 evidence ID；
- raw 目录哈希在生成前后保持一致；
- sources 路径和摘要 evidence 标记可解析。

校验失败时由同一 Agent 修复一次；第二次失败不发布。已有 Wiki 进入 staging 时，迁移会删除旧 `concepts/`、`concepts.json`、`chunks.jsonl`，并移除旧摘要末尾的“关键主张”栏目。

## 5. QA 证据链

```text
用户问题
  -> sources.json 中的 summaries 检索（至多 3 篇）
  -> 解析命中摘要中的 evidence ID
  -> 从相应 evidence JSONL 加载这些原文记录
  -> QA Agent 输出答案、evidence ID、页码和 quote
  -> 本地校验 ID、页码和 quote
```

QA 不能引用未由命中摘要加载的 evidence。证据不足时，必须说明当前 Wiki 无法支撑该回答。

## 6. 运行记录

每个运行保存状态、事件、模型原始输出、校验错误和最终结果。发布以完整 staging Wiki 的原子替换完成，避免产生半更新的 Wiki。