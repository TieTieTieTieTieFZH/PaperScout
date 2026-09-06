# PaperScout Review Chat Client 方案

> 状态：当前已确认的 Review 设计方案。本文记录 Review 的职责、输入输出、规则审核和语义审核流程，不表示相关代码已经完成。

## 1. Review 的定位

Review 采用无工具、无记忆的 Workflow Chat Client，不采用 QA 那样的通用 Agent。

Review 的工作是判断候选内容是否得到指定资料支持。宿主程序已经知道审核对象，并负责准备所需 evidence，因此 Review 不需要自行搜索资料、读取文件或修改候选内容。

系统包含两种 Review：

- **Wiki Review**：审核 Ingest 生成的 Wiki Markdown；
- **Answer Review**：审核 QA 生成的自然语言 Markdown 回答。

两种 Review 共用相同运行机制，但使用不同的审核提示词和侧重点。

Review 不负责：

- 调用文件读取或搜索工具；
- 修改 Wiki 或 QA 回答；
- 写入文件或发布结果；
- 保存会话记忆或长期记忆；
- 为候选内容补充新的论文事实。

## 2. 输入与输出形式

Ingest、QA 和 Review 均不要求将用户可读内容改成 JSON。

| 组件 | 输入 | 输出 |
| --- | --- | --- |
| Ingest | 论文元数据和带 evidence ID 的原文 | Wiki Markdown |
| Wiki Review | Wiki Markdown 和相关 evidence | 审核状态与自然语言意见 |
| QA | 用户问题、记忆及读取的文件内容 | 自然语言 Markdown 回答 |
| Answer Review | 用户问题、QA 回答及相关 evidence | 审核状态与自然语言意见 |

宿主程序内部可以使用 Python 对象组织审核数据，但发送给 Review 时使用自然语言消息和明确的分隔符。

## 3. Wiki Review 输入

Wiki Review 接收论文基本信息、待审核 Wiki 和相关 section evidence。

```text
请审核下面这份论文 Wiki。

【论文】
Paper ID: 8272
Title: ...

【待审核 Wiki】
--- WIKI START ---

# 研究问题

论文研究……

[evidence:8272:s0008]

# 核心思路

……

[evidence:8272:s0060]

--- WIKI END ---

【可用 Evidence】

--- EVIDENCE 8272:s0008 START ---

标题：1 Introduction
页面：1–2

完整章节内容……

--- EVIDENCE 8272:s0008 END ---

--- EVIDENCE 8272:s0060 START ---

标题：4 The Proposed P-HNet Model
页面：4–6

完整章节内容……

--- EVIDENCE 8272:s0060 END ---
```

Wiki Review 使用当前 section evidence 粒度进行审核，不提取更细的公开 evidence ID，也不要求返回具体支持句。

## 4. Answer Review 输入

Answer Review 接收用户问题、QA 回答和回答所引用的 section evidence。

```text
请审核下面的 QA 回答。

【用户问题】

这些论文使用了哪些特征交互方法？

【QA 回答】

论文 8272 使用个性信息引导视觉特征交互。
[evidence:8272:s0064]

【相关 Evidence】

--- EVIDENCE 8272:s0064 START ---

完整章节内容……

--- EVIDENCE 8272:s0064 END ---
```

Answer Review 只依据宿主提供的用户问题、回答、Wiki 和 evidence 判断，不使用自身记忆补充事实。

## 5. Review 输出

Review 输出固定状态行和自然语言审核意见，不要求输出 JSON。

### 5.1 允许

```markdown
VERDICT: APPROVE

未发现需要修改的问题。
```

### 5.2 修改

```markdown
VERDICT: REVISE

1. “该方法在所有数据集上均取得最佳结果”表述过强。引用章节只支持部分指标上的比较结果。
2. 实验部分将实验目的写成了已经证明的结论。
3. 建议缩小结论范围，并区分实验设置和实验结果。
```

### 5.3 拒绝

```markdown
VERDICT: REJECT

候选内容中的主要方法描述与原文不一致，无法通过局部措辞调整解决，建议重新生成。
```

状态使用：

```text
APPROVE
REVISE
REJECT
```

程序只解析第一行：

- `APPROVE`：进入下一阶段；
- `REVISE`：将自然语言意见反馈给 Ingest 或 QA；
- `REJECT`：放弃当前候选并重新生成；
- 第一行无法解析：视为 Review 调用失败，不能默认通过。

## 6. 简单规则审核

规则审核由程序完成，先于语义审核执行。当前只检查已经有明确数据支撑的事项。

### 6.1 Wiki 候选规则

- 输出不能为空；
- 输出必须是 Markdown 文本；
- 包含研究问题、核心思路、方法、实验概况、结论与局限；
- 各部分必须有正文；
- evidence 标记格式正确；
- 引用的 evidence ID 在当前 evidence 文件中存在；
- evidence ID 属于当前论文；
- 同一个位置不重复引用相同 evidence；
- 不允许引用 References 对应的 evidence；
- 不允许出现宿主未提供的 evidence ID。

### 6.2 QA 回答规则

- 回答不能为空；
- evidence 标记格式正确；
- 引用的 evidence ID 存在；
- evidence ID 能定位到具体 section evidence；
- 引用了多篇论文时，不同 evidence ID 能找到对应论文；
- 用户明确要求原文或依据时，回答必须包含 evidence 引用；
- 不允许引用不存在的 paper ID 或 evidence ID。

### 6.3 当前暂不检查

以下规则依赖尚未定义的数据结构，当前不加入：

- PDF、MinerU 或 Wiki 多版本；
- `paper_fact` 等类型字段；
- claims 是否覆盖全部回答；
- candidate hash；
- raw 是否在审核期间变化；
- supporting excerpt 是否精确存在；
- 截断范围对应的引用；
- 逐条陈述的结构化字段。

## 7. Wiki Review 的语义侧重点

Wiki Review 检查：

- 研究问题是否与论文描述一致；
- 核心思路是否准确；
- 方法包含的步骤是否真实存在；
- 方法各部分的作用和连接关系是否被错误解释；
- 是否把相关工作写成当前论文的方法；
- 是否出现原文没有的模块、机制或结论；
- 实验类型、验证目的和评估指标是否准确；
- 是否把“进行了某个实验”写成“已经证明某个结论”；
- 是否把局部实验结果扩大为普遍结论；
- 结论与局限是否符合作者的描述；
- Wiki 的五个部分之间是否互相矛盾；
- 引用的完整 section evidence 是否能够支撑相关内容。

Wiki Review 审核的是 Wiki 是否忠实表达当前论文材料，不负责判断论文研究是否真实、实验设计是否优秀或结论是否可以被外部复现。

## 8. Answer Review 的语义侧重点

Answer Review 检查：

- 是否回答了用户的问题；
- 回答中的论文描述是否与 Wiki 和 evidence 一致；
- 是否混淆不同论文的方法；
- 是否把一篇论文的方法归到另一篇论文；
- 多篇论文比较时是否忽略任务、数据集或评估指标差异；
- 是否把某篇论文的局部结果写成普遍结论；
- 是否将研究构思写成已经验证的事实；
- 用户要求依据时是否确实提供了相关 evidence；
- 当前资料不足时，QA 是否仍给出无依据结论。

Review 可以在内部自行拆分回答中的陈述，但不要求输出结构化 claims。

## 9. Wiki Review 工作流

```text
Ingest 输出 Wiki Markdown
  ↓
程序执行简单规则审核
  ↓
程序收集 Wiki 引用的 section evidence
  ↓
Wiki Review Chat Client
  ↓
APPROVE
  → 写入 staging

REVISE
  → 将自然语言问题反馈给 Ingest
  → Ingest 重新输出完整 Markdown
  → 重新执行规则和语义审核

REJECT
  → 放弃当前候选并重新生成
```

## 10. Answer Review 工作流

```text
QA 输出自然语言回答
  ↓
程序执行简单引用规则审核
  ↓
程序收集回答引用的 section evidence
  ↓
Answer Review Chat Client
  ↓
APPROVE
  → 返回用户

REVISE
  → 将问题反馈给 QA
  → QA 修改回答或继续读取资料
  → 重新审核

REJECT
  → 不返回当前答案
```

每次修改后都重新审核完整候选内容，不只审核被修改的句子。

## 11. Review 的状态与审计记录

Review 不需要会话记忆和长期记忆。每次使用独立上下文，避免前一次审核意见影响当前判断。

需要保存的是审核运行记录：

```text
runs/{run_id}/review/
├── request.md
├── response.md
└── result.json
```

记录内容包括：

- 审核类型；
- 待审核文本；
- 提供给 Review 的 evidence；
- Review 原始输出；
- 最终 VERDICT；
- 修复次数。

这些记录用于审计，不作为 Review Agent 的记忆。

## 12. 当前 Review 完整流程

```text
候选内容生成完成
  ↓
程序执行简单规则审核
  ├─ 不通过：直接反馈生成器
  └─ 通过：构造自然语言 Review 输入
              ↓
            Review Chat Client 语义审核
              ↓
            解析第一行 VERDICT
              ├─ APPROVE：进入写入或返回阶段
              ├─ REVISE：反馈意见并重新生成
              ├─ REJECT：放弃当前候选
              └─ 无法解析：Review 失败，不允许通过
```

## 13. 组件职责

| 组件 | 职责 |
| --- | --- |
| 宿主程序 | 执行基础规则、收集 evidence、构造 Review 输入、解析 VERDICT 和控制工作流 |
| Wiki Review | 判断 Wiki 是否忠实表达论文内容 |
| Answer Review | 判断 QA 回答是否得到 Wiki 和 evidence 支持 |
| Ingest | 根据 Review 反馈重新生成 Wiki Markdown |
| QA | 根据 Review 反馈补读资料或修改回答 |

