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

`content_list.json` 是 Ingest 的唯一权威来源。宿主以 `type: text`、`text_level: 2` 标题开始一个 section evidence，并按标题原始数组下标生成 `<paper_id>:sNNNN`；跳过页眉或页脚等噪声块不会改变 ID。

宿主将完整 section evidence 转换为可引用 Markdown，并一次性提供给无工具 Ingest Chat Client。第一版使用简单前缀预算；任何截断都会失败关闭，避免在缺少后部实验或结论时发布完整性不明的五栏 Wiki。`full.md` 仅供人工阅读，不作为 citation 来源。

## 3. Wiki 文件

```text
wiki/
├── evidence/
│   └── {paper_id}/
│       ├── {section_id}.md
│       └── {section_id}.json
├── papers/
│   └── {paper_id}.md
├── indexes/
│   ├── overview.md
│   └── sources.json
└── health/
    └── latest-report.md
```

- `evidence/{paper_id}/{section_id}.md`：QA 可直接读取的 section 原文；同名 JSON 保存严格结构、block index、页码和 bbox。
- `papers/{paper_id}.md`：固定五栏：研究问题、核心思路、方法、实验概况、结论与局限。每栏含 1–3 个 `[evidence:<id>]` 标记。
- `indexes/overview.md`：研究问题和核心思路入口；`sources.json` 保存机器可读路径和来源哈希。
- 不生成 `concepts/`、`concepts.json`、`chunks.jsonl`、claims 或 method components。

## 4. Ingest 校验与发布

Ingest Agent 直接输出 Markdown；宿主添加标题和元数据后写入 Wiki。发布前本地校验：

- 五个栏目按固定顺序且正文非空；
- 每栏有 1–3 个不重复的当前论文 evidence ID；
- raw 目录哈希在生成前后保持一致；
- sources 路径和摘要 evidence 标记可解析。

校验失败时由同一 Agent 修复一次；第二次失败不发布。旧 Wiki 不进入新流程；保留不可变 raw 并使用当前契约重新生成。

## 5. QA 证据链

```text
用户问题
  -> sources.json 中的 papers 检索（至多 3 篇）
  -> 解析命中摘要中的 evidence ID
  -> 从相应 section evidence JSON 加载原文记录
  -> QA Agent 输出答案、evidence ID、页码和 quote
  -> 本地校验 ID、页码和 quote
```

QA 不能引用未由命中摘要加载的 evidence。证据不足时，必须说明当前 Wiki 无法支撑该回答。

## 6. 运行记录

每个运行保存状态、事件、模型原始输出、校验错误和最终结果。发布以完整 staging Wiki 的原子替换完成，避免产生半更新的 Wiki。

LangGraph 运行时使用 `runtime/checkpoints.sqlite` 保存 thread checkpoint。Checkpoint、用户可读 Session 和审计事件是三个独立契约；当前阶段已建立 SQLite 生命周期与 `thread_id` 配置骨架，后续 Graph 节点逐步接入。
