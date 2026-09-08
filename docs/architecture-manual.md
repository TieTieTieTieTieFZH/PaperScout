# PaperScout 当前架构手册

> 基线：本手册描述 LangGraph 重构进行中的当前 `main` 实现。它不把 TODO 中的目标能力写成已完成功能。进度与验证状态见 [重构进度台账](./refactor-progress.md)。

## 1. 系统定位与边界

PaperScout 是一个本地、单用户、单进程、文件系统优先的论文知识流水线。当前有两个可运行闭环：Ingest 从 PDF/MinerU 构造 Evidence、生成并审核发布五栏 Wiki；QA 通过受控只读工具渐进读取 Wiki、Evidence 与 raw，输出经过确定性引用校验和独立语义审核的结构化回答。

当前明确边界：

- `raw/` 是不可变事实层，只能由导入阶段首次建立；重建 Wiki 不得修改它。
- `wiki/` 是可重建发布层；旧 Wiki 不做兼容迁移，可从保留的 raw 重新生成。
- `runs/` 是单次运行的审计层，保存事件、结果、模型原始输出和失败信息。
- `runtime/checkpoints.sqlite` 是 LangGraph 执行位置的持久化层；它与用户可读 Session、审计事件是不同契约。
- Ingest、QA Agent Loop、Checkpoint 中断续跑、用户可读 Session 文件、约 60% 窗口阈值的动态上下文压缩、资源哈希失效重读、文件型项目级长期记忆、只读用户 Profile，以及 Answer Review 的确定性规则、独立语义审核、审核驱动补读/重答和最大次数安全降级均已具备可运行实现。
- Ingest 与 QA 都由真正的 LangGraph `StateGraph` 编排；显式节点边界中断和瞬时模型/工具故障会保留可恢复 Checkpoint，并由对应 `resume_*` API 继续。

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

```text
用户问题 → qa.py 的 QA Agent
                 │ tool_call JSON
                 ▼
          read_project_file
                 │ 精简 JSON 外壳 + 自然语言正文
                 └───────────────→ QA Agent（按需循环）
                                      │ final JSON
                                      ▼
                    Answer 规则与当前 Evidence 文件校验
                                      │
                                      ▼
                       独立 Answer Review Chat Client
                    │ APPROVE  │ REVISE/REJECT │ 非法
                    ▼          ▼               ▼
              返回结构化回答  QA 补读/重答    失败关闭
                               │ 达到上限
                               └──────→ 安全证据不足回答
```

QA Graph 的模型节点只接受严格的单对象 JSON 协议。工具内部完整结果包含哈希、预算与资源记录，并写入运行审计；模型只看到 `ok`、内容/资源、截断位置、Evidence ID 或结构化错误。工具错误可回填给模型修正，预算耗尽后模型只能基于已有证据回答或明确返回证据不足。

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
│   ├── qa-output-*.txt
│   ├── tools/{model_step}/
│   │   ├── request.json
│   │   └── result.json
│   ├── ingest-validation-error-*.txt
│   ├── review/wiki/{attempt}/
│   │   ├── request.md
│   │   ├── response.md
│   │   └── result.json
│   ├── review/answer/{attempt}/
│   │   ├── request.md
│   │   ├── response.md
│   │   └── result.json
│   └── staging/wiki/...
├── runtime/
│   └── checkpoints.sqlite
└── memory/
    ├── profile.json
    ├── projects/{project_id}/
    │   └── state.json
    └── sessions/{session_id}/
        ├── messages.jsonl
        ├── state.json
        └── summary.md
```

说明：

- `content_list.json` 是自动构造 citation Evidence 的权威输入；`full.md` 只供人工阅读。
- Evidence ID 为 `{paper_id}:s{二级标题在 content_list 中的原始下标，四位补零}`。跳过噪声块不会重编号。
- `papers/{paper_id}.md` 固定为“研究问题、核心思路、方法、实验概况、结论与局限”五栏。
- `events.jsonl` 是审计事件，不是恢复 Checkpoint；当前不再生成自定义 `runs/{run_id}/state.json`。
- 每次 Ingest 和 QA 都通过 `GraphRuntime` 运行，并将 JSON 原生 Graph State 保存到 `checkpoints.sqlite`；`resume_ingest()` 与 `resume_qa()` 使用返回的 `thread_id` 在新 runtime 中续跑。
- 用户或宿主可显式调用 `save_user_profile()` 原子维护 `memory/profile.json`；QA 每轮只加载其快照，不提供模型写回路径。完成的 QA 轮次把项目级结构化记忆原子写入 `memory/projects/{project_id}/state.json`；完整消息审计、Session 记忆/资源状态和摘要投影仍分别写入 Session 三文件。相同 `project_id` 的不同 Session 共享项目记忆，但不共享消息或已读资源正文。

## 4. 运行时契约

### 4.1 Evidence

`models.py` 中的 `EvidenceBlock`、`SectionEvidence`、`EvidenceExtractionReport` 和 `SectionEvidenceBundle` 约束章节边界、原始 block 下标、页码、bbox、文本和解析质量。`evidence.py` 只在 `type=text` 且 `text_level=2` 时开始新章节；找不到二级章节会直接失败并给出质量报告。

References 章节仍保存在 Evidence 中以保证 raw 可追溯，但标为不允许 Ingest 引用。一个被保留的 block 必须且只能属于一个章节。

### 4.2 Wiki

`WikiCandidate` 只接受固定五栏、固定顺序和当前输入拥有的 Evidence ID。每栏要求 1–3 个互不重复的 citation。模型只输出候选 Markdown；宿主负责解析、补充元数据、写文件、审核与发布。

### 4.3 Review

`ReviewVerdict` 和 `ReviewDecision` 定义 `APPROVE`、`REVISE`、`REJECT` 契约。Wiki Review 是与 Ingest Provider 隔离的无工具、无 Session Chat Client，只接收完整候选和候选实际引用的 section evidence；宿主只解析严格的第一行 verdict。`APPROVE` 进入 staging，`REVISE/REJECT` 在次数允许时进入完整重生成，非法输出或超过次数进入失败终态。Answer Review 同样是与 QA Provider 隔离、无工具、无 Session 的独立 Chat Client，只接收用户问题、完整回答和实际引用 Evidence；`APPROVE` 放行，`REVISE/REJECT` 以结构化审核消息驱动 QA 补读或完整重答，并重新执行规则与语义审核。初稿后最多修复两次；仍未通过时由宿主生成无 Claim、无引用、无记忆补丁的证据不足回答。非法 verdict 继续失败关闭。

### 4.4 Graph State 与 Checkpoint

`IngestGraphState` 和 `QAGraphState` 是严格 Pydantic 状态契约。`GraphRuntime` 使用 SQLite Saver，并要求非空 `thread_id`。Ingest 与 QA 都以 JSON 原生状态运行并写入 Checkpoint；显式 `interrupt_before`、瞬时节点故障后的新 runtime 续跑、完成终态重复 resume 均已验证。恢复前会核对 workspace、thread 和模型模式，避免把 Checkpoint 用到错误运行环境。

### 4.5 Profile、项目记忆、Session 与读取工具

`read_project_file` 是 QA 唯一的宿主控制纯只读工具：参数先经严格 Pydantic Schema 校验，路径必须是项目相对路径且只能解析到 `wiki/` 或 `raw/papers/`，拒绝绝对路径、`..`、越界路径和越界符号链接。文本与目录按字符预算返回并支持 offset，PDF/图片不返回二进制；有效调用和返回字符分别计入 `ReadBudget`。宿主保留含哈希与预算的完整结果，模型只接收精简 JSON 外壳。

`user_profile.py` 严格加载工作区内的 `memory/profile.json`；文件缺失时返回空 `UserProfile` 且不自动创建，未知字段或非普通文件直接失败。用户或宿主只能通过显式 `save_user_profile()` 原子写入研究方向、回答风格和引用偏好。QA Graph 把 Profile 快照放入模型上下文并明确标记为只读；最终回答 Schema 不接受 `profile_patch`，完成与恢复节点也不写 Profile。

`session.py` 校验 `session_id` 只能映射到工作区内的单个安全目录，并严格加载完整的三文件集合。加载已有 Session 时，宿主通过与读取工具相同的允许根目录和目录哈希语义重新计算每个资源的 SHA256；已变化、缺失或不再位于允许范围的记录从本轮状态移除，只由失效记录支撑的 Evidence ID 同步从记忆中移除。最近历史中的对应工具正文会替换为 `STALE_SESSION_RESOURCE`，完整 `messages.jsonl` 审计保持不变，模型提示必须重新读取。完整消息审计保留每个被拒候选及 Answer Review 结果；下一轮模型上下文按轮次只保留最终批准或安全降级的答案，过滤被拒草稿和历史审核控制消息。

`project_memory.py` 对 `project_id` 使用与 Session 相同的安全单路径分量约束；`run_qa()` 的可选关键字参数默认为 `default`。项目状态只保存严格的 `ProjectState` 和结构化 `ProjectMemory`，不保存消息、已读资源或工具正文。同一项目下的新 Session 会加载长期记忆；Session 首次写入后绑定其 `project_id`，后续不能切换项目。项目记忆写后故障保留在 `complete_qa` 之前的 Checkpoint，重复合并与写入是幂等的。

完成节点会先把经本轮 Evidence 校验的记忆合并到最新项目状态，再把 `SessionMessage` 全量审计写入 `messages.jsonl`，累积去重后的有效 `ReadResourceRecord`，并将不含 `messages` 字段的 `SessionState` 写入 `state.json`。`state.json` 不保存消息或工具正文。Session 写入异常同样保留在 `complete_qa` 之前的 Checkpoint，已写入的消息通过 message ID 幂等重放，恢复时不会重复消息或模型调用。

模型上下文加载只读用户 Profile、当前项目长期记忆、Session 摘要、尚未压缩的历史轮次和当前问题。`QA_LLM_CONTEXT_WINDOW` 独立配置 QA 模型的真实上下文窗口；宿主把完整下一轮消息序列化为 UTF-8 JSON，以每 3 字节约 1 token 的保守估算与 60% 阈值比较。低于阈值时保留全部未压缩历史；超过阈值时逐轮压缩最早前缀，同时始终至少保留最近四个已完成交互轮次。`SessionState.compacted_turns` 持久化单调前进的压缩边界，防止旧工具正文因后续窗口变大而重新进入模型。

压缩后的结构化 `summary.md` 保留项目记忆、有效资源的路径、offset、字符数、SHA256、Evidence ID，以及每个旧轮次的精简用户问题和最终回答；旧工具正文仍完整留在 `messages.jsonl`，但不会回流模型。摘要最多列出最近 20 个已压缩轮次，更老轮次只保留在完整审计中。上下文变换在模型调用前记录 `context.compacted` 事件，并携带窗口、阈值、估算 token、累计压缩轮数和保留轮数。该估算用于跨模型的确定性离线控制，不宣称等同于 Provider 的精确 tokenizer。

### 4.6 事件

`WorkflowEvent` 强制携带运行关联信息并限制事件类型。当前 Ingest 和 QA 均写入运行、模型、中断、恢复及终态事件，QA 还写入工具、上下文压缩、确定性 Answer 规则和语义 Answer Review 开始/完成事件；事件序号会在恢复时与磁盘记录对齐。流式消费与事件回放仍未完成。

## 5. 实际执行顺序

### 5.1 Ingest

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
15. 若在节点前显式中断或模型/Review Provider 瞬时失败，返回 `interrupted`、`thread_id` 和下一节点；`resume_ingest()` 从 SQLite Checkpoint 继续。
16. 模型与 Review 输出使用运行目录中的持久化结果避免重复调用；残缺 staging 会重建，发布前清单用于恢复目录替换的两个崩溃窗口并在篡改时回滚。

### 5.2 QA

1. `run_qa()` 加载只读用户 Profile，校验 `project_id` 与 `session_id`，加载项目长期记忆和绑定同一项目的用户可读 Session，根据 QA 窗口约 60% 阈值选择未压缩历史并至少保留最近四轮，建立独立运行与 `thread_id`，再把完整上下文计划写入 `QAGraphState` 并启动 Checkpointed `StateGraph`。
2. `qa_agent` 调用模型并严格解析单个 JSON 对象；合法动作只能是一次 `read_project_file` 调用或最终结构化回答。
3. `read_project_file` 执行 Schema、路径与预算校验，把完整结果写入 `runs/{run_id}/tools/`，只把精简结果回填模型。
4. 工具错误可回到 `qa_agent` 修正；工具成功会更新预算、已读资源和可用 Evidence，再由模型决定是否继续读取。
5. `answer_rules` 检查 Claim 类型、论文/Evidence 归属、跨论文覆盖、正文标记、引用汇总、引用是否真正出现在本轮工具结果中，以及每条实际引用能否定位到声明相同 ID 的当前 section Evidence 文件；用户明确索要原文或依据时，无引用的非“证据不足”答案被拒绝。规则节点只收集实际引用 Evidence，并记录其 SHA256。
6. `answer_review` 使用独立 Provider 和仅含 system/user 的一次性上下文审核用户问题、完整回答与实际引用 Evidence；逐次保存 request、response 和结构化 result，并把审核结论追加到完整 Session 消息审计。严格首行 `APPROVE` 进入返回门；非法 verdict 失败关闭。
7. `REVISE/REJECT` 在当前候选未达到三次总审核上限时进入 `revise_answer`，把反馈交给保留当前上下文的 QA；QA 可继续调用只读工具或完整重答，随后重新经过 `answer_rules` 与 `answer_review`。第三次仍不通过时进入 `safe_answer`，生成无事实 Claim、无 Evidence 引用和无记忆补丁的 `insufficient_evidence` 回答，并重新通过确定性规则。
8. `verify_answer_evidence` 在返回前重新核对上述 Evidence 哈希；变化、缺失或越界时失败关闭，不保存 Session。通过后才把最终答案的 `memory_patch` 幂等合并到最新项目记忆，再原子保存完整 Session 消息日志、合并后的记忆、资源元数据和本轮使用的压缩边界，最后写入运行结果并结束。非法 JSON、未知工具、本轮重复调用 ID、伪造引用或模型步数耗尽同样进入统一失败终态。
9. 节点边界中断或瞬时模型/工具/Answer Review 故障返回可恢复状态；`resume_qa()` 复用持久化模型、工具和审核结果，并保证读取预算不会重复计费。

## 6. 每个代码与工程文件的作用

### 6.1 生产代码

| 文件 | 作用 | 当前完成边界 |
| --- | --- | --- |
| `src/paperscout/__init__.py` | 包入口，导出工作区辅助函数、Ingest/QA 运行与恢复 API，以及用户 Profile 的模型和显式读写 API。 | 不再导出旧迁移入口。 |
| `src/paperscout/models.py` | 所有跨模块 Pydantic 契约：论文元数据、Evidence、Wiki、QA Answer/Claim、Review、Profile、项目状态、Session、预算、Graph State、Tool Call、事件。 | Answer Review 中部分模型先于执行逻辑存在。 |
| `src/paperscout/evidence.py` | 将 MinerU `content_list.json` 转换为稳定、可定位、带质量报告的 section evidence。 | 已接入 Ingest；缺失二级标题时失败关闭。 |
| `src/paperscout/importer.py` | 导入本地预解析 MinerU 目录和 PDF，推断论文元数据，并建立不可变 raw 目录。 | 只负责来源层，不生成 Wiki。 |
| `src/paperscout/mineru.py` | MinerU 精准解析 HTTP 客户端：申请上传、上传 PDF、轮询、下载 ZIP、安全解压和定位结果。 | 属于保留的外部解析边界；离线测试不访问网络。 |
| `src/paperscout/prompts.py` | 定义 Ingest、规则修复、Review 驱动重生成、Wiki/Answer Review 和严格 JSON QA 协议提示。 | 当前两类 Review profile 均已接入。 |
| `src/paperscout/llm.py` | LLM 适配层：从环境读取设置与独立 Ingest/QA 上下文窗口、确定性 `MockLLM`、OpenAI-compatible Responses 客户端。 | Mock 覆盖 Ingest、Wiki/Answer Review 和基础 QA 循环；真实语义质量未验证。 |
| `src/paperscout/review.py` | 严格解析 Review 第一行状态，并把自然语言意见转换为 `ReviewDecision`。 | Wiki Review 与 Answer Review 共用同一失败关闭机制。 |
| `src/paperscout/read_tool.py` | 实现 QA 唯一宿主只读工具：Schema、允许根目录、路径穿越/符号链接防护、文件类型、文本截断、资源哈希、读取预算和精简模型可见结果。 | 已接入 QA Graph；越界 Windows reparse point 已通过 junction 回退实测。 |
| `src/paperscout/answer_review.py` | 执行 Answer Review 确定性规则，安全收集候选实际引用的当前 section Evidence，并在返回前复核哈希。 | 规则与 Evidence 输入契约已完成；语义审核与修订由 QA Graph 编排。 |
| `src/paperscout/user_profile.py` | 严格加载并显式原子保存 `memory/profile.json`，缺失时返回空 Profile。 | 用户/宿主显式维护、QA 只读、跨项目加载和未知 `profile_patch` 拒绝已接入。 |
| `src/paperscout/project_memory.py` | 校验项目记忆路径，严格加载并原子保存 `memory/projects/{project_id}/state.json`。 | 可选 `project_id="default"`、跨 Session 共享、项目隔离和写后故障幂等恢复已接入。 |
| `src/paperscout/session.py` | 校验 Session 路径与项目绑定，严格加载/原子保存三文件契约，合并记忆与已读资源元数据，失效陈旧资源，投影无被拒草稿的模型历史，并构造无旧工具正文的结构化摘要。 | 用户可读文件、多轮继续、审核审计隔离、单调压缩边界、资源哈希失效和项目绑定已接入。 |
| `src/paperscout/qa.py` | 定义并运行 QA `StateGraph`：严格 JSON 模型动作、工具循环、审计、预算、结构化回答、Answer 双阶段审核、Review 驱动补读/重答、安全降级、Evidence 校验、项目/Session 记忆、多轮继续、上下文压缩、资源失效提示和 `resume_qa()`。 | P0-C2、P0-D1、P0-D2 与 P0-E 已完成。 |
| `src/paperscout/wiki.py` | 渲染模型输入、解析/校验五栏候选、生成论文 Wiki、读写 section evidence、更新规范索引。 | 不含旧 chunks/claims/concepts 或迁移兼容逻辑。 |
| `src/paperscout/health.py` | 对 staging Wiki 执行确定性结构/引用规则并生成健康报告。 | 不是语义 Review Agent。 |
| `src/paperscout/storage.py` | 文件系统路径、原子文本/JSON 写入、目录哈希、严格事件追加、staging 重建、带清单校验与崩溃窗口恢复的原子发布、结果写入和安全复制。 | Checkpoint 不存于此；旧自定义运行状态文件已删除。 |
| `src/paperscout/graph_runtime.py` | 创建 SQLite LangGraph Checkpointer、生成 `thread_id` 配置、声明可恢复节点异常并按节点前断点编译 `StateGraph`。 | 已接入 Ingest 与 QA 的中断/瞬时故障恢复。 |
| `src/paperscout/workflow.py` | 定义并运行 Ingest `StateGraph`：上下文、模型生成/规则修复、语义 Wiki Review/重生成、raw 保护、staging、规则审核、可恢复发布和 `resume_ingest()`。 | P0-B/P0-D1 本地闭环已完成；真实 Provider 语义质量仍需人工验证。 |

### 6.2 自动化测试与人工验证脚本

| 文件 | 作用 | 是否自动运行 |
| --- | --- | --- |
| `tests/test_evidence_contract.py` | 验证稳定 Evidence ID、章节归属、解析质量失败和严格参数契约。 | 是。 |
| `tests/test_p0a_contracts.py` | 验证 Wiki/Session/Graph/Event 契约，以及 SQLite Checkpoint 跨运行时恢复。 | 是。 |
| `tests/test_ingest_from_raw.py` | 验证 raw→Wiki 闭环、失败不发布、节点中断/瞬时故障恢复、模型与 Review 持久化重放、残缺 staging 重建以及原子发布崩溃窗口/篡改回滚。 | 是。 |
| `tests/test_review_contract.py` | 验证严格 verdict 第一行解析及未知、缺失或错位状态全部失败关闭。 | 是。 |
| `tests/test_read_project_file.py` | 验证文本/目录读取、offset/截断、预算、路径范围、资源元数据、Schema 和结构化错误；符号链接不可用时以 Windows junction 实测越界 reparse point。 | 是。 |
| `tests/test_answer_review.py` | 验证只收集实际引用 Evidence、文件/ID 失败关闭、显式依据请求规则和返回前哈希复核。 | 是。 |
| `tests/test_qa_graph.py` | 验证精简工具结果、严格模型 JSON、渐进读取、引用失败关闭、节点中断/瞬时故障恢复、模型/工具/Review 持久化重放、预算不重复计费、终态幂等 resume、Session 三文件、多轮继续、路径保护、写后故障幂等恢复、动态压缩与四轮下限、资源哈希失效重读、项目记忆共享/隔离/恢复、用户 Profile 跨项目只读加载，以及 Answer Review 隔离、审计、修订补读、安全降级和被拒草稿上下文隔离。 | 是。 |
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

当前 P0 可信闭环已完成，剩余架构缺口集中在运行可观测性和真实服务评测：

1. 补齐事件流式消费和回放；
2. 使用真实 MinerU 与真实 LLM 执行语义质量、上下文缩减比例、Review 修复率和恢复成功率评测。

每一步都应先添加 fixture 和关键契约测试，再实现功能，并在同一提交中更新 `docs/refactor-progress.md`。

## 9. 本地验证命令

```powershell
.venv\Scripts\python.exe -m pytest -q
uv --no-cache lock --check
git diff --check
```

离线 pytest 证明当前自动测试覆盖的行为没有回归，但 Mock QA/Review 只验证控制流和契约，不能证明真实问答或语义审核质量；保守 token 估算也不等同于具体 Provider tokenizer 的精确计数。真实 MinerU/LLM 只通过对应人工脚本单独验证，不能混入离线基线结论。
