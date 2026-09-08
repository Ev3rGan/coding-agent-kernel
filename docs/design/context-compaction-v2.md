# Context Compaction v2：语义 checkpoint、近期窗口与权威上下文重投影

**Status:** Proposed implementation design

**Issue:** #33

**Baseline:** `main@dae4237029f0fdaf95a0064ad5f2a17be827db54`

## 1. 结论

现有调研已经完成，下一步应把结论收敛为可实现和可验收的 Context Compaction v2，而不是继续研究三套 Agent。

总体方向是：以 Pi 的 Compaction 机制作为主干，用 Codex 的独立 checkpoint 思路强化数据边界，用 Claude Code 的来源分层与资源重载思路强化权威上下文，再结合本 Kernel 已有的 provider-neutral Context、append-only Session、Extension hook 和 replay validation，形成可审计、可恢复、可重复压缩的实现。

这不是四个并列补丁，而是一条一致的设计：

> Pi 负责 checkpoint、近期原文、安全切分和增量更新的主干；Codex 强化 checkpoint 独立性；Claude Code 强化来源分层和权威资源重载；本 Kernel 用 provider-neutral typed sidecar、replay validation 和现有 Extension seam 将它们收敛成可审计实现。

## 2. 当前问题的精确位置

Ticket 04 / #5 已经建立：

- 单一 `ContextPipeline`；
- Active Branch 投影；
- append-only Session tree；
- 独立的 `kind="compaction"` SessionEntry；
- checkpoint coverage validation；
- 摘要失败发生在正常 Coding Turn 的 Provider 请求之前；
- 失败时不持久化部分 checkpoint，并保持 Session 可恢复。

因此，当前问题不是“Compaction 没有独立 SessionEntry”。真正的问题是旧 checkpoint 在下一次压缩时失去了独立身份：

1. 最新 checkpoint 被投影成 `BranchSummaryMessage`；
2. 再次压缩时，summarizer 接收到“旧 summary + checkpoint 后的新消息”；
3. 当前实现把旧 summary 再序列化为 `summary:...`；
4. 全部内容重新拼接，并从字符 0 截断；
5. 最终出现 `summary:summary:user:...`，同时丢失最近发现、修改、测试结果和下一步。

当前默认 summarizer 因而只是有界截断，不是语义压缩。Django 保存记录已经显示：压缩前 Agent 知道修改文件和验证结果，压缩后却重新开始理解任务和检查仓库。

需要继续保持三个概念的区分：

- Session tree 中保留原始消息和派生 checkpoint 是 append-only 审计语义，不是缺陷。
- checkpoint 与原始历史存在派生重合，不等于同一个 Provider request 同时重复发送 covered raw prefix。
- v2 要修复的是 checkpoint 的语义价值、重复压缩方式和近期原文保留方式，而不是删除权威历史。

## 3. 设计原则

### 3.1 一个 canonical Context pipeline

`ContextPipeline` 继续是唯一的 Model Context 装配 interface。不能新增 Provider-specific ContextBuilder，也不能让 CLI、SWE-bench evaluator 或 Extension 各自复制压缩规则。

内部可以把压缩深化为独立 Module，但调用者仍只需要提供当前 Context snapshot，并取得最终 `ProviderRequest`、可选 `CompactionPlan` 和可观察元数据。

### 3.2 checkpoint 在数据模型中独立

Compaction checkpoint 在 Session、compaction interface 和 replay schema 中都是独立对象。Provider Adapter 最终可能仍需把它编码为模型可消费的 message，但这只是传输格式；它不能重新成为下一轮 summarizer 的普通 conversation message。

重复压缩必须采用：

```text
previous checkpoint state
        +
newly covered conversation span
        ↓
incremental checkpoint update
```

不能再采用：

```text
old summary rendered as a message
        +
all projected messages
        ↓
concatenate and truncate again
```

### 3.3 权威上下文重新投影

每次模型请求都从当前 Kernel/Host/Environment/Extension 状态重新构造权威层。旧 summary 不能成为这些信息的来源。

权威层至少包括：

- 当前 system prompt；
- 当前 Tool guidelines 与 active Tool schemas；
- 当前 project resources；
- 与模型决策有关的当前 Permission Mode 和环境能力；
- 当前相关、由 Extension 提供的 instructions、Skill 或资源。

当前实现已经有 `ContextSettings.system_prompt`、`tool_guidelines`、`project_context`、`active_tools` 和 `ContextSupplement` 的承载位置。v2 应深化这些现有 seam，不建立第二套 ContextBuilder。

### 3.4 conversation facts 才由 Compaction 表示

Compaction 表示对话产生的工作状态：

- 当前目标；
- 用户约束与偏好；
- 已完成并验证的工作；
- 当前进行中事项与 blocker；
- 仍然相关的关键决策；
- 为避免重复尝试而需要保留的 rejected approach；
- 精确路径、symbol、error；
- 下一步动作。

早期探索结论不是权威 instruction。新的证据推翻旧判断后，增量摘要应删除旧判断或标记为 superseded，而不是永久累积。只有仍有助于避免重复错误的失败路线才继续保留。

### 3.5 近期完整 turns 提供局部保真

语义 checkpoint 提供全局连续性，近期原文提供局部精度。Model Context 使用：

```text
current authoritative layer
        +
one latest checkpoint
        +
recent complete turns
        +
current injected messages
        ↓
one ContextPipeline
        ↓
ProviderRequest
```

从最新 entry 反向选择近期 turns，直到加入下一完整 turn 会超过剩余 character budget。cut point 优先位于完整 user turn 边界；Assistant ToolCall 与对应 ToolResult 是不可拆交易单元。

若单个 turn 本身超出预算，应先对可再生的长 Tool output 做类型感知 shedding，同时保留退出状态、错误、路径和关键输出。仍无法形成安全 cut point 时产生结构化失败，不能在 ToolCall/ToolResult 中间或序列化文本中间任意截断。

## 4. Context 来源分层与 Extension/Skill

当前系统并非没有权威规则或项目资源，而是已有承载位置、缺少完整的来源和生命周期表达。

建议在 canonical pipeline 内表达以下逻辑来源：

| 来源 | 示例 | Compaction 行为 | 刷新方式 |
| --- | --- | --- | --- |
| Kernel authority | system prompt、Tool guidelines | 不进入摘要 | 每次请求重新投影 |
| Runtime authority | active tools、Permission Mode、环境能力 | 不进入摘要 | 从当前运行态重新投影 |
| Project authority | repository instructions、项目资源 | 不进入摘要 | 从当前资源版本重新加载 |
| Extension authority | 当前相关 Skill/instructions | 不进入摘要 | Extension 每次或按需重新提供 |
| Conversation | user、assistant、ToolCall、ToolResult | checkpoint + recent turns | 按 coverage 投影 |
| Current input | user、steering、follow-up 注入 | 不被当前 checkpoint 覆盖 | 当次请求注入 |

Extension 加载 Skill 时，不应把 Skill 全文作为普通 user message 写入 Session，再依赖 summary 保存。更合理的规则是：

- Extension 按当前任务或显式调用加载相关 Skill；
- Skill 作为带 source、authority、resource ID 和 revision/hash 的当前资源进入权威层；
- Compaction 后仍需要时重新加载当前版本；
- 不机械加载所有 Skill；
- Skill 内容参与整体 character budget，但不参与 conversation summarization；
- pipeline 在选择近期 turns 前为权威资源和当前输入保留预算。

当前 context hook 位于初始 compaction 之后。v2 需要确保 Extension 权威资源在最终预算分配中可见，同时仍处于同一 canonical pipeline。实现可以调整 hook 阶段或引入 typed resource supplement，但不能建立平行 Context builder。

## 5. Compaction Module

当前浅 interface：

```python
BranchSummarizer.summarize(messages) -> str
```

应深化为 provider-neutral 的异步 Compaction Module。概念 interface 为：

```text
CompactionInput
├── previous_checkpoint
├── newly_covered_entries
├── retained_recent_entries
├── current authoritative metadata
├── character budget
└── optional focus

CompactionEngine.compact(...)
        ↓
CompactionPlanV2
├── semantic summary
├── typed evidence sidecar
├── coverage / first-kept boundary
├── checkpoint lineage
└── strategy/version/size metadata
```

生产实现通过当前 `ModelProvider` seam 发起无 Tool 的语义摘要请求；测试可以使用确定性的 Fake Provider。摘要请求是 Compaction Module 内部的 Provider 操作，不经过正常 Coding Turn 的 Context recursion。

语义摘要使用稳定结构：

- Goal
- Constraints and preferences
- Done and verified
- In progress
- Blocked
- Key decisions and relevant rejected approaches
- Exact files, symbols, commands and errors
- Next steps

summary 是给模型继续任务的语义状态，不是审计真相。可由程序从 ToolCall/ToolResult 中确定提取的内容进入 typed evidence sidecar，例如：

- read files；
- modified files；
- command、cwd、exit status；
- Tool error code；
- coverage 和 checkpoint lineage。

sidecar 记录“已观察或已执行的证据”，不把无法由运行记录证明的推断伪装成权威 workspace 状态。

## 6. Compaction checkpoint v2

建议的持久字段至少包括：

```text
version: 2
summary: string
covered_entry_ids: string[]
first_kept_entry_id: string | null
previous_checkpoint_id: string | null
evidence:
  read_files: string[]
  modified_files: string[]
  commands: [{command, cwd, exit_status}]
  tool_errors: [{call_id, code}]
strategy:
  name: string
  revision: string
metrics:
  characters_before: int
  characters_after: int
  covered_count: int
  retained_count: int
  compaction_depth: int
```

精确字段名可在实现中按现有 dataclass/JSON contract 收敛，但必须保持这些职责，不得重新退化为只有 `summary` 和累计 ID 列表的字符串 checkpoint。

验证规则：

- coverage 是所选 Active Branch 的合法旧前缀；
- `first_kept_entry_id` 指向保留近期 span 的起点；
- 新 coverage 包含 previous checkpoint coverage，并只增加本次新覆盖 span；
- previous checkpoint 通过独立 lineage 字段关联，不出现在 summarizer conversation input 中；
- ToolCall/ToolResult 不跨越 cut point；
- 最新 checkpoint 是 Model Context 中唯一 active summary；
- retained recent entries 可以位于最新 checkpoint entry 之前，projection 必须根据 `first_kept_entry_id` 回取，并跳过旧 checkpoint entries；
- checkpoint、最终 Model Context 和持久化 payload 使用同一份已验证 plan；
- 预算、schema、coverage 或 Extension transform 失败时不写入部分 checkpoint。

## 7. 重复压缩与过时事实

第二次及后续压缩只输入：

1. previous checkpoint 的结构化状态；
2. previous coverage 之后、本次 cut point 之前的新 conversation span；
3. 当前 summary focus（如有）。

更新规则：

- 较新验证结果替换较旧结果；
- 明确被推翻的假设删除或进入 relevant rejected approaches；
- 已解决 blocker 从 Blocked 移入 Done；
- 已完成 next step 不继续作为下一步；
- 当前文件、symbol、错误与测试结果优先采用新证据；
- 不复制 `summary:` wrapper；
- 连续压缩后若 context size 没有显著下降，报告 compaction thrashing 并停止自动重试。

Durable Session 可以保留 checkpoint lineage，但 Provider request 只看最新 checkpoint 和相应近期 raw span。

## 8. 失败、原子性与 Provider 行为

语义摘要会引入一次真实 Provider 操作。其失败边界必须显式：

- summary Provider 调用开始、成功和失败可观察；
- summary 调用不启用 Tool；
- failure、timeout、cancel、空摘要、超预算和非法 schema 都产生结构化 Compaction failure；
- 正常 Coding Turn 的 Provider 请求只在 checkpoint 生成、Extension transform、Session validation 和最终 Context validation 成功后开始；
- 失败不持久化 checkpoint，不删除 raw history，Session 保持可恢复；
- 不静默回退到当前前缀截断；
- Provider-specific message mapping 留在 Adapter 内，Kernel compaction contract 保持 provider-neutral。

## 9. Replay 与兼容性

实现必须继续读取已有 version 1 checkpoint：

- v1 entry 按当前规则恢复和投影；
- 新 checkpoint 一律写 version 2；
- v1 后发生下一次压缩时，将 v1 summary 作为 legacy previous state 输入，生成 v2 checkpoint；
- 不原地重写旧 Session；
- 非法 v2 lineage、coverage、first-kept、sidecar 或 metrics 在 replay 时明确拒绝；
- Session schema 或 Extension compaction hook 的兼容变化必须有聚焦测试和迁移说明。

## 10. 可观察性

Host 和 artifact 至少可以检查：

- trigger：automatic/manual；
- checkpoint ID 和 previous checkpoint ID；
- characters before/after；
- covered/retained entry count；
- compaction depth；
- strategy/revision；
- summary Provider usage；
- start/succeeded/failed；
- failure code/stage；
- 是否触发 thrashing guard。

敏感 prompt、Provider credential 和不必要的完整 Tool output 不进入公开事件或 artifact。

## 11. 实现验证与最终验收

### 11.1 开发和 PR 门禁

Fake Provider、pytest、strict typing、lint、format、replay fixtures 和 CLI demo 仍然必要，用于确定性证明：

- v2 schema 和 v1 replay；
- safe cut point；
- latest checkpoint + recent raw projection；
- 二次、三次增量压缩；
- 无递归 `summary:summary:`；
- 权威资源每次重新投影；
- Extension/Skill resource 不进入 conversation summary；
- superseded hypothesis 更新；
- summary failure 在正常 Coding Turn Provider 请求前停止且保持 Session 可恢复；
- Django artifact-shaped continuation fixture 在压缩后仍保留目标、修改文件、验证结果、blocker 和 next step。

这些门禁只能证明实现契约，不能关闭 #33。

### 11.2 Issue 关闭门禁

#33 在实现 PR 合并后继续保持 OPEN。只有以下 post-merge 验收全部满足后才能关闭：

1. 从 exact merged `main` 构建或安装候选版本。
2. 使用公开 `python -m coding_agent swebench run` 路径、真实 credentialed DeepSeek Adapter 和官方 SWE-bench Harness。
3. 选择至少两个彼此不同、且不同于既有 `django__django-11133` 的 SWE-bench Verified 实例。
4. 每个实例都必须在 Agent Run 中实际产生至少一个有效 v2 Compaction checkpoint；不能只运行短任务然后宣称 Compaction 可用。
5. 若自然上下文长度不能稳定触发压缩，CLI 应提供正常的、记录到 manifest 的 Context character-budget 配置，使验收可以在真实 Provider 路径中确定性触发 Compaction；不得使用仅测试可见的 seam。
6. 每个实例的官方 Harness 结果都必须同时满足：
   - `FAIL_TO_PASS`: PASS；
   - `PASS_TO_PASS`: PASS。
7. 每个运行 artifact 必须保留无 secret 的配置、Session、compaction/event 记录、ToolResult、prediction、patch 和官方 Harness report。
8. Session 与事件证据必须证明 latest checkpoint、近期 raw span、coverage/lineage 合法，且没有 recursive summary wrapper。
9. checkpoint 后 Agent 继续当前工作状态，没有因摘要丢失而重新开始已完成的仓库探索。
10. 两个实例的 post-merge 证据、exact main SHA 和 artifact 摘要发布到 #33 后，才能关闭 Issue。

一个实例失败、未触发 Compaction、只有 `FAIL_TO_PASS` 通过、只有 `PASS_TO_PASS` 通过、使用 Fake Provider、只通过 pytest，均不满足关闭条件。

## 12. 实施顺序

1. 用 Django-shaped fixture 固定当前递归 summary 和状态丢失的 red behavior。
2. 引入 v2 checkpoint schema、lineage、first-kept 和 backward-compatible replay。
3. 实现近期完整 turn 选择、安全 cut point 和 Tool output shedding。
4. 深化 Compaction Module，并实现 previous checkpoint + new span 的语义更新。
5. 增加 typed evidence sidecar 和过时事实更新规则。
6. 把权威 Context、Extension/Skill resource 和 conversation budget 纳入同一 pipeline。
7. 增加可观察性、failure atomicity 和 thrashing guard。
8. 完成确定性测试、全量质量门禁和安装后 CLI 验收。
9. 由 Git custody 发布、审阅并合并实现 PR；PR 使用 `Relates to #33`，不自动关闭 Issue。
10. 从 exact merged main 完成两个额外 Verified 实例的真实 post-merge 验收，再决定关闭 #33。

## 13. 非目标

- 删除、改写或去重 append-only Session 中的权威 raw history；
- 长期记忆、自动记忆抽取、向量检索、RAG、知识图谱或多 Agent 记忆共享；
- 第二套 ContextBuilder；
- Provider prompt/model benchmark；
- 复制 Codex opaque encrypted compaction item；
- 把 Claude Code 未公开内部算法当成事实；
- SWE-bench 排名、固定得分目标或 benchmark-specific Kernel prompt；
- 重构 Agent Run control、Session branching、Permission、DeepSeek 或 evaluator 中与本问题无关的行为；
- 仅为了减少 cumulative `covered_entry_ids` 字节数而进行独立存储优化；除非 v2 schema 迁移自然需要窄调整。

## 14. English summary

Context Compaction v2 keeps Pi's semantic-checkpoint, recent-turn window, safe cut-point, and incremental-update spine. It adds Codex-style checkpoint identity, Claude Code-style authoritative-context reloading, and Kernel-owned typed evidence, replay validation, and Extension resource layering.

The latest checkpoint remains a distinct Session record and compaction input. Repeated compaction receives the previous checkpoint plus only the newly covered conversation span; it never reserializes the old summary as an ordinary message. Each Provider request rebuilds current system instructions, tools, project resources, runtime facts, and relevant Extension/Skill resources. Only conversation facts are summarized.

Deterministic tests and Fake Providers are development gates, not completion evidence. Issue #33 remains open after the implementation PR merges. Closure requires two distinct additional SWE-bench Verified instances, each run from the exact merged main through the real credentialed DeepSeek Adapter, each demonstrably triggering v2 Compaction, and each passing both `FAIL_TO_PASS` and `PASS_TO_PASS` in the official Harness.
