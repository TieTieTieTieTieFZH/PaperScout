# PaperScout LangGraph 重构进度台账

> 本文件是当前实现状态与验证证据的唯一台账。目标架构见 [重构总计划](./todo/project-experience-langgraph-refactor-plan.md)，当前代码结构见 [架构手册](./architecture-manual.md)。

## 1. 记录规则

从本文件建立后，每完成一部分功能，必须在同一个功能提交中更新本文件：

1. 写明完成的最小功能切片、代码位置和未完成边界；
2. “实现状态”只能使用 `未开始`、`仅契约`、`部分实现`、`已完成`；
3. “实现是否已验证”只能使用 `未验证`、`本地自动验证`、`远程 CI 验证`、`人工/真实服务验证`，可组合使用；
4. 记录可重复执行的命令、测试名称或人工步骤；
5. 只有满足该阶段完成标准并验证后，才能标为“已完成”；类或文件存在不等于功能已经接入；
6. 失败关闭是验收要求：解析、规则或 Review 覆盖不足时，不得发布该论文 Wiki；
7. raw 始终保留；旧 Wiki 可从 raw 重新生成，不保留旧实现兼容层。

## 2. 当前验证基线

| 项目 | 当前结果 | 实现是否已验证 | 验证方式 |
| --- | --- | --- | --- |
| 离线测试集 | 26 tests | 本地自动验证 | `.venv\Scripts\python.exe -m pytest -q` |
| 依赖锁一致性 | 通过 | 本地自动验证 | `uv --no-cache lock --check`（绕开本机全局 uv cache 路径冲突，不改变校验语义） |
| 补丁空白/冲突标记 | 通过 | 本地自动验证 | `git diff --check` |
| Windows GitHub Actions | 本次 main 推送后触发 | 未验证 | `.github/workflows/ci.yml`：`uv sync --frozen --group dev`，随后 `uv run pytest -q` |
| 真实 MinerU/真实 LLM | 未纳入本轮 | 未验证 | `tests/manual_mineru_raw.py`、`tests/manual_raw_to_wiki.py`、`tests/evaluate_ingest_agent.py` |

这里的 26 个测试只证明表中已覆盖的当前行为，不证明尚未实现的 QA、Session、语义 Review 或中断恢复。

## 3. 阶段进度

| 阶段/切片 | 实现状态 | 已落地内容 | 实现是否已验证 | 验证方式 | 剩余缺口 |
| --- | --- | --- | --- | --- | --- |
| P0-0 离线基线与 CI | 已完成 | 测试不再被忽略；微型 MinerU fixture 已跟踪；Windows CI 已配置。 | 本地自动验证；远程 CI 待本次推送 | 完整 pytest；推送后检查 Actions | 在 main 推送结果上确认远程 CI。 |
| P0-A Evidence 契约 | 已完成 | 稳定 section ID、block/page/bbox、References 不可用于 Ingest、质量报告、缺失二级标题失败关闭。 | 本地自动验证 | `tests/test_evidence_contract.py`、`tests/test_ingest_from_raw.py` | 后续可增加更多真实 MinerU 版式 fixture，不阻塞当前完成标准。 |
| P0-A Wiki 契约 | 已完成 | 固定五栏与顺序、每栏 1–3 个当前输入 Evidence、规范渲染与索引。 | 本地自动验证 | `tests/test_p0a_contracts.py`、`tests/test_ingest_from_raw.py` | 无。 |
| P0-A Review 契约 | 仅契约 | `ReviewVerdict`/`ReviewDecision` 严格模型。 | 本地自动验证 | 非法 verdict 拒绝测试 | 尚无语义 Review Client 和工作流节点。 |
| P0-A Session/Graph State/Tool/Event 契约 | 仅契约 | 严格 Session、预算、Tool Call、Graph State、WorkflowEvent 模型。 | 本地自动验证 | `tests/test_p0a_contracts.py`、`tests/test_evidence_contract.py` | 执行层分别属于 P0-C、P0-D、P1。 |
| P0-B Ingest 核心 | 部分实现 | 真实 `StateGraph` 节点与条件边、section evidence 输入、无工具 Chat Client、一次修复、两次 raw 哈希保护、候选哈希复核、staging、确定性规则审核、原子发布和统一失败终态。 | 本地自动验证 | `tests/test_ingest_from_raw.py`：成功/覆盖失败/规则拒绝终态、节点集合、修复分支和发布门 | 缺少语义 Wiki Review Chat Client、`REVISE/REJECT` 语义循环和中断后续跑。 |
| P0-C QA Graph 与只读工具 | 未开始 | 仅保留状态、参数和预算契约。 | 未验证 | 无可运行 QA 功能 | 实现工具权限/预算、Agent Loop、渐进读取、多论文引用校验。 |
| P0-D1 SQLite Checkpointer | 部分实现 | `GraphRuntime`、SQLite Saver、`thread_id` 配置；实际 Ingest 各节点使用 Checkpointer，成功和失败终态可跨 runtime reconstruction 读取。 | 本地自动验证 | `test_sqlite_checkpointer_survives_runtime_reconstruction`、`test_ingest_runs_as_checkpointed_state_graph`、失败终态测试 | QA 尚未接入；未实现并验证中断后续跑和副作用幂等。 |
| P0-D2 Session、压缩与记忆 | 仅契约 | Session/消息/资源记录/预算/项目记忆模型。 | 本地自动验证（仅模型） | 严格模型测试 | 文件持久化、最近四轮、结构化摘要、旧工具文本移除、继续执行与恢复均未实现。 |
| P0-E Answer Review | 未开始 | 只有通用 Review 契约。 | 未验证 | 无 | 答案规则、语义 Review、补读/修订/拒绝条件边。 |
| P1 事件与可观测性 | 部分实现 | Ingest 严格 JSONL 事件及运行关联字段。 | 本地自动验证 | Ingest 事件序列测试 | QA/Tool/Review/Compaction 全事件、流式消费和回放未实现。 |
| 旧核心逻辑清理 | 已完成 | 移除旧 Evidence、ReadRaw、QA、迁移和旧 `state.json` 路径；保留导入、MinerU、raw 和发布边界。 | 本地自动验证 | 全量 pytest；旧符号与路径静态检索 | 无；后续不新增兼容层。 |

## 4. 已完成切片与提交证据

| 日期 | 提交 | 功能切片 | 验证结论 |
| --- | --- | --- | --- |
| 2026-09-06 | `f80dd79` | 重构前快照，并推送 `codex/pre-langgraph-refactor` 备份分支。 | Git 分支/远端引用已确认。 |
| 2026-09-06 | `4832ec5` | Section Evidence 与相关严格契约。 | 本地契约测试通过。 |
| 2026-09-06 | `257a6d5` | Workflow State、Session/Event 契约和 SQLite Checkpointer 骨架。 | 本地 Checkpointer 重建测试通过。 |
| 2026-09-06 | `4b6ced0` | Ingest 切换到新的 Section Evidence 输入和失败关闭策略。 | 本地 Ingest 流程测试通过。 |
| 2026-09-07 | `0aaf0f6` | 删除重构前 QA、Evidence、迁移和自定义 checkpoint 兼容路径。 | 22 个离线测试通过；旧符号静态审计无残留。 |
| 2026-09-07 | `649d152` | 新增当前架构手册、进度台账并修正文档中的过期能力声明。 | 22 个离线测试、依赖锁检查、补丁格式检查和代码文件覆盖检查均通过。 |
| 2026-09-07 | `2fdb03c` | 新增根目录 `AGENTS.md`，固化文档阅读顺序、开发边界、提问条件和验证/推送规则。 | 22 个离线测试、依赖锁、文档引用和补丁格式检查通过。 |
| 2026-09-07 | `606e1dd` | 将 raw→Wiki 主路径切换为真实 LangGraph `StateGraph`，接入 SQLite Checkpointer、显式失败分支、结构化覆盖报告和发布前二次完整性门。 | 26 个离线测试、依赖锁、补丁格式、图接线和过期架构声明检查通过。 |

## 5. 项目经历表述验收

| 简历候选表述 | 当前是否可以写成“已实现” | 证据与原因 |
| --- | --- | --- |
| 自研领域 Agent Harness | 否 | 只有严格契约和 Checkpointer 骨架；Agent Loop、工具生命周期、Session、压缩和真实恢复未形成闭环。 |
| 基于 LangGraph 编排 Ingest、QA、Review | 否 | Ingest 已由 LangGraph 编排，但 QA Graph 和语义 Review 节点尚未实现，不能扩大表述为完整多 Agent 编排。 |
| Wiki 到 PDF 原文的 Evidence 映射 | 可以，限定为 MinerU section evidence | 稳定 ID、原始 block index、页码与 bbox 已实现并有自动测试。 |
| 渐进式加载 Wiki | 否 | 尚无可运行 QA 工具和入口→正文→Evidence→raw 的 Agent Loop。 |
| 规则校验 + Review Agent 双阶段审核 | 否 | 只有确定性 Wiki 规则；语义 Wiki/Answer Review 未接入。 |
| 审核驱动自动修订或发布 | 只能描述局部能力 | 确定性解析失败可修复一次、规则通过才发布；语义 verdict 尚不能控制工作流。 |

## 6. 下一步

下一最小切片是 P0-B 的 Wiki Review Chat Client：先为严格 verdict 解析、语义 `APPROVE/REVISE/REJECT`、修订反馈、最大尝试次数和失败不发布添加 fixture/契约测试，再接入现有 `StateGraph` 条件循环。中断后续跑与副作用幂等仍属于 P0-D1 的未完成项；完成并验证每个切片时同步更新本文件。
