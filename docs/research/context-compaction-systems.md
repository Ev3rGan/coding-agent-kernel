# Codex、Claude Code 与 Pi 的上下文压缩机制研究

**Status:** Research snapshot

**Date:** 2026-09-08

**Scope:** 调查三套 Coding Agent 的记忆压缩／上下文压缩机制，并核对本仓库 SWE-bench Verified `django__django-11133` 运行的实际 compaction 证据；本文给出优化优先级，但不修改产品代码。

## 证据边界

本文使用三种证据标签：

- **官方确认**：产品官方文档或官方仓库源码直接陈述。
- **源码推断**：可由公开客户端控制流推出，但不是厂商对服务端算法的承诺。
- **未知**：公开材料不足，不能把相邻 API 或经验现象当成产品实现。

研究快照固定在以下版本或页面：

- Codex：本机 `codex-cli 0.153.4`，对应官方 tag [`rust-v0.153.4`](https://github.com/openai/codex/tree/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a)（commit `3d2ee51ca2d5db578f328aa75e20aa22c0197c9a`）。
- Claude Code：官方文档当前页面；官方 [`anthropics/claude-code`](https://github.com/anthropics/claude-code/tree/ab9b2cf7bb9e4f98ff264c07a22e46d83c29c558) 仓库快照不包含核心 runtime，因此不能从该仓库恢复精确摘要 prompt 或切分算法。
- Pi：官方仓库已从 `badlogic/pi-mono` 迁移至 [`earendil-works/pi`](https://github.com/earendil-works/pi/tree/b2602be77cb7b0de45dd616407fd210daa48aa75)，本文固定 commit `b2602be77cb7b0de45dd616407fd210daa48aa75`。

## 结论先行

用户对“拼接后裁剪”的质疑方向正确：它是**截断**，不是语义压缩；它没有把分散的目标、约束、已验证事实、决策理由和下一步重新组织成更小但更有用的状态。

但“Session tree 中原文与 checkpoint 摘要同时存在”本身不构成缺陷。三套系统都不同程度地区分：

1. **权威、可恢复的历史**：用于审计、恢复、分支或重放；
2. **派生的有界 Model Context**：用于下一次推理。

真正需要守住的不变量是：checkpoint 覆盖的旧前缀不应再以原文进入同一次 Model Context；Model Context 中应只出现一次 checkpoint，再加未覆盖的近期原文。存储层保留“原文 + 派生摘要”是可解释的 materialized checkpoint，而不是模型输入重复。

三套系统最一致的可借鉴方向是：**语义摘要／不透明语义状态 + 近期原文窗口 + 权威上下文重新注入 + 明确覆盖边界**。其中 Pi 的公开实现最适合本仓库借鉴；Codex 的服务端 compaction 内容不可解释，不宜直接复制成 provider-neutral Kernel 的核心契约。

## 1. Codex

### 已证实机制

Codex 0.153.4 不是只有一条摘要路径，而是按 Provider 能力选择实现：OpenAI/Azure Responses Provider 声明支持 Remote Compaction V2；该 feature 在此版本已是 stable 且默认开启，其他 Provider 回退到本地摘要路径。参见 [`provider.rs` L353-L364](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/model-provider/src/provider.rs#L353-L364)、[`features/src/lib.rs` L1682-L1687](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/features/src/lib.rs#L1682-L1687) 和 [`turn.rs` L1219-L1295](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/session/turn.rs#L1219-L1295)。

自动压缩在配置的 auto-compaction budget 或完整 context window 被耗尽时触发；模型切换导致 compaction compatibility hash 改变，或切到更小窗口模型时，也可能在新采样前触发。参见 [`turn.rs` L1053-L1080](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/session/turn.rs#L1053-L1080) 和 [`turn.rs` L1117-L1209](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/session/turn.rs#L1117-L1209)。

默认 Remote V2 客户端路径会：

1. 取当前 prompt history；必要时先把过大的 function/tool outputs 改写为截断占位；
2. 在输入末尾添加专门的 `CompactionTrigger`，而不是普通用户“请总结”消息；
3. 要求响应中出现一个 `Compaction` item；该 item 的载荷是 `encrypted_content`；
4. 从原输入保留有界的真实用户消息／特定 Agent 消息，再追加新的 compaction item；默认 retained-message budget 为 64k tokens，优先保留最新内容；
5. 从当前 Session 重新注入 canonical initial context，并将 replacement history 安装为新的 live history。

对应源码为 [`compact_remote_v2_attempt.rs` L39-L85](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/compact_remote_v2_attempt.rs#L39-L85)、[`compact_remote_v2.rs` L295-L357](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/compact_remote_v2.rs#L295-L357)、[`compact_remote_v2.rs` L485-L514](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/compact_remote_v2.rs#L485-L514)、[`compact_remote_v2.rs` L539-L570](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/compact_remote_v2.rs#L539-L570)、[`compact_remote_v2.rs` L590-L684](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/compact_remote_v2.rs#L590-L684) 和 [`models.rs` L1193-L1204](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/protocol/src/models.rs#L1193-L1204)。

Codex 同时保留透明的本地回退路径。它让模型生成一份面向“另一个将继续任务的 LLM”的 handoff summary，要求包含当前进展、关键决策、约束／偏好、剩余工作和关键数据；随后保留最近真实用户消息（上限 20k tokens）和这份 summary，而不是简单保留任意字符串尾部。参见官方 [`compact/prompt.md` L1-L9](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/prompts/templates/compact/prompt.md#L1-L9)、[`compact.rs` L352-L385](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/compact.rs#L352-L385) 和 [`compact.rs` L645-L733](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/compact.rs#L645-L733)。

压缩不会把 durable rollout 原地改写成只有摘要。客户端把 replacement history、窗口编号、前后窗口 ID、compaction response ID 等写入一个新的 `CompactedItem` checkpoint，再切换 live history。参见 [`session/mod.rs` L3752-L3813](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/session/mod.rs#L3752-L3813)。

OpenAI 的 Responses 文档对服务端 compaction 的公开契约是：返回 loss-aware、加密、不透明的 item，用于 continuation；调用方不应解析或依赖其内部结构。`/responses/compact` 的返回形状是“所有用户消息 + 一个 compaction item”。参见 [Model guidance: Compaction](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.2) 和 [Compact a response](https://developers.openai.com/api/reference/java/resources/responses/methods/compact)。

### 未知与限制

- **未知：** Remote V2 的服务端如何选取事实、如何处理冲突、是否使用与公开 `/responses/compact` 完全相同的内部算法。公开 Codex 客户端只看到 `encrypted_content`；不能把 Responses API 的“loss-aware”描述进一步外推成可检查的 Codex 摘要 schema。
- **已知限制：** 本地回退路径在“压缩请求本身已超窗”时会从最旧 history item 开始删除后重试，因此极端情况下最早事实可能根本没有进入摘要。参见 [`compact.rs` L314-L323](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/compact.rs#L314-L323)。
- **已知限制：** Codex 源码明确提示，长 thread 和多次 compaction 会降低模型准确性；compaction 不能被当作无限无损记忆。参见 [`compact.rs` L392-L399](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/compact.rs#L392-L399)。

### 可借鉴点

- 将 compaction 作为独立协议项／checkpoint，而不是伪装成普通对话消息。
- 把当前权威 instructions、环境与动态状态从 Session 重新投影；不要让旧摘要永久携带可能已过期的 developer/context 内容。
- 保留有界的近期原文，并按“最新优先”执行预算，而不是只依赖摘要。
- 为 model switch、manual/auto trigger、before/after token usage、失败状态建立可观测元数据。
- 不应照搬 opaque item：它不利于 provider-neutral 实现、调试、回放验证和质量评测。

## 2. Claude Code

### 已证实机制

Claude Code 接近 context limit 时自动管理上下文：先清除较旧的 tool outputs，再在仍有需要时总结 conversation。官方文档明确提醒，用户请求和关键代码片段会尽量保留，但早期详细指令可能丢失。参见 [How Claude Code works: When context fills up](https://code.claude.com/docs/en/how-claude-code-works#when-context-fills-up)。

`/compact [instructions]` 可手动触发并提供 summary focus；`/rewind` 还能只总结从某一点开始或到某一点为止的区间。自动阈值可通过 `/autocompact` 或配置调整。参见 [Commands](https://code.claude.com/docs/en/commands) 和 [Explore the context window](https://code.claude.com/docs/en/context-window#when-your-context-fills-up)。

压缩后并非所有内容都依赖摘要存活。Claude Code 对不同来源采用不同重载策略：system prompt 不属于 message history；project-root `CLAUDE.md`、unscoped rules、auto memory 和 plan 从磁盘重新注入；最近修改的少量文件会重新读取；path-scoped rules、nested `CLAUDE.md` 和 skill bodies 按各自规则重载。参见 [What survives compaction](https://code.claude.com/docs/en/context-window#what-survives-compaction)。

Claude Code 还暴露 `PreCompact`、`PostCompact` 和 `SessionStart(source="compact")` lifecycle。`PreCompact` 可以区分 manual/auto 并读取自定义指令，`PostCompact` 能读取生成的 `compact_summary`，`SessionStart` 可在压缩后重新注入动态上下文。参见 [Hooks reference: PreCompact](https://code.claude.com/docs/en/hooks#precompact)、[PostCompact](https://code.claude.com/docs/en/hooks#postcompact) 和 [SessionStart](https://code.claude.com/docs/en/hooks#sessionstart)。

Session 的每条消息、tool use 和 result 会写入 `~/.claude/projects/` 下的 JSONL；这说明 durable session 与即时 context window 是不同职责，但官方文档没有公开 compaction entry 的完整 JSONL schema。参见 [How Claude Code works: Work with sessions](https://code.claude.com/docs/en/how-claude-code-works#work-with-sessions)。

### 未知与限制

- **未知：** 核心 runtime、精确 summarization prompt、默认 summary schema、cut point 和重复摘要合并算法没有在官方仓库公开；不能根据压缩后的表象反推实现。
- **已知限制：** 官方文档承认早期 conversation-only instructions 可能丢失；因此关键规则必须进入可重新注入的 `CLAUDE.md`／memory，而不能只赌摘要。
- **已知限制：** 如果一个文件或 tool output 大到压缩后立刻再次填满窗口，Claude Code 在若干次后停止 auto-compaction 并报错，避免无限 thrashing。

### 可借鉴点

- 先做低成本、类型感知的 source shedding（尤其旧 tool output），再做语义摘要。
- 对上下文按来源分层：权威规则／项目资源重新加载，conversation facts 才由 compaction 表示。
- 支持用户给 summary focus，必要时支持“只压缩指定区间”，不要永远自动猜重要性。
- 暴露 pre/post lifecycle 和防 thrashing 机制；连续压缩却不能显著降载时应失败并给出原因。
- 大规模探索可隔离到独立 context（例如 subagent），只把最终摘要带回主 context；这是减少待压缩噪声，而不是事后补救。

## 3. Pi

### 已证实机制

Pi 同时有 compaction 和 branch summarization，但二者用途不同：前者在超阈值或 `/compact` 时压缩当前 active path 的旧部分，后者在 `/tree` 导航时把离开的分支信息注入目标分支。参见官方 [`compaction.md` L14-L23](https://github.com/earendil-works/pi/blob/b2602be77cb7b0de45dd616407fd210daa48aa75/packages/coding-agent/docs/compaction.md#L14-L23) 和 [`compaction.md` L150-L179](https://github.com/earendil-works/pi/blob/b2602be77cb7b0de45dd616407fd210daa48aa75/packages/coding-agent/docs/compaction.md#L150-L179)。

Compaction 的核心不是“全 branch 拼接后截尾”，而是“旧前缀语义摘要 + 近期原文窗口”：

1. 从最新 entry 反向累计 token estimate，保留约 `keepRecentTokens`（默认 20k）的近期内容；
2. 只摘要 cut point 之前的旧内容；
3. 生成结构化 summary；若已有 checkpoint，则把上一份 summary 作为独立的 `previousSummary` 与新覆盖消息合并；
4. 追加含 `summary`、`firstKeptEntryId`、`tokensBefore` 和 details 的 `CompactionEntry`；
5. 下一次 Model Context 只取最新 compaction entry、从 `firstKeptEntryId` 开始的近期原文以及 checkpoint 后新增 entries，covered prefix 不再进入模型。

参见 [`compaction.md` L27-L47](https://github.com/earendil-works/pi/blob/b2602be77cb7b0de45dd616407fd210daa48aa75/packages/coding-agent/docs/compaction.md#L27-L47)、[`compaction.ts` L388-L455](https://github.com/earendil-works/pi/blob/b2602be77cb7b0de45dd616407fd210daa48aa75/packages/coding-agent/src/core/compaction/compaction.ts#L388-L455)、[`compaction.ts` L750-L828](https://github.com/earendil-works/pi/blob/b2602be77cb7b0de45dd616407fd210daa48aa75/packages/coding-agent/src/core/compaction/compaction.ts#L750-L828) 和 [`session-manager.ts` L411-L453](https://github.com/earendil-works/pi/blob/b2602be77cb7b0de45dd616407fd210daa48aa75/packages/coding-agent/src/core/session-manager.ts#L411-L453)。

Pi 在安全 cut point 上有显式规则：通常在完整 turn 边界切；不能把 tool result 与对应 tool call 拆开。若单个 turn 已超过近期预算，Pi 把“更早历史”和“当前 turn 前缀”分别总结，再组合成 checkpoint。参见 [`compaction.md` L83-L119](https://github.com/earendil-works/pi/blob/b2602be77cb7b0de45dd616407fd210daa48aa75/packages/coding-agent/docs/compaction.md#L83-L119) 和 [`compaction.ts` L887-L946](https://github.com/earendil-works/pi/blob/b2602be77cb7b0de45dd616407fd210daa48aa75/packages/coding-agent/src/core/compaction/compaction.ts#L887-L946)。

默认摘要是明确的 semantic/state handoff，固定字段包括 Goal、Constraints & Preferences、Done/In Progress/Blocked、Key Decisions、Next Steps 和 Critical Context，并要求保留精确 file path、function name 和 error message。重复压缩使用 update prompt，显式要求保留旧摘要并合并新进展。参见 [`compaction.ts` L467-L539](https://github.com/earendil-works/pi/blob/b2602be77cb7b0de45dd616407fd210daa48aa75/packages/coding-agent/src/core/compaction/compaction.ts#L467-L539) 和 [`compaction.ts` L655-L693](https://github.com/earendil-works/pi/blob/b2602be77cb7b0de45dd616407fd210daa48aa75/packages/coding-agent/src/core/compaction/compaction.ts#L655-L693)。

Pi 没有只依赖自然语言摘要保存文件状态。它从 tool calls 和既有 summary details 累积 `readFiles`／`modifiedFiles`，作为确定性 sidecar 附在 checkpoint 上；摘要输入中的每个 tool result 最多序列化 2000 characters，避免大输出吞噬 summarization request。参见 [`compaction.md` L181-L187](https://github.com/earendil-works/pi/blob/b2602be77cb7b0de45dd616407fd210daa48aa75/packages/coding-agent/docs/compaction.md#L181-L187)、[`compaction.ts` L949-L963](https://github.com/earendil-works/pi/blob/b2602be77cb7b0de45dd616407fd210daa48aa75/packages/coding-agent/src/core/compaction/compaction.ts#L949-L963) 和 [`utils.ts` L101-L158](https://github.com/earendil-works/pi/blob/b2602be77cb7b0de45dd616407fd210daa48aa75/packages/coding-agent/src/core/compaction/utils.ts#L101-L158)。

### 限制

- Markdown summary 仍由 LLM 生成；公开实现没有证明事实完整性，也没有消除多次摘要造成的累积漂移。
- “保留上一份 summary”依赖 prompt 约束，不等于强 schema merge；冲突事实和已失效状态仍可能留下。
- tool result 的 2000-character 截断是控制成本的合理启发式，但可能截掉长错误输出尾部的关键证据。
- 默认实现会把 summary 与近期原文同时放入 Model Context，二者可能有少量语义重叠；这种重叠是为近期细节保真而支付的预算，不等于把 covered prefix 原文再次完整发送。

### 可借鉴点

- `firstKeptEntryId`（或等价覆盖区间）必须是 checkpoint 的一等字段；投影时据此排除 covered prefix。
- 保留完整近期 turn，而不是按字符任意截断；tool call/result 是不可拆交易单元。
- 重复压缩应是“上一份语义状态 + 本次新覆盖 span”的增量更新；上一份 summary 不应再被包装成普通 `summary:` 文本后与所有原文递归拼接。
- 将语言模型擅长的 semantic summary 与程序可确定提取的 state sidecar 分开。
- branch summary 与 compaction 应保持不同事件与数据语义，不要让导航摘要污染容量管理。

## 4. SWE-bench Verified Django 运行证据

### 核对范围

保存的 artifact 共包含 6 次 `django__django-11133` 正式 agent 运行。下表的状态来自各运行 `manifest.json`，entry／assistant call／compaction 计数来自 `session.jsonl`；`<artifact-root>` 指调用 `swebench run --artifacts` 时指定的评测 artifact 根目录。

| 运行 | 最终状态 | Session entries | Assistant calls | Compaction | 覆盖 entry 数 |
| --- | --- | ---: | ---: | ---: | --- |
| `issue-10-deepseek-final-a78b-20260901-auth1` | `agent_failed` / turn limit | 62 | 20 | 1 | 57 |
| `issue-10-deepseek-final-a78b-20260901-auth2` | `agent_failed` / turn limit | 65 | 20 | 0 | — |
| `issue-10-deepseek-final-a78b-20260901-auth3` | `agent_failed` / turn limit | 61 | 20 | 0 | — |
| `issue-10-deepseek-final-a78b-20260901-auth4` | `cancelled` | 248 | 78 | 3 | 64, 145, 232 |
| `issue-10-deepseek-final-a78b-20260901-auth5` | `success` / official Harness resolved | 79 | 26 | 0 | — |
| `parent-1-fbab60f9-django-11133-final` | `success` / official Harness resolved | 58 | 19 | 0 | — |

### 实际压缩内容与继续效果

当前 `DeterministicBranchSummarizer` 会把投影后的 message 转成 `role:text`，用换行拼接，然后从字符 0 开始截断到 180 字符。超预算时，`ContextPipeline` 将 Active Branch 中所有非 configuration entry 都放入 `covered_entry_ids`，用这个 summary 取代已覆盖前缀。若 Active Branch 已有 checkpoint，下一次的 branch projection 会先放入旧 summary，因此新 summarizer 会再次从旧 summary 的开头截断。这与运行中的递归 `summary:summary:user:` 完全一致。

`auth1` 在 sequence 56 已经得出“Tests pass”，sequence 58 读到实际 diff；sequence 59 却把前 57 个 entries 压成仅保留 issue 描述开头的 180 字符。sequence 60 立即重新说“We need understand task”并再次列目录，最后以 20-turn limit 失败。证据：`<artifact-root>/issue-10-deepseek-final-a78b-20260901-auth1/session.jsonl:56-62`。

`auth4` 分别在 sequence 66、147、234 压缩。三个 summary 依次以 `user:`、`summary:user:`、`summary:summary:user:` 开头，却始终只保留 issue 描述前缀，没有保留已找到的 `django/http/response.py`、`make_bytes()`、已存在的 workspace diff、通过的 smoke test 或当前下一步。每次 checkpoint 后的 assistant 都从“需要理解任务／检查仓库”重新开始。第三次压缩前，sequence 215 已确认 `django/http/response.py` 有修改，sequence 233 的复现已输出 `b'abc'` 和 `ok`；sequence 235 却又从新任务起点开始，运行最终被取消。证据：`<artifact-root>/issue-10-deepseek-final-a78b-20260901-auth4/session.jsonl:66-67`、`:147-148`、`:215-235`。

这两次有 compaction 的运行都没有进入 Harness 成功终态，两次 official Harness resolved 的运行都没有 compaction。但这不是受控 A/B 实验：6 次运行的 prompt、turn limit、workspace 与偶发 tool 错误并不完全相同，而 `auth2`、`auth3` 在无 compaction 时也失败。因此不能声称“compaction 是两次失败的唯一原因”，也不能从两次成功推导“长任务无需 compaction”。

能够直接从 checkpoint 前后对比得出的是：**当前 summary 没有保留 coding continuation 必需的工作状态，且实际触发了重复探索。** 这已超出“理论上可能不够好”，是需要优化的实证级缺陷。

### 存储重合的精确判断

- **Model Context 不重复：** latest checkpoint 会替换 covered prefix，后续 Provider request 不同时包含 summary 和该前缀的全部 raw messages。
- **Durable Session 有意重合：** raw entries 保留用于审计／恢复，checkpoint 是派生 materialized view。不应为消除这一点重合而删除历史。
- **当前 checkpoint 有可避免的物理膨胀：** `covered_entry_ids` 在重复压缩时累计重写整个 prefix。`auth4` 的 3 条 checkpoint 共占 19,640 / 449,822 bytes（约 4.37%）。这是次要优化点；优先级低于语义丢失。

### 现有测试的证据边界

现有测试覆盖了有界 request、非法 coverage、summary failure 无 Provider 副作用、raw history 保留、checkpoint replay 与 Extension transform。但没有测试二次／三次 compaction 后是否仍保留 goal、已修改文件、测试结果、blocker 和 next step。所以当前 green suite 证明了机械契约，没有证明语义延续质量。

## 5. 对本仓库的建议优先级

这些建议不改变 append-only Session tree，也不要求引入长期记忆或向量检索。

### P0：先修 checkpoint 的语义和覆盖契约

1. 将默认 summarizer 从“拼接 + 字符裁剪”升级为可替换的 semantic summarizer；摘要至少包含：当前目标、硬约束／用户偏好、已完成且已验证的动作、正在进行／阻塞项、关键决策及理由、精确标识符／错误、下一步。
2. checkpoint 同时保存 machine-readable sidecar，例如 `covered_entry_ids`（已有时继续复用）、`first_kept_entry_id`、read/modified files、关键验证命令及结果、未解决错误。不要要求 LLM 稳定生成这些关系字段。
3. 强化 Model Context 不变量：`one latest checkpoint + uncovered recent raw span + post-checkpoint entries`；任何 covered entry 都不能再次进入同一请求。
4. 只在安全边界切分：优先完整 user turn；tool call 与 tool result 不可拆。为单 turn 超预算设计显式 split-turn 策略，而不是在序列化文本中部截断。

### P1：约束重复压缩

1. 下一次压缩只处理“previous checkpoint 未覆盖的新 span”，并通过独立 `previous_summary` 参数做 state update；不要把旧 summary 当普通 message 再次序列化。
2. 新 checkpoint 替代旧 checkpoint 成为唯一 active summary；durable tree 可保留全部 checkpoint，但 Model Context 只看最新有效者。
3. 给 summary 加 version、parent checkpoint ID、coverage 和生成模型／策略元数据；在持久化前验证非空、覆盖合法、预算满足、没有未配对 tool transaction。
4. 增加重复压缩回归测试：禁止出现递归 `summary: summary:` 膨胀；断言已完成任务、测试证据、当前 blocker 和 next step 在 2–3 次压缩后仍可回答。

### P1：把权威上下文与 conversation summary 分层

- project instructions、当前权限、可用 tools、运行模式等 canonical context 每次重新投影，不由旧 summary 继承。
- 对旧 tool outputs 先做类型感知 shedding：保留状态、退出码、关键错误与 artifact/path，删除可再生的冗长 stdout/stderr；然后再对剩余旧对话做语义摘要。
- 保留一个近期原文预算，优先容纳最新完整 turns 和当前工作集；摘要提供全局连续性，近期原文提供局部精度。

### P2：可控性与评测

- manual compaction 允许附加 focus；将 focus 记录为 checkpoint provenance。
- 暴露 compaction start/succeeded/failed、before/after context size、covered count、retained count、summary size 和 repeated-compaction depth。
- 连续压缩后上下文没有显著下降时停止重试，报告 thrashing。
- 使用 continuation eval，而不只测字符数：压缩后询问目标、硬约束、已经修改的文件、已运行且通过的测试、当前失败原因和下一步；同时检查模型不会重新执行已完成工作。

## 6. 不建议直接复制的做法

- 不直接复制 Codex 的 encrypted opaque item 作为 Kernel 契约：它依赖 Provider 私有语义，难以审计、离线回放和做字段级质量评测。
- 不把 Claude Code 的“最近五个文件”等产品启发式固化为通用 Kernel 标准；应抽象成有预算的 current working set reload policy。
- 不原样复制 Pi 的自由 Markdown 作为唯一真相；保留它的 summary schema 思路，同时把覆盖、文件状态、验证事实和重复压缩 lineage 放入强类型 sidecar。
- 不因 durable Session 中有摘要副本就删除原始历史；应通过投影规则消除 Model Context 重复，继续保留可恢复、可审计的权威 tree。
