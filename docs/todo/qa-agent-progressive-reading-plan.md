# PaperScout QA Agent 渐进式阅读与记忆方案

> 状态：QA Agent 当前设计方案。本文描述 QA 的职责、工具、记忆、阅读流程和审核方式，不包含 Wiki 修改能力，也不表示相关代码已经完成。

## 1. 设计目标

QA Agent 通过读取 PaperScout 中的 Wiki 和 raw 资料，帮助用户理解一篇或多篇论文。

支持三类主要任务：

- **领域概览**：读取多篇论文，生成简短综述。
- **方法比较**：比较不同论文的方法、步骤或模块。
- **研究构思**：根据已有论文讨论模块组合和跨领域方法借鉴。

QA 需要区分：

| 内容类型 | 含义 |
| --- | --- |
| 论文事实 | 论文明确报告的内容 |
| 跨论文归纳 | QA 根据多篇论文形成的综合结论 |
| 研究假设 | QA 根据已有方法提出的待验证构思 |
| 证据不足 | 当前 Wiki 和 raw 无法支持的内容 |

论文事实必须提供 evidence。研究假设可以引用灵感来源，但不能表述为已被论文验证。

## 2. QA Agent 定位

QA 采用通用 Agent 形式，具备：

- 工具调用能力；
- 多轮决策能力；
- 会话状态；
- 长期项目记忆；
- 渐进式读取能力；
- 根据证据生成回答的能力。

QA 不负责：

- 导入 PDF；
- 调用 MinerU；
- 生成或修改 Wiki；
- 修改 raw 或 evidence；
- 将自己的回答保存为论文事实。

QA 只有读取能力，文件写入、状态持久化和审核均由宿主程序执行。

## 3. 可读取的资料

QA 可以读取两个受控目录：

```text
wiki/
raw/papers/
```

### 3.1 Wiki

```text
wiki/
├── indexes/
│   └── overview.md
├── papers/
│   └── {paper_id}.md
└── evidence/
    └── {paper_id}/
        └── {section_id}.md
```

| 文件 | 内容 |
| --- | --- |
| `indexes/overview.md` | 全部论文的标题、研究问题和核心思路 |
| `papers/{paper_id}.md` | 一篇论文的完整 Wiki |
| `evidence/{paper_id}/{section_id}.md` | 一个 `text_level: 2` 章节对应的原文内容 |

### 3.2 Raw

```text
raw/papers/{paper_id}/
├── source.pdf
├── metadata.json
└── mineru/
    ├── full.md
    ├── content_list.json
    └── images/
```

Raw 用于：

- Wiki 内容不够详细；
- section evidence 不足；
- 用户要求查看原文；
- 用户要求查看 PDF、图片或表格；
- 需要检查 MinerU 原始 block；
- 需要核对公式、页码、bbox 或解析问题。

正常读取顺序为：

```text
Wiki 入口
  ↓
Wiki 正文
  ↓
Section evidence
  ↓
Raw MinerU 或 PDF
```

用户明确要求原文时，可以直接进入 evidence 或 raw。

## 4. 唯一读取工具

QA 只提供一个工具：

```text
read_project_file
```

### 4.1 输入

模型请求工具时使用严格动作外壳：

```json
{
  "type": "tool_call",
  "id": "call-001",
  "name": "read_project_file",
  "arguments": {
    "path": "wiki/papers/8272.md",
    "offset_chars": 0,
    "max_chars": 20000
  }
}
```

不得附加代码围栏、解释文字、第二个动作或未知字段。`arguments` 的内容为：

```json
{
  "path": "wiki/papers/8272.md",
  "offset_chars": 0,
  "max_chars": 20000
}
```

JSON Schema：

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["path"],
  "properties": {
    "path": {
      "type": "string",
      "minLength": 1,
      "maxLength": 500
    },
    "offset_chars": {
      "type": "integer",
      "minimum": 0,
      "default": 0
    },
    "max_chars": {
      "type": "integer",
      "minimum": 1,
      "maximum": 50000,
      "default": 20000
    }
  }
}
```

### 4.2 工具行为

| 目标类型 | 返回结果 |
| --- | --- |
| 目录 | 返回目录中的文件和子目录 |
| Markdown、JSON、JSONL、TXT | 返回指定范围的文本 |
| PDF | 返回 PDF 资源、路径和哈希，不返回二进制内容 |
| 图片 | 返回图片资源、路径和元数据 |
| 不支持的文件 | 返回结构化错误 |

宿主内部保留完整结果，用于预算、Checkpoint 和审计，例如：

```json
{
  "status": "success",
  "path": "wiki/papers/8272.md",
  "content": "……",
  "offset_chars": 0,
  "next_offset_chars": 20000,
  "truncated": true,
  "sha256": "..."
}
```

完整内部结果还包含 `kind`、`returned_chars`、`media_type`、结构化目录 `entries`，以及失败时的 `error_code` 和 `error`。SHA256、实际字符数、预算余额、规范化路径和路径安全细节只由宿主保存，不直接暴露给模型。

QA 模型只看到“精简 JSON 外壳 + 自然语言正文”。文本读取格式为：

```json
{
  "ok": true,
  "path": "wiki/papers/8272.md",
  "content": "这里是文件的自然语言内容……",
  "truncated": true,
  "next_offset": 20000,
  "evidence_ids": ["8272:s0064"]
}
```

完整读取时 `next_offset` 为 `null`。失败只返回稳定错误代码和可读说明：

```json
{
  "ok": false,
  "error": {
    "code": "PATH_OUTSIDE_ALLOWED_ROOTS",
    "message": "path must remain inside wiki/ or raw/papers/"
  }
}
```

完整目录返回可直接再次读取的项目相对路径列表；目录发生截断时改为带 `content`、`truncated` 和 `next_offset` 的分页结果，避免泄露预算外条目。PDF 和图片只向模型返回路径、资源类型和媒体类型，不返回二进制或 SHA256。

调用预算规则：未通过 JSON Schema 的参数不进入有效工具调用，也不消耗预算；通过 Schema 后的调用会消耗一次调用额度，即使随后因路径或文件类型失败。成功返回的文本字符计入累计字符预算；剩余额度小于请求长度时只返回剩余预算内的内容并标记 `truncated: true`。

### 4.3 宿主校验

JSON Schema 校验后，宿主继续检查：

- 路径只能位于 `wiki/` 或 `raw/papers/`；
- 禁止使用 `../` 越界；
- 禁止符号链接跳出允许目录；
- 文件必须存在；
- offset 和读取长度必须有效；
- 单次和单轮读取量不能超过预算。

错误调用返回结构化错误，由 QA 决定是否修正后重试。

## 5. QA 渐进式阅读流程

```text
接收用户问题
  ↓
加载会话状态和长期项目记忆
  ↓
判断任务类型和论文范围
  ↓
判断现有上下文是否足够
  ├─ 足够：生成回答草稿
  └─ 不足：调用 read_project_file
              ↓
            更新当前理解
              ↓
            再次判断是否足够
  ↓
执行回答校验和 Answer Review
  ↓
向用户返回回答
  ↓
宿主保存会话状态
```

### 5.1 领域概览

1. 读取 `wiki/indexes/overview.md`；
2. 获取选定论文的研究问题和核心思路；
3. 对论文进行主题或方法路线归纳；
4. 如果只需要粗粒度综述，可以直接生成回答；
5. 如果涉及具体方法，继续读取相关论文 Wiki。

### 5.2 方法比较

1. 根据入口简介确定相关论文；
2. 批量读取相关论文的方法；
3. 比较主要步骤、信息处理方式和模块关系；
4. 需要核对具体机制时读取 section evidence；
5. 数据集、任务或指标不同的论文不直接比较性能。

### 5.3 研究构思

1. 读取相关论文的方法；
2. 必要时读取原文解释和实验；
3. 分析不同方法解决的问题是否具有相似结构；
4. 提出可能的组合或迁移方案；
5. 明确标记为研究假设；
6. 说明可能的依赖、冲突和需要验证的实验。

### 5.4 原文请求

用户要求原文时：

1. 优先根据 evidence ID 读取 section evidence；
2. 返回原文内容、章节和页码；
3. 用户要求原始文件时返回 PDF 资源；
4. 需要图表时返回对应图片或 PDF 页面位置；
5. section evidence 不完整时继续读取 raw。

## 6. Evidence 使用规则

Wiki 内容引用 section evidence，例如：

```text
8272:s0064
```

该 ID 对应一个 `text_level: 2` 二级章节。

回答中的论文事实需要附带：

- `paper_id`；
- `evidence_id`；
- 必要时附带页码或原文摘录。

示例：

```markdown
P-HNet 使用个性信息引导视觉特征与视觉内容进行交互建模。

[paper:8272] [evidence:8272:s0064]
```

使用规则：

- 粗粒度概览可以使用 Wiki 已关联的 evidence；
- 具体方法、公式、指标和实验结果需要读取相应 evidence；
- 跨论文归纳需要引用参与归纳的多篇论文；
- 从 raw 找到的内容应转换回所属 section evidence ID；
- 无法转换时返回 PDF 页码和 raw block 位置，不编造 evidence ID；
- Wiki 没有提到某项内容时，不能直接断言论文没有，应按需检查 evidence 或 raw。

## 7. QA 回答结构

QA 最终生成结构化回答：

```json
{
  "type": "final",
  "answer": "面向用户的 Markdown 回答",
  "claims": [
    {
      "text": "P-HNet 使用个性信息进行条件引导。",
      "type": "paper_fact",
      "paper_ids": ["8272"],
      "evidence_ids": ["8272:s0064"]
    },
    {
      "text": "类似机制可能迁移到其他条件引导任务。",
      "type": "hypothesis",
      "paper_ids": ["8272"],
      "evidence_ids": ["8272:s0064"]
    }
  ],
  "cited_evidence_ids": ["8272:s0064"],
  "status": "answered",
  "memory_patch": {
    "research_goal": null,
    "paper_aliases": {},
    "confirmed_decisions": [],
    "unresolved_questions": [],
    "research_hypotheses": [],
    "evidence_ids": ["8272:s0064"]
  }
}
```

回答状态包括：

```text
answered
partially_answered
insufficient_evidence
```

当资料只能支持部分回答时，只返回受支持的部分，并指出仍缺少什么信息。

## 8. 会话记忆

会话记忆由宿主管理：

```text
memory/sessions/{session_id}/
├── state.json
├── messages.jsonl
└── summary.md
```

### 8.1 完整消息记录

`messages.jsonl` 保存：

- 用户消息；
- QA 回答；
- 工具调用；
- 工具结果；
- Answer Review 结果。

它用于审计和恢复，不会每轮全部加载到模型上下文。

### 8.2 工作状态

`state.json` 保存用户可读 Session 状态；完整工具正文仍只在消息审计中出现，不写入长期记忆：

```json
{
  "session_id": "session-1",
  "summary": "用户正在比较个性引导的视觉建模方法。",
  "memory": {
    "research_goal": "了解显著性预测方法",
    "paper_aliases": {
      "第一篇论文": "8272"
    },
    "confirmed_decisions": [],
    "unresolved_questions": [
      "该方法能否迁移到其他条件引导任务"
    ],
    "research_hypotheses": [
      "使用其他条件信息替换个性特征"
    ],
    "evidence_ids": ["8272:s0064"]
  },
  "read_resources": [
    {
      "path": "wiki/evidence/8272/s0064.md",
      "offset_chars": 0,
      "returned_chars": 8234,
      "sha256": "...",
      "evidence_ids": ["8272:s0064"]
    }
  ]
}
```

宿主根据真实工具调用自动记录文件路径、读取范围、文件哈希和已加载 evidence。QA 只能提交研究目标、主题、指代、未解决问题和候选想法等状态变更。

## 9. 长期记忆

长期记忆保存在：

```text
memory/
├── profile.json
└── projects/{project_id}/state.json
```

长期记忆保存：

- 用户研究方向；
- 用户回答风格和引用偏好；
- 用户确认的项目决策；
- 常用论文集合；
- 跨会话未完成的问题；
- 明确标记为假设的研究构思。

长期记忆不保存：

- 完整论文和 raw；
- 完整工具返回内容；
- 没有 evidence 的论文事实；
- QA 的临时猜测；
- 可以从 Wiki 重新读取的内容。

第一版不使用向量数据库，也不提供记忆读写工具。宿主在每轮开始时加载当前项目记忆，在结束时校验并应用 QA 返回的 `memory_patch`。

## 10. 上下文压缩

每轮上下文包含：

```text
系统规则和工具定义
＋
长期项目记忆
＋
当前会话状态
＋
历史对话摘要
＋
最近四轮完整对话
＋
当前用户问题
＋
本轮工具结果
```

压缩规则：

- 始终保留最近四个完整交互轮次；
- 更早对话写入结构化 `summary.md`；
- 完整消息继续保存在 `messages.jsonl`；
- 旧工具大文本不继续放入上下文；
- 工具内容只保留路径、哈希和 evidence ID；
- 需要原文时重新读取文件；
- 用户确认的决策、论文指代、未解决问题和 evidence ID 不得在压缩时丢失。

预计下一轮输入达到模型上下文约 60% 时，压缩最早对话，为新工具结果和回答保留空间。

Wiki 或 raw 文件哈希变化后，对应的“已读取”状态失效，QA 必须重新读取。

## 11. Answer Review

QA 生成回答后，宿主先执行确定性校验：

- evidence ID 是否存在；
- evidence 是否属于对应论文；
- `paper_fact` 是否有 evidence；
- `cross_paper_synthesis` 是否引用相关论文；
- 引用是否来自当前 Wiki/raw 版本。

之后由 Answer Review 检查：

- evidence 是否支持对应陈述；
- 是否把研究假设写成论文事实；
- 是否误解方法内容；
- 是否忽略任务、数据集和指标差异；
- 是否回答了用户问题。

Review 拒绝时，将问题返回 QA。QA 保留当前上下文，可以继续读取资料或修改回答。

建议最多修复两次。仍不能通过时，返回受支持的部分，或者明确说明证据不足。

## 12. 读取预算

- 单次默认读取 20,000 字符；
- 单次最多读取 50,000 字符；
- 单轮累计读取量根据模型上下文动态计算；
- 工具结果最多使用约 60% 的上下文窗口；
- 为当前问题、系统规则和回答保留剩余空间；
- 相同路径、offset、长度和哈希的读取结果可以缓存；
- 达到预算后，QA 不再继续读取，并根据当前证据回答或返回证据不足。

## 13. 完整流程

```text
用户提问
  ↓
宿主加载会话状态和长期项目记忆
  ↓
启动 QA Agent
  ↓
QA 判断任务类型、论文范围和所需资料
  ↓
调用 read_project_file
  ↓
读取 Wiki 入口、论文正文、section evidence 或 raw
  ↓
QA 判断信息是否足够
  ├─ 不足且有预算：继续读取
  ├─ 不足且无预算：返回证据不足
  └─ 足够：生成结构化回答
  ↓
宿主执行引用与 Schema 校验
  ↓
Answer Review
  ↓
拒绝 → 反馈给 QA → 补读或修改回答
  ↓
允许
  ↓
返回用户
  ↓
宿主保存消息、会话状态和长期记忆更新
```

## 14. 组件职责

| 组件 | 职责 |
| --- | --- |
| QA Agent | 决定读取内容、调用工具、理解论文并生成回答 |
| `read_project_file` | 受控读取 Wiki 和 raw |
| 宿主程序 | 工具校验、路径安全、预算、状态持久化和引用校验 |
| Answer Review | 审核回答与 evidence 的语义一致性 |
| 会话记忆 | 保存当前对话、已读资料和未解决问题 |
| 长期记忆 | 保存用户研究方向、项目决策和研究假设 |

## 15. Checkpoint 与恢复

QA Graph 可以通过 `interrupt_before` 在模型或工具节点前暂停，返回 `thread_id`、下一节点和当前读取预算。`resume_qa()` 在新 runtime 中核对 workspace、thread 与 `llm_mode` 后继续执行；终态重复 resume 直接返回已保存结果，不再次调用模型或工具。

每个模型步骤原子保存 `qa-output-{step}.txt`，每次工具调用原子保存 request 与完整 result。若副作用已经持久化但 Graph Checkpoint 尚未推进，恢复节点会校验请求并重放结果；工具预算与已读资源因此只更新一次。瞬时模型或工具异常会保留失败节点之前的 Checkpoint，并返回可重试的 `interrupted`。非法 JSON、伪造 Evidence 和审计请求冲突仍失败关闭。

远程模型请求在响应尚未写入运行目录的在途窗口可能重发；除非 Provider 提供幂等键，否则不承诺计费级 exactly-once。Checkpoint 仍不代替 `messages.jsonl`、`state.json`、`summary.md` 等用户可读 Session 契约。

