# PaperScout 项目经历与 LangGraph 重构总计划

> 状态：后续重构的总入口。本文保存项目经历表述，并将现有专项 TODO 汇总为实施路线；不表示所列能力已经全部完成。

## 1. 项目经历

```latex
\section{项目经历}
\datedsubsection{\textbf{PaperScout（多 Agent 学术调研系统）}}{2026.05 -- 2026.08}

项目介绍：面向科研选题与文献综述场景，构建可复用、可追溯的论文知识库，解决多轮调研中上下文难沉淀、跨论文分析成本高及生成结论缺少原文依据的问题。

\begin{itemize}

\item \textbf{自研领域 Agent Harness：}
参考 Pi Agent Harness 的运行时设计，基于 LangGraph 编排 Ingest、QA 和 Review Agent，实现 Agent Loop、工具调用、上下文压缩、Session 持久化、Checkpoint 与失败恢复。

\item \textbf{学术 Wiki：}
基于 MinerU 解析学术论文，以二级章节为粒度建立 Wiki 陈述到 PDF 原文的 Evidence 映射；设计渐进式加载 Wiki，支持多论文综述、方法比较与研究思路分析。

\item \textbf{可信审核机制：}
设计“规则校验 + Review Agent”的双阶段审核链路，校验 Wiki 与 QA 回答的事实依据、Evidence 匹配及跨论文归属，并通过审核门驱动重新生成或发布。

\end{itemize}
```

简历中的“实现”必须以本计划的完成标准为依据。尚未落地的能力在开发期间只能表述为“设计”。

## 2. 重构目标

后续使用 Python 和 LangGraph 重构整个 PaperScout。LangGraph 负责状态、节点、条件分支、循环、Checkpoint 和恢复；PaperScout 在其上实现面向论文调研的轻量 Agent Harness。

参考 Pi 的设计思想，但不复制 Pi 的全部通用编码 Agent 能力。第一阶段只实现 PaperScout 需要的部分：

- 统一的模型调用与工具调用循环；
- Session、消息记录和上下文压缩；
- Checkpoint、中断与失败恢复；
- 工具参数校验、目录权限和读取预算；
- Wiki Review 与 Answer Review 审核门；
- 可追踪的运行状态和审计记录。

暂不实现 Shell、通用写文件、动态 Skill、自由创建子 Agent、远程 Sandbox、插件市场、向量长期记忆和通用 TUI。

## 3. 已确认的专项方案

重构以以下文档为准，本文不重复覆盖其中的内容细节：

1. [Wiki 渐进式加载与 Ingest 方案](./wiki-progressive-loading-plan.md)
   - 定义 Wiki 五部分内容和渐进式加载方式；
   - 定义基于 MinerU `text_level: 2` 的 section evidence；
   - Ingest 是无工具、无记忆的 Chat Client；
   - 宿主负责 evidence 构造、校验、写入和发布。

2. [QA Agent 渐进式阅读与记忆方案](./qa-agent-progressive-reading-plan.md)
   - QA 是有状态、可调用只读工具的 Agent；
   - QA 可依次读取 Wiki、section evidence 和 raw；
   - 定义 Session、长期记忆、上下文压缩和读取预算；
   - 第一版不允许 QA 修改 Wiki。

3. [Review Chat Client 方案](./review-chat-client-plan.md)
   - Wiki Review 与 Answer Review 均为无工具、无记忆的 Chat Client；
   - 先执行确定性规则，再执行语义审核；
   - 使用 `APPROVE`、`REVISE`、`REJECT` 控制后续流程。

若专项文档与本文产生冲突，以更新更具体的专项文档为准，并同步修订本文的实施映射。

## 4. 目标架构

```text
PDF / MinerU
  ↓
Importer 与 Evidence Builder
  ↓
Ingest Graph
  ├─ 构造 Ingest 上下文
  ├─ Ingest Chat Client 生成 Wiki 候选
  ├─ Wiki 确定性规则校验
  ├─ Wiki Review Chat Client
  ├─ REVISE / REJECT → 带反馈重新生成
  └─ APPROVE → staging → 发布

用户问题
  ↓
QA Graph
  ├─ 加载 Session 与项目记忆
  ├─ QA Agent
  ├─ read_project_file Tool
  ├─ 按需读取 Wiki / Evidence / Raw
  ├─ 上下文压缩
  ├─ Answer 确定性规则校验
  ├─ Answer Review Chat Client
  ├─ REVISE / REJECT → 补读或重新回答
  └─ APPROVE → 返回答案并保存 Session
```

## 5. LangGraph 与 Harness 的职责边界

### 5.1 LangGraph 负责

- 定义 Ingest Graph 和 QA Graph；
- 管理 Graph State；
- 执行节点与条件边；
- 管理 Ingest、QA、Review 的循环和最大重试次数；
- 使用 Checkpointer 保存执行位置；
- 支持中断、恢复和流式状态输出。

### 5.2 PaperScout Harness 负责

- 统一模型调用接口与错误类型；
- 构造每类 Agent 的上下文；
- 注册并执行工具；
- 使用 Pydantic 或 JSON Schema 校验 Tool Call；
- 实施文件路径权限、读取长度和单轮预算；
- 保存 Session 消息、摘要和长期项目记忆；
- 决定何时压缩上下文以及压缩后保留什么；
- 收集 Review 输入并解析审核结果；
- 记录运行、工具、审核和失败信息。

### 5.3 Agent 负责

| Agent | 形态 | 工具 | 记忆 | 职责 |
| --- | --- | --- | --- | --- |
| Ingest | Chat Client | 无 | 无 | 根据宿主提供的 evidence 生成 Wiki 候选内容 |
| Wiki Review | Chat Client | 无 | 无 | 判断 Wiki 是否受到 evidence 支持 |
| QA | Stateful Agent | `read_project_file` | 有 | 自主读取资料并回答用户问题 |
| Answer Review | Chat Client | 无 | 无 | 判断 QA 回答是否正确、有依据且未混淆论文 |

Review 是显式工作流节点和审核门，不作为普通 Hook 隐藏在写入逻辑中。

## 6. Graph State 初步设计

### 6.1 Ingest State

至少包含：

- `run_id`、`paper_id`；
- MinerU 和 evidence 的输入覆盖信息；
- Wiki 候选 Markdown；
- 规则校验结果；
- Review verdict 与反馈；
- 当前尝试次数和最大重试次数；
- staging 与发布状态；
- 最近一次错误。

### 6.2 QA State

至少包含：

- `run_id`、`session_id`、用户问题；
- 当前对话消息和历史摘要；
- 用户项目记忆；
- 已读取资源的路径、范围、哈希和 evidence ID；
- 当前工具调用与读取预算；
- QA 候选答案；
- Answer Review verdict 与反馈；
- 当前尝试次数和最近一次错误。

具体字段在实现前以 Pydantic 模型固化，避免在不同节点中传递无约束字典。

## 7. Pi 风格能力映射

| Pi 风格能力 | PaperScout 实现方式 | 优先级 |
| --- | --- | --- |
| Agent Loop | LangGraph 中的模型节点、工具节点和条件边 | P0 |
| Tool lifecycle | 工具注册、参数校验、执行、错误标准化和结果回填 | P0 |
| Session | `messages.jsonl`、`state.json`、`summary.md` | P0 |
| Context transform | 模型调用前构造系统规则、记忆、摘要和当前资料 | P0 |
| Context compaction | 达到预算时压缩旧对话并移除旧工具大文本 | P0 |
| Checkpoint / resume | LangGraph Checkpointer 与 `thread_id` | P0 |
| Review gate | 确定性规则节点、Review 节点和条件分支 | P0 |
| Event stream | 输出模型、工具、审核和运行状态事件 | P1 |
 |  |

## 8. 分阶段重构计划

### P0-A：固化数据契约

1. 定义 section evidence、Wiki、Review verdict、Tool Call、Session 和 Graph State 模型；
2. 固化 evidence ID 生成和 `text_level: 2` 分段规则；
3. 明确 Markdown 中 evidence 标记的解析规则；
4. 将宿主确定性校验从 Agent Prompt 中分离；
5. 为现有数据建立兼容层，避免重构时破坏已有 raw 和 Wiki。

完成标准：同一份 MinerU 输入能够稳定产生相同 evidence ID；所有 Graph 节点共享明确的数据模型。

### P0-B：重构 Ingest Graph

1. 将现有 Ingest 过程拆为上下文构造、生成、规则校验、语义审核、重试、staging 和发布节点；
2. 移除 Ingest 的工具循环，保持单次上下文 Chat Client 模式；
3. 将 Wiki Review 实现为独立 Chat Client 节点；
4. 为 `REVISE` 和 `REJECT` 建立显式条件边及最大重试限制；
5. 仅由宿主执行文件写入和发布。

完成标准：从 MinerU 数据到 Wiki 发布的每个状态可观察；未通过规则或 Review 的候选无法发布。

### P0-C：重构 QA Graph 与只读工具

1. 实现 QA 模型与 `read_project_file` 之间的 Agent Loop；
2. 校验 Tool Call JSON Schema，并限制工具读取目录和返回长度；
3. 支持目录、Wiki、section evidence、MinerU 文本、PDF 和图片资源读取；
4. 实现 Wiki 入口 → Wiki 正文 → Evidence → Raw 的渐进读取；
5. 增加调用次数、单次读取量和单轮累计读取预算。

完成标准：QA 能自主决定读取范围；越界路径、无效参数和超预算请求不会进入真实文件读取。

### P0-D：实现 Session、压缩与恢复

1. 使用 LangGraph Checkpointer 保存工作流执行状态；
2. 保存 `messages.jsonl`、`state.json` 和 `summary.md`；
3. 保留最近四轮完整对话，将更早内容压缩为结构化摘要；
4. 从上下文移除旧工具大文本，保留路径、哈希和 evidence ID；
5. 实现同一 Session 的继续执行和失败恢复；
6. 第一版使用文件型长期项目记忆，不引入向量数据库。

完成标准：进程中断后可以从 Checkpoint 恢复；长对话压缩后仍保留研究目标、论文指代、已用证据和未解决问题。

### P0-E：接入 Answer Review

1. 在 QA 候选答案后执行确定性引用规则；
2. 收集答案引用的 evidence，构造 Answer Review 输入；
3. 根据 verdict 返回、修改、补读或拒绝答案；
4. 保存审核请求、响应、结论和修改次数；
5. 达到最大修复次数后只返回受支持内容或明确报告证据不足。

完成标准：未通过规则或语义审核的答案不能直接返回；跨论文陈述能够追踪到各自 evidence。

### P1：运行事件与可观测性

统一以下事件：

```text
run.started
model.started
model.completed
tool.started
tool.completed
review.started
review.completed
context.compacted
run.completed
run.failed
```

事件至少包含 `run_id`、`session_id`、Agent 类型、时间和必要状态。事件可先写入 JSONL，后续再用于前端流式进度。

完成标准：一次 Ingest 或 QA 运行能够还原模型、工具、审核、压缩和失败的执行顺序。

## 9. 验证策略

围绕关键风险进行验证，不为简单的数据搬运编写重复测试：

- Evidence：二级章节边界、稳定 ID、图表与公式归属；
- Tool：Schema、路径越界、符号链接、读取截断和预算；
- Ingest：规则失败无法发布，Review 反馈能够触发重新生成；
- QA：Wiki 不足时能够回查 evidence/raw，跨论文引用不混淆；
- Memory：压缩后保留用户目标、论文指代和 evidence；
- Recovery：模型或工具失败后能够从已保存状态恢复；
- Review：无法解析 verdict 时必须失败关闭，不能默认通过。

## 10. 项目经历的验收映射

| 简历表述 | 必须具备的代码或验证证据 |
| --- | --- |
| 自研领域 Agent Harness | Agent Loop、工具生命周期、Session、压缩、Checkpoint 和恢复均有实际实现 |
| 基于 LangGraph 编排多 Agent | Ingest Graph 与 QA Graph 存在明确节点、状态、条件边和审核循环 |
| Wiki 到 PDF 原文的 Evidence 映射 | 稳定 evidence ID、章节内容、页码、block index 和 bbox 可追溯 |
| 渐进式加载 Wiki | QA 能从入口筛选到正文，再按需读取 evidence/raw |
| 双阶段审核链路 | 确定性规则和两个 Review profile 均接入放行流程 |
| 自动修订或发布 | verdict 能实际控制重试、拒绝、staging、发布或答案返回 |

重构完成后，再根据真实测试数据补充量化指标，例如平均上下文缩减比例、Review 拒绝与修复率、失败恢复成功率或多论文问答的证据命中率；不得预先编造指标。

## 11. 实施原则

- 保留 `raw/` 为不可变来源层，不让任何 Agent 修改；
- Ingest 和 Review 保持 Chat Client，不为了“多 Agent”增加无必要的工具和记忆；
- QA 只获得受控读取工具，暂不提供 Wiki 写入能力；
- Review 作为显式 Graph 节点，不隐藏在普通 Hook 中；
- Checkpoint 保存执行状态，Session Memory 保存对话语义，两者分开设计；
- 优先完成 P0 的可信闭环，再增加事件流和高级 Harness 能力；
- 重构期间保持现有文件兼容，避免一次性更换全部存储格式。
