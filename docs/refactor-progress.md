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
| 离线测试集 | 120 tests | 本地自动验证 | `.venv\Scripts\python.exe -m pytest -q`；越界 reparse point 在普通 symlink 不可用时通过 Windows junction 回退实测 |
| 依赖锁一致性 | 通过 | 本地自动验证 | `uv --no-cache lock --check`（绕开本机全局 uv cache 路径冲突，不改变校验语义） |
| 补丁空白/冲突标记 | 通过 | 本地自动验证 | `git diff --check` |
| Windows GitHub Actions | main 推送后触发 | 未验证 | `.github/workflows/ci.yml`：`uv sync --frozen --group dev`，随后 `uv run pytest -q` |
| 真实 MinerU/真实 LLM | 未纳入本轮 | 未验证 | `tests/manual_mineru_raw.py`、`tests/manual_raw_to_wiki.py`、`tests/evaluate_ingest_agent.py` |

离线测试只证明表中已覆盖的控制流和契约；Mock QA/Review 不能证明真实语义质量，保守 token 估算不等同于具体 Provider tokenizer 的精确计数。

## 3. 阶段进度

| 阶段/切片 | 实现状态 | 已落地内容 | 实现是否已验证 | 验证方式 | 剩余缺口 |
| --- | --- | --- | --- | --- | --- |
| P0-0 离线基线与 CI | 已完成 | 测试不再被忽略；微型 MinerU fixture 已跟踪；Windows CI 已配置。 | 本地自动验证；远程 CI 待确认 | 完整 pytest；推送后检查 Actions | 在 main 推送结果上确认远程 CI。 |
| P0-A Evidence 契约 | 已完成 | 稳定 section ID、block/page/bbox、References 不可用于 Ingest、质量报告、缺失二级标题失败关闭。 | 本地自动验证 | `tests/test_evidence_contract.py`、`tests/test_ingest_from_raw.py` | 后续可增加更多真实 MinerU 版式 fixture，不阻塞当前完成标准。 |
| P0-A Wiki 契约 | 已完成 | 固定五栏与顺序、每栏 1–3 个当前输入 Evidence、规范渲染与索引。 | 本地自动验证 | `tests/test_p0a_contracts.py`、`tests/test_ingest_from_raw.py` | 无。 |
| P0-A Review 契约 | 已完成 | `ReviewVerdict`/`ReviewDecision` 严格模型和仅解析第一行的 fail-closed parser。 | 本地自动验证 | `tests/test_review_contract.py` | 无；真实语义质量属于人工评测，不影响契约完成状态。 |
| P0-A Session/Graph State/Tool/Event 契约 | 仅契约 | 严格用户 Profile、项目状态、Session、预算、Tool Call、Graph State、WorkflowEvent 模型。 | 本地自动验证 | `tests/test_p0a_contracts.py`、`tests/test_evidence_contract.py` | 执行层分别属于 P0-C、P0-D、P1。 |
| P0-B Ingest Graph | 已完成 | 真实 `StateGraph`、无工具 Ingest、规则修复、独立 Wiki Review、`APPROVE/REVISE/REJECT` 条件循环、raw 保护、候选哈希复核、staging 审核、原子发布和统一失败终态。 | 本地自动验证 | `tests/test_ingest_from_raw.py`、`tests/test_review_contract.py` | 真实 LLM 语义质量未人工验证；中断续跑已在 P0-D1 完成。 |
| P0-C1 受控只读工具 | 已完成 | `read_project_file` 支持文本/目录、PDF/图片资源、严格 Schema、允许根目录、路径穿越/reparse point 防护、offset/截断、哈希、资源记录和调用/字符预算。 | 本地自动验证 | `tests/test_read_project_file.py`：14 passed；Windows junction 回退验证越界链接 | 无；工具事件与 Graph State 更新已在 P0-C2 完成。 |
| P0-C2 QA Agent Loop | 已完成 | 真实 QA `StateGraph`、严格单对象 JSON 动作、单工具条件循环、精简模型可见结果、完整宿主审计、错误纠正、预算耗尽处理、Wiki→Evidence→raw 渐进读取、结构化回答及论文/Evidence 归属校验。 | 本地自动验证 | `tests/test_qa_graph.py`：35 passed；覆盖 Mock 公开入口、四级渐进读取、错误修正、预算、跨论文与伪造引用 | 真实 LLM 问答质量未人工验证。 |
| P0-D1 SQLite Checkpointer 与恢复 | 已完成 | Ingest/QA 显式节点前中断、瞬时模型/Review/工具故障保留可恢复 Checkpoint、`resume_ingest`/`resume_qa`、运行环境校验、终态幂等返回、模型/Review/工具持久化重放、原子审计写、staging 重建、发布清单校验与两个原子替换崩溃窗口恢复。 | 本地自动验证 | `tests/test_ingest_from_raw.py`：28 passed；`tests/test_qa_graph.py`：13 passed；覆盖 runtime reconstruction、原子文件替换、预算不重复计费、发布回滚和 raw 不变性 | 宿主文件副作用已幂等；若进程在远程模型返回但结果尚未持久化的极短窗口崩溃，Provider 请求可能重发，除非 Provider 支持幂等键。 |
| P0-D2 Session、压缩与记忆 | 已完成 | 用户可读 Session 三文件原子写入；安全 Profile/Session/项目路径；用户/宿主显式维护且 QA 只读的 `memory/profile.json`；可选 `project_id="default"`；跨 Session 项目记忆共享与跨项目隔离；Session 项目绑定；记忆/资源合并去重；`QA_LLM_CONTEXT_WINDOW` 独立配置；下一轮输入的保守 token 估算达到约 60% 时动态压缩最早历史；至少保留最近四轮；压缩边界单调持久化；旧工具正文不回流模型；加载时重新核对资源哈希并强制重新读取；`context.compacted` 事件；项目/Session 写后故障从 `complete_qa` 幂等续跑且不重复消息或模型调用。 | 本地自动验证 | `tests/test_qa_graph.py`：三文件、多轮继续、路径保护、写后故障恢复、阈值以下保留、阈值压缩、四轮下限、资源失效、项目记忆共享/隔离/恢复，以及 Profile 跨项目加载、只读、缺失与损坏文件测试；`tests/test_p0a_contracts.py`：独立 QA 窗口配置 | 无；精确 token 数取决于具体 Provider tokenizer，当前按文档采用确定性保守估算。 |
| P0-E Answer Review | 已完成 | 显式 `answer_rules`、独立 `answer_review`、`revise_answer`、`safe_answer` 与返回前 `verify_answer_evidence` 节点；只安全收集实际引用的当前 section Evidence；独立、无工具、无 Session Provider 审核问题、完整回答和实际引用 Evidence；严格解析首行 verdict；`APPROVE` 放行；`REVISE/REJECT` 驱动 QA 补读或完整重答并重新执行两层审核；初稿后最多修复两次，仍不通过则返回无 Claim/引用/记忆补丁的证据不足答案；非法输出失败关闭；逐次保存 request/response/result；Review 结果进入完整 Session 审计而被拒草稿不回流下一轮模型；Provider 瞬时故障可续跑且不重复 QA；最终哈希变化不保存 Session。 | 本地自动验证 | `tests/test_answer_review.py`：4 passed；`tests/test_review_contract.py`：5 passed；`tests/test_qa_graph.py`：35 passed，覆盖隔离、审计、事件、失败关闭、续跑、修订、补读、上限降级和历史投影 | 真实 LLM 语义审核与修订质量未人工验证。 |
| P1 事件与可观测性 | 部分实现 | Ingest 与 QA 严格 JSONL 运行/模型、中断、恢复及终态事件；QA 工具、上下文压缩、确定性 Answer 规则及语义 Answer Review 开始/完成事件；压缩事件携带窗口、阈值、估算 token 和保留轮数；恢复后序号连续；公开 API 支持完整严格回放和基于排他性序号游标的增量分页读取，验证路径、连续序号、唯一事件 ID、运行关联与终态边界。 | 本地自动验证 | `tests/test_workflow_events.py`：18 passed；Ingest/QA 真实运行日志回放断言；全量 pytest | 主动推送式前端流消费未实现；节点重试事件语义当前为 at-least-once。 |
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
| 2026-09-07 | `27cf929` | 接入独立 Wiki Review Chat Client、严格 verdict parser、只提交实际引用 Evidence、Review 审计文件和 `REVISE/REJECT` 重生成循环。 | 37 个离线测试、依赖锁、逐文件架构登记、过期声明和补丁格式检查通过。 |
| 2026-09-07 | `39f0a89` | 实现受控 `read_project_file` 的统一结果契约、路径安全、文件类型处理、文本截断、哈希、资源记录和读取预算。 | 完整基线 51 passed；符号链接不可用时通过 Windows junction 回退完成越界 reparse point 实测。 |
| 2026-09-07 | `5ec709f` | 实现 P0-C2 QA Agent Loop，并将工具模型输出收敛为精简 JSON 外壳；宿主继续保留完整预算、哈希与审计结果。 | QA 专项 9 passed；完整离线基线 60 passed；依赖锁与补丁格式检查通过。 |
| 2026-09-07 | `0481a71` | 完成 P0-D1 Checkpoint 中断/瞬时故障续跑与宿主副作用幂等恢复。 | Ingest/QA 专项 41 passed；完整离线基线 75 passed；依赖锁与补丁格式检查通过。 |
| 2026-09-08 | `502a953` | 完成 P0-D2 用户可读 Session 三文件、多轮继续、记忆与资源合并、路径保护及 Session 写入故障恢复。 | QA 专项 17 passed；完整离线基线 79 passed；依赖锁与补丁格式检查通过。 |
| 2026-09-08 | `1bdba0f` | 完成 P0-D2 固定最近四轮压缩、结构化摘要、旧工具正文移除、资源元数据保留、压缩事件和写后幂等恢复。 | QA 专项 18 passed；完整离线基线 80 passed；依赖锁与补丁格式检查通过。 |
| 2026-09-08 | `07f9016` | 完成 P0-D2 Session 资源哈希重验、失效资源移除、独占 Evidence 记忆清理、旧工具正文隔离和强制重新读取提示。 | QA 专项 20 passed；完整离线基线 82 passed；依赖锁与补丁格式检查通过。 |
| 2026-09-08 | `8b5ff22` | 完成 P0-D2 可选 `project_id="default"`、文件型项目状态、跨 Session 共享、项目隔离、Session 项目绑定和写后故障幂等恢复。 | QA 专项 23 passed；完整离线基线 85 passed；依赖锁与补丁格式检查通过。 |
| 2026-09-08 | `6df370b` | 完成 P0-D2 严格 `UserProfile`、用户/宿主显式保存、QA 跨项目只读加载、缺失文件兼容和未知 `profile_patch` 拒绝。 | QA 专项 26 passed；完整离线基线 88 passed；依赖锁与补丁格式检查通过。 |
| 2026-09-08 | `457dc55` | 完成 P0-D2 独立 QA 窗口配置、约 60% 动态压缩、四轮保留下限、单调压缩边界和可审计估算数据。 | QA 专项 27 passed；契约与 QA 专项 33 passed；完整离线基线 90 passed；依赖锁与补丁格式检查通过。 |
| 2026-09-08 | `dddfd71` | 完成 P0-E 确定性 Answer Review 规则、实际引用 Evidence 收集契约和返回前哈希复核。 | Answer Review 专项 4 passed；Answer Review、QA 与契约专项 38 passed；完整离线基线 95 passed；依赖锁与补丁格式检查通过。 |
| 2026-09-08 | `c83913f` | 完成 P0-E 独立语义 Answer Review `APPROVE` 审核门、逐次审计、严格 verdict 失败关闭和瞬时故障恢复。 | 语义 Answer Review 专项 5 passed；Answer Review/Review/QA 专项 44 passed；完整离线基线 100 passed；依赖锁与补丁格式检查通过。 |
| 2026-09-08 | `92aa442` | 完成 P0-E `REVISE/REJECT` 补读/重答循环、两次修复上限、安全证据不足降级、Review Session 审计和被拒草稿上下文隔离。 | QA 专项 35 passed；完整离线基线 102 passed；依赖锁与补丁格式检查通过。 |
| 2026-09-09 | （本提交） | 为 JSONL 审计新增严格完整回放与游标分页读取 API，覆盖安全运行路径、序号/关联/终态完整性校验，并接入现有 Ingest/QA 恢复日志测试。 | 事件专项 18 passed；Ingest/QA 联合专项 20 passed；完整离线基线 120 passed；依赖锁与补丁格式检查通过。 |

## 5. 项目经历表述验收

| 简历候选表述 | 当前是否可以写成“已实现” | 证据与原因 |
| --- | --- | --- |
| 自研领域 Agent Harness | 可以，限定为本地 PaperScout Harness | Agent Loop、工具生命周期、Session 文件、约 60% 阈值的动态上下文压缩、只读用户 Profile、文件型项目级长期记忆、Checkpointer 和中断/瞬时故障恢复均已落地并有自动测试；token 数使用跨模型的保守估算，不应表述为 Provider 精确计数。 |
| 基于 LangGraph 编排 Ingest、QA、Review | 可以 | Ingest、QA、Wiki Review 与 Answer Review 均为显式节点和条件边；两类 Review 都实际控制放行、重试或安全终止。 |
| Wiki 到 PDF 原文的 Evidence 映射 | 可以，限定为 MinerU section evidence | 稳定 ID、原始 block index、页码与 bbox 已实现并有自动测试。 |
| 渐进式加载 Wiki | 可以，限定为受控只读 QA | QA Agent Loop 已自动验证入口→论文 Wiki→section Evidence→raw 的模型决策与工具回填链路；真实模型的资料选择质量尚未人工评测。 |
| 规则校验 + Review Agent 双阶段审核 | 可以 | Wiki 与 QA 都已接入确定性规则和独立语义 Review；非通过 verdict 实际驱动完整重生成、补读/重答或安全降级。 |
| 审核驱动自动修订或发布 | 可以 | Wiki Review verdict 控制完整重生成、失败关闭与发布；Answer Review verdict 控制 QA 补读、重答、复审及最大次数安全降级。 |

## 6. 下一步

下一最小切片继续 P1 事件与可观测性：基于稳定读取/回放 API 设计主动流式消费边界，并明确节点重试的 at-least-once 语义；真实 MinerU/LLM 语义质量仍需单独人工评测。
