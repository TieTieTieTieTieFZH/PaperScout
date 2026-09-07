# PaperScout 当前架构手册

> 基线：本手册描述 LangGraph 重构进行中的当前 `main` 实现。它不把 TODO 中的目标能力写成已完成功能。进度与验证状态见 [重构进度台账](./refactor-progress.md)。

## 1. 系统定位与边界

PaperScout 是一个本地、单用户、单进程、文件系统优先的论文知识流水线。当前可运行闭环是：导入 PDF/MinerU 结果、构造章节级 Evidence、调用 Ingest Chat Client 生成五栏 Wiki、执行确定性校验并原子发布。

当前明确边界：

- `raw/` 是不可变事实层，只能由导入阶段首次建立；重建 Wiki 不得修改它。
- `wiki/` 是可重建发布层；旧 Wiki 不做兼容迁移，可从保留的 raw 重新生成。
- `runs/` 是单次运行的审计层，保存事件、结果、模型原始输出和失败信息。
- `runtime/checkpoints.sqlite` 是 LangGraph 执行位置的持久化层；它与用户可读 Session、审计事件是不同契约。
- 当前只有 Ingest 具备可运行实现；QA Graph、Session 持久化、上下文压缩和 Answer Review 尚未完成。
- 当前 Ingest 已由真正的 LangGraph `StateGraph` 编排，并通过 `GraphRuntime` 将各节点状态写入 SQLite Checkpointer；独立 Wiki Review 已接入，尚未实现中断后续跑。

## 2. 当前组件与数据流

```text
本地 MinerU 目录 ─┐
                  ├─ importer.py ──> raw/papers/{paper_id}/
PDF + MinerU API ─┘                         │
                                            ▼
                                      evidence.py
                                 SectionEvidenceBundle
                                            │
                                            ▼
                     prompts.py + llm.py（无工具 Ingest Chat Client）
                                            │
                                            ▼
                                 wiki.py 解析 WikiCandidate
                                            │
                                            ▼
                       review.py + 独立 Wiki Review Chat Client
                                 │ APPROVE       │ REVISE/REJECT
                                 │               └──> 完整重生成（最多一次）
                                 ▼
                       raw hash 再校验 ─────┤
                                            ▼
                               storage.py staging 目录
                                            │
                                            ▼
                              health.py 确定性规则审核
                                            │
                                通过 ───────┴─────── 失败
                                 │                    │
                                 ▼                    ▼
                        原子发布到 wiki/       不发布并记录失败
```

`workflow.py` 将上述组件注册为显式 LangGraph 节点和条件边。结构规则失败可进入一次修复；语义 `REVISE/REJECT` 可在最大生成次数内驱动完整重生成。任何节点错误、非法 verdict、超过次数或 staging 审核拒绝都会进入统一失败终态。只有语义 Review 与 staging 规则均通过，且发布前再次核对 raw、候选哈希和 staging 后，才能进入发布节点。运行过程同时追加严格的 `WorkflowEvent`。

## 3. 工作区数据布局

```text
workspace/
├── raw/
│   └── papers/{paper_id}/
│       ├── source.pdf
│       ├── metadata.json
│       └── mineru/
│           ├── content_list.json
│           ├── full.md
│           └── task.json
├── wiki/
│   ├── papers/{paper_id}.md
│   ├── evidence/{paper_id}/{section_id}.md
│   ├── evidence/{paper_id}/{section_id}.json
│   ├── indexes/overview.md
│   ├── indexes/sources.json
│   └── health/latest-report.md
├── runs/{run_id}/
│   ├── events.jsonl
│   ├── result.json
│   ├── ingest-output-*.md
│   ├── ingest-validation-error-*.txt
│   ├── review/wiki/{attempt}/
│   │   ├── request.md
│   │   ├── response.md
│   │   └── result.json
│   └── staging/wiki/...
└── runtime/
    └── checkpoints.sqlite
```

说明：

- `content_list.json` 是自动构造 citation Evidence 的权威输入；`full.md` 只供人工阅读。
- Evidence ID 为 `{paper_id}:s{二级标题在 content_list 中的原始下标，四位补零}`。跳过噪声块不会重编号。
- `papers/{paper_id}.md` 固定为“研究问题、核心思路、方法、实验概况、结论与局限”五栏。
- `events.jsonl` 是审计事件，不是恢复 Checkpoint；当前不再生成自定义 `runs/{run_id}/state.json`。
- 每次 Ingest 都通过 `GraphRuntime` 运行，并将 JSON 原生 Graph State 保存到 `checkpoints.sqlite`；当前可在 runtime 重建后读取终态，但尚未提供中断后续跑 API。

## 4. 运行时契约

### 4.1 Evidence

`models.py` 中的 `EvidenceBlock`、`SectionEvidence`、`EvidenceExtractionReport` 和 `SectionEvidenceBundle` 约束章节边界、原始 block 下标、页码、bbox、文本和解析质量。`evidence.py` 只在 `type=text` 且 `text_level=2` 时开始新章节；找不到二级章节会直接失败并给出质量报告。

References 章节仍保存在 Evidence 中以保证 raw 可追溯，但标为不允许 Ingest 引用。一个被保留的 block 必须且只能属于一个章节。

### 4.2 Wiki

`WikiCandidate` 只接受固定五栏、固定顺序和当前输入拥有的 Evidence ID。每栏要求 1–3 个互不重复的 citation。模型只输出候选 Markdown；宿主负责解析、补充元数据、写文件、审核与发布。

### 4.3 Review

`ReviewVerdict` 和 `ReviewDecision` 定义 `APPROVE`、`REVISE`、`REJECT` 契约。Wiki Review 是与 Ingest Provider 隔离的无工具、无 Session Chat Client，只接收完整候选和候选实际引用的 section evidence；宿主只解析严格的第一行 verdict。`APPROVE` 进入 staging，`REVISE/REJECT` 在次数允许时进入完整重生成，非法输出或超过次数进入失败终态。Answer Review 尚未实现。

### 4.4 Graph State 与 Checkpoint

`IngestGraphState` 和 `QAGraphState` 是严格 Pydantic 状态契约。`GraphRuntime` 使用 SQLite Saver，并要求非空 `thread_id`。Ingest 使用 `IngestGraphState` 定义节点输入，并以 JSON 原生值写入 Checkpoint；成功、覆盖不足和规则审核拒绝的终态均已验证可在 runtime 重建后读取。QA Graph 和通用中断恢复仍不存在。

### 4.5 Session 与读取工具

`SessionState`、`SessionMessage`、`ProjectMemory`、`ReadBudget`、`ReadResourceRecord`、`AgentToolCall` 与 `ReadProjectFileArguments` 已定义。它们目前只是契约：尚未实现 `messages.jsonl`、Session `state.json`、`summary.md`、压缩策略或真正的 `read_project_file` 工具。

### 4.6 事件

`WorkflowEvent` 强制携带运行关联信息并限制事件类型。当前 Ingest 写入 `run.started`、模型事件及终态事件等 JSONL 记录；完整的 QA、Tool、Review、Compaction 事件链及事件回放仍未完成。

## 5. Ingest 的实际执行顺序

1. `run_ingest()` 选择使用本地 MinerU 结果，或调用 MinerU API 解析 PDF；随后通过 Importer 建立 raw。
2. `run_ingest_from_raw()` 复用已有 raw，不调用 MinerU，也不复制或改写 PDF。
3. 工作流拒绝覆盖已有论文 Wiki、Evidence 或 source index。
4. 记录 raw 文件哈希，读取 `metadata.json` 和 `content_list.json`。
5. 按二级标题构造 section evidence，并生成供模型一次性读取的 citable Markdown。
6. 采用简单前缀预算；若完整输入放不下，直接失败，不调用模型、不发布 Wiki。
7. `ingest_agent` 生成固定五栏 Markdown，`validate_wiki` 解析；失败且未达到最大次数时经条件边进入 `repair_ingest`，只修复一次。
8. `verify_raw` 再次校验 raw 哈希；若模型调用期间 raw 变化，进入 `fail_run`。
9. `wiki_review` 收集候选实际引用的 Evidence，保存审核请求，调用独立 Review Client 并严格解析 verdict。
10. `APPROVE` 进入 `render_wiki`；`REVISE/REJECT` 在未达到两次生成上限时进入 `revise_ingest`，随后重新执行完整规则与语义审核。
11. `render_wiki` 在 `runs/{run_id}/staging/wiki` 渲染论文页、Evidence、索引和健康报告。
12. `wiki_rules` 执行 staging 确定性审核；拒绝时进入 `fail_run`。
13. `verify_publish` 在发布前再次检查 raw 哈希、候选哈希和 staging；任一变化都不发布。
14. `publish_wiki` 原子替换正式 `wiki/` 并保存成功终态；其他路径统一保存失败终态。

## 6. 每个代码与工程文件的作用

### 6.1 生产代码

| 文件 | 作用 | 当前完成边界 |
| --- | --- | --- |
| `src/paperscout/__init__.py` | 包入口，导出工作区辅助函数和两个 Ingest API。 | 不再导出旧 QA/迁移入口。 |
| `src/paperscout/models.py` | 所有跨模块 Pydantic 契约：论文元数据、Evidence、Wiki、Review、Session、预算、Graph State、Tool Call、事件。 | Session、QA、Review 中部分模型先于执行逻辑存在。 |
| `src/paperscout/evidence.py` | 将 MinerU `content_list.json` 转换为稳定、可定位、带质量报告的 section evidence。 | 已接入 Ingest；缺失二级标题时失败关闭。 |
| `src/paperscout/importer.py` | 导入本地预解析 MinerU 目录和 PDF，推断论文元数据，并建立不可变 raw 目录。 | 只负责来源层，不生成 Wiki。 |
| `src/paperscout/mineru.py` | MinerU 精准解析 HTTP 客户端：申请上传、上传 PDF、轮询、下载 ZIP、安全解压和定位结果。 | 属于保留的外部解析边界；离线测试不访问网络。 |
| `src/paperscout/prompts.py` | 定义 Ingest、规则修复、Review 驱动重生成和 Wiki Review 的系统/用户提示。 | Ingest 与 Wiki Review 均无工具、无 Session；尚无 QA/Answer Review prompt。 |
| `src/paperscout/llm.py` | LLM 适配层：从环境读取设置、确定性 `MockLLM`、OpenAI-compatible Responses 客户端。 | Ingest 与 Review 使用独立实例；旧 QA 特定方法已删除。 |
| `src/paperscout/review.py` | 严格解析 Review 第一行状态，并把自然语言意见转换为 `ReviewDecision`。 | Wiki Review 已接入；Answer Review 后续复用同一机制。 |
| `src/paperscout/wiki.py` | 渲染模型输入、解析/校验五栏候选、生成论文 Wiki、读写 section evidence、更新规范索引。 | 不含旧 chunks/claims/concepts 或迁移兼容逻辑。 |
| `src/paperscout/health.py` | 对 staging Wiki 执行确定性结构/引用规则并生成健康报告。 | 不是语义 Review Agent。 |
| `src/paperscout/storage.py` | 文件系统路径、JSON/哈希、严格事件追加、staging 准备、带回滚的原子发布、结果写入和安全复制。 | Checkpoint 不存于此；旧自定义运行状态文件已删除。 |
| `src/paperscout/graph_runtime.py` | 创建 SQLite LangGraph Checkpointer、生成 `thread_id` 配置并编译任意 `StateGraph`。 | 已编译实际 Ingest 图；QA 图和中断恢复尚未接入。 |
| `src/paperscout/workflow.py` | 定义并运行 Ingest `StateGraph`：上下文、模型生成/规则修复、语义 Wiki Review/重生成、两次 raw 不变性校验、staging、规则审核、发布和统一失败终态。 | P0-B 已形成完整本地闭环；中断恢复后续实现。 |

### 6.2 自动化测试与人工验证脚本

| 文件 | 作用 | 是否自动运行 |
| --- | --- | --- |
| `tests/test_evidence_contract.py` | 验证稳定 Evidence ID、章节归属、解析质量失败和严格参数契约。 | 是。 |
| `tests/test_p0a_contracts.py` | 验证 Wiki/Session/Graph/Event 契约，以及 SQLite Checkpoint 跨运行时恢复。 | 是。 |
| `tests/test_ingest_from_raw.py` | 验证 raw→Wiki 闭环、固定五栏、Evidence 产物、事件、一次修复、真实图节点、SQLite 终态、结构化覆盖报告及各种失败不发布。 | 是。 |
| `tests/test_review_contract.py` | 验证严格 verdict 第一行解析及未知、缺失或错位状态全部失败关闭。 | 是。 |
| `tests/fixtures/mineru_micro/content_list.json` | 最小确定性 MinerU fixture，覆盖二级章节和 Evidence 构造。 | 被自动测试读取。 |
| `tests/manual_raw_to_wiki.py` | 使用本机已有 raw 手动运行真实或 Mock Ingest。 | 否，需人工调用。 |
| `tests/manual_mineru_raw.py` | 手动调用 MinerU API，把本地 PDF 解析并导入 raw。 | 否；含本机示例路径，需按环境修改。 |
| `tests/evaluate_ingest_agent.py` | 对真实 LLM 重复执行 Ingest 并汇总通过率，同时检查 fixture 未被修改。 | 否；需要真实模型配置。 |

### 6.3 工程、CI 与文档

| 文件 | 作用 |
| --- | --- |
| `pyproject.toml` | Python 包元数据、运行依赖、开发依赖、构建后端与 pytest 配置。 |
| `uv.lock` | 锁定全部直接和传递依赖，保证 `uv sync --frozen` 可复现。 |
| `.github/workflows/ci.yml` | Windows CI；对 main push 和 PR 执行冻结依赖同步及完整离线 pytest。 |
| `AGENTS.md` | 仓库级 Agent 开发约束：规定文档阅读顺序、测试先行、进度更新、系统边界和遇到歧义先询问用户。 |
| `README.md` | 面向使用者的能力概览、配置、Python API 和工作区布局。 |
| `docs/paperscout-architecture.md` | 较短的设计概览；部分章节描述目标态，详细当前态以本手册为准。 |
| `docs/architecture-manual.md` | 当前文件；面向维护者的逐模块架构与真实完成边界。 |
| `docs/refactor-progress.md` | 唯一实施进度和验证台账；功能提交必须同步更新。 |
| `docs/todo/project-experience-langgraph-refactor-plan.md` | 总体目标、阶段路线和简历表述验收映射，不代表已经完成。 |
| `docs/todo/wiki-progressive-loading-plan.md` | Wiki/Evidence/Ingest 的目标设计。 |
| `docs/todo/qa-agent-progressive-reading-plan.md` | QA 渐进读取、预算、Session 和记忆的目标设计。 |
| `docs/todo/review-chat-client-plan.md` | Wiki Review 与 Answer Review 的目标设计。 |

## 7. 失败关闭与数据安全

以下任一情况都会阻止发布：MinerU 缺少二级标题、上下文需要截断、模型 Markdown 无法在一次修复内满足契约、Evidence ID 不属于当前输入、raw 哈希发生变化、staging 规则审核失败或发布过程异常。

发布使用完整 staging Wiki 和可回滚的目录替换，避免只更新一部分正式 Wiki。已存在的论文 Wiki 默认不覆盖；若要重建，操作者应只清理可重建 Wiki 产物，保留 raw。

## 8. 当前缺口与推荐实现顺序

当前最大架构缺口不是数据模型，而是“模型已经定义、执行图尚未接线”：

1. 实现受控的 `read_project_file`，覆盖路径权限、符号链接、单次与累计预算；
2. 建立 QA Agent Loop 和 Wiki→Evidence→raw 渐进读取；
3. 为 Ingest/QA 增加明确的中断后续跑入口和幂等副作用策略；
4. 接入 Session 文件、上下文压缩和长期项目记忆；
5. 增加 Answer Review；
6. 补齐完整事件流、回放和真实模型评测。

每一步都应先添加 fixture 和关键契约测试，再实现功能，并在同一提交中更新 `docs/refactor-progress.md`。

## 9. 本地验证命令

```powershell
.venv\Scripts\python.exe -m pytest -q
uv --no-cache lock --check
git diff --check
```

离线 pytest 证明当前自动测试覆盖的行为没有回归，但 Mock Review 只验证控制流和契约，不能证明真实语义审核质量；也不能证明尚未实现的 QA、Session、Answer Review 或中断恢复已经完成。真实 MinerU/LLM 只通过对应人工脚本单独验证，不能混入离线基线结论。
