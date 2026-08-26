# Coding Agent Kernel Product Loop

**Status:** Approved and published

**Source:** Frozen canonical Spec `Independent Python Coding Agent Kernel`

**Tracker Parent:** [#1 — Independent Python Coding Agent Kernel](https://github.com/Ev3rGan/coding-agent-kernel/issues/1)

This plan was published as Parent #1 and implementation tickets #2–#10. The numbered items are vertical implementation tickets, not product versions, release stages, schedule commitments, or estimates. Ticket count follows capability boundaries and genuine blockers rather than a fixed range.

The planned development CLI is `python -m coding_agent`. This command does not exist in the repository yet; Ticket 01 owns establishing it, and all later acceptance commands must reuse that same public seam.

## Product outcome sentence

当 Kernel 开发者通过 Terminal CLI 从一个真实代码任务开始时，他们能够运行、观察和控制一个由 DeepSeek 驱动的 Agent Run：它在 Host 控制的权限边界内检查并修改 workspace、持久化可恢复 Session、通过受控 Extension 扩展，并最终产生可由官方 SWE-bench Harness 评估的结果。

## Decision ledger

### Confirmed

- 整个项目是一个独立实现的 Python Coding Agent Kernel，不提出新的 Agent 架构。
- Pi commit `5cd93f6` 是已选 Kernel 范围内的默认行为与技术机制基线；默认继承项不再成为 planning 问题。
- `AgentKernel`/`AgentRun` 是统一公开 interface；Terminal CLI 与 evaluator 是薄 Host。
- 公开 Event Stream 使用 `AgentSessionEvent`，同时保留 Provider Stream Event、AgentEvent 与 ExtensionEvent 的分层语义。
- 完整内置 Tool 集合是 `read/write/edit/bash/grep/find/ls`，调度只区分 `parallel/sequential`。
- Session 使用 append-only tree 语义并提供 JSONL 与 in-memory SessionStore。
- Model Context 只有一条确定性组装 pipeline；Compaction 与 branch summary 是 Session 表示机制。
- 支持 Pi 风格 Steering Message、Follow-up Message、取消、重试与 settled 语义。
- Extension 使用显式 Python 实例、固定 Hook 与确定性组合，不提供产品外壳插件系统。
- Permission Mode 是本项目自有能力；Host 控制 `plan/ask/auto/full`，模型和 Extension 不能提权或模拟批准。
- DeepSeek 是真实 ModelProvider；Fake Provider 用于可重复的能力演示和聚焦故障验证。
- 最终系统结果是在官方 Harness 中完整执行一个 SWE-bench Verified 实例；不预设分数或实例数量目标。
- ADR 0001 保留为公开架构 provenance，但 README、简历和面试主张不以“复现 Pi”为卖点。

### Constraints

- 规划不决定开发周期、日期、版本拆分、评测费用或工作量上限。
- 每张 ticket 必须交付可运行的 Kernel 能力，而不是测试结果、研究报告、Provider 比较或来源审计。
- 每张 ticket 必须给出用户可亲自运行的命令或场景、可观察结果、主要故障行为，以及 Pi 的借鉴/简化/必要差异说明。
- 自动验证仅覆盖关键确定性规则与主要故障路径；测试、Review、benchmark 和报告只是交付证据。
- Parent 与 tickets 可以使用稳定 interface 和状态语义，但不以文件清单、类清单或代码片段代替产品能力。
- 所有中间演示只针对 disposable workspace 或明确授权的仓库；权限模式不是生产级安全 Sandbox。

### Deferred

- 科研场景、长期记忆、向量检索和自动记忆沉淀。
- 多 Agent、写入型 subagent 和 MCP。
- IDE 集成、完整 TUI、主题、快捷键和 renderer。
- Cordis/DeepSeek Harness 插件 runtime。
- Extension 自动发现、entry point、热重载和产品外壳插件能力。
- 完整 Provider 生态、Provider 选择 benchmark 和 provider-specific prompt 优化。
- 生产级 Docker/micro-VM 隔离与任意 shell command 的完美副作用分析。
- SWE-bench 排名、最低分数、固定实例数和得分优化。
- 外部用户增长、商业化与完整产品运营。

### Open

没有会改变 Parent outcome、capability map、integrated path 或 non-goals 的开放决策。具体 Python 库、内部包布局和选取哪一个 Verified instance 属于 ticket 内实现选择，不得改变 canonical Spec。

## Capability map

| ID | Build | Effect | Why this capability is required | Owning ticket |
| --- | --- | --- | --- | --- |
| C1 | Headless `AgentKernel`/`AgentRun`、分层流式事件、Fake Provider 与薄 CLI | 用户可以运行并观察一个确定性 Agent Run，而不是只看到最终字符串 | 后续所有能力共享同一公开运行 seam | 01 |
| C2 | 完整模型—工具—模型循环、七个内置 Tool、Local CodingEnvironment 与确定性批调度 | Agent 可以在 disposable workspace 中读取、修改并验证代码 | Coding Agent 必须把模型输出转为真实代码操作 | 02 |
| C3 | append-only Session tree、JSONL/in-memory SessionStore、resume/fork/navigation | 运行可以关闭、恢复和分支，历史保持可审计 | 长任务和后续 Context 投影需要权威持久状态 | 03 |
| C4 | 单一 Model Context pipeline、项目资源、Active Branch projection、Compaction 与 branch summary | 长 Session 保持有界上下文而不删除权威历史 | 真实编码任务必须稳定管理上下文 | 04 |
| C5 | Steering Message、Follow-up Message、取消、重试、queue 与 settled 事件 | 用户可以在运行中改变方向、追加工作或安全终止 | Coding Agent 需要可控的长运行语义 | 05 |
| C6 | 显式 Extension registration、固定 Hook、ExtensionEvent 与确定性组合 | 项目可以扩展 Tool、Provider、SessionEntry 和 Kernel lifecycle，而不开放任意状态改写 | 展示 Kernel 边界和可扩展工程能力 | 06 |
| C7 | Host-controlled Permission Mode、Operation Intent、一次性 Permission Request 与 workspace containment | 用户可以明确控制副作用，且模型/Extension 无法自行提权 | 真实仓库操作需要项目自有的授权边界 | 07 |
| C8 | DeepSeek streaming/ToolCall Adapter 与完整本地编码路径 | 用户可以用真实模型完成代码修改、命令验证、恢复和 patch 输出 | 把确定性 Kernel 机制连接到真实 Coding Agent 任务 | 08 |
| C9 | SWE-bench evaluator/container Adapter、prediction/patch 输出与官方 Harness 入口 | 同一 Kernel 可以完整执行一个 Verified instance 并获得官方结果 | 提供最终的真实任务结果化证明 | 09 |

## Parent Spec draft

**Proposed title:** Independent Python Coding Agent Kernel

### Problem

现有作品能够证明业务侧大模型 API 应用，却不能充分证明项目作者理解并能独立实现 AgentLoop、流式 Provider、Tool Execution、Session、Model Context、运行控制和 Extension 等 Coding Agent Kernel 机制。仅有架构图、模块测试或 API 调用也不能说明这些机制在真实编码任务中能够闭环。

### Product outcome

项目作者可以通过一个薄 Terminal CLI 启动、观察和控制独立 Python Kernel 的 Agent Run。Agent 使用 DeepSeek 与受权限约束的 Tool 在真实 workspace 中完成代码任务，保留可恢复 Session 和结构化 Event Stream，并能够通过同一 Kernel 的 evaluator boundary 完整执行一个 SWE-bench Verified instance。

### Capabilities delivered

- 提供 `AgentKernel`/`AgentRun` 公开 seam，使 Host 可以异步消费完整 `AgentSessionEvent` 并控制运行。
- 提供完整内置 Tool 与 Local CodingEnvironment，使模型可以执行确定性的模型—工具—模型循环并把错误作为 ToolResult 继续反馈。
- 提供 append-only Session tree 与 JSONL/in-memory SessionStore，使运行能够恢复、分支和导航。
- 提供单一确定性 Model Context pipeline 与 Compaction/branch summary，使长 Session 的权威历史和模型输入保持分离。
- 提供 steering、follow-up、取消、重试和 settled 行为，使进行中的 Agent Run 可被控制。
- 提供显式 Extension registration 与固定 Hook，使 Kernel 能力可扩展但内部状态不向任意插件开放。
- 提供 `plan/ask/auto/full` Permission Mode，使所有副作用由 Host 选择的策略和一次性批准控制。
- 提供 DeepSeek Adapter 与薄 Terminal CLI，使真实模型可以在本地代码任务上使用完整 Kernel。
- 提供 SWE-bench evaluator boundary，使官方 Harness 可以完整运行一个 Verified instance 并保留结果证据。

### Decisions and constraints

- Pi commit `5cd93f6` 是所选 Kernel 范围内的默认行为和机制基线；本项目独立实现 Python 代码，不复制或逐行翻译上游 runtime。
- 项目价值是实现所有权、工程质量和可解释的运行机制，不是 Agent 架构创新。
- Tool 调度仅使用 `parallel/sequential`；权限分类与调度分类正交。
- `AgentRun` 是唯一运行控制对象；CLI、evaluator 和未来 Host 不复制 Kernel 状态机。
- Extension 只覆盖 Kernel capability，不扩张为 TUI/Cordis 产品插件宿主。
- `full` 只绕过 Kernel approval 与 containment，不提升操作系统权限，也不形成生产级安全承诺。
- 每项能力通过一条用户可运行路径和少量关键故障验证交付；不存在测试专用、评测专用或研究专用 ticket。
- 不按版本、周期、日期、费用、SWE-bench 分数或固定实例数量裁剪能力。

### Non-goals

- 新 Agent 架构、科研增强、长期记忆、向量检索、多 Agent、MCP、IDE 或完整 TUI。
- Cordis/DeepSeek Harness runtime、Extension 自动发现/热重载和完整 Provider 生态。
- Provider 比较、prompt 优化项目、SWE-bench 排名与得分优化。
- 生产级 Sandbox、恶意代码检测、永久信任规则或操作系统提权。
- 外部用户采用量、商业化、版本路线图和开发周期承诺。

### System acceptance

在冻结的候选代码与真实目标环境上，用户通过 `python -m coding_agent swebench run --instance <verified-instance>` 启动一个官方 SWE-bench Verified instance。Kernel 使用真实 DeepSeek Adapter、AgentRun Event Stream、完整 ToolRuntime、受选择模式约束的 CodingEnvironment、JSONL Session 和确定性 Model Context 完成任务，生成 prediction/patch，并由官方 Harness 返回结果；运行保留可检查的 Session、事件、工具结果和 Harness artifacts。

该终端场景证明核心编码路径的装配结果，但不人为强迫一个 SWE-bench instance 同时触发所有正交分支。steering/follow-up、取消、fork、Compaction、Extension 阻断和交互式 Permission Request 由所属 ticket 的可运行场景证明，并在最终候选代码上保持有效。

### Delivery graph

```text
01 Headless Agent Run
├── 02 Model-tool-model loop ──┐
└── 03 Durable Session tree ───┴── 04 Deterministic Context
                                      ↓
                                  05 Run control
                                      ↓
                                  06 Extensions
                                      ↓
                                  07 Permissions
                                      ↓
                                  08 DeepSeek coding path
                                      ↓
                                  09 SWE-bench Verified path
```

## Ticket 01 — 建立可观察的 Headless Agent Run

### Parent

Parent: `Independent Python Coding Agent Kernel` (draft reference; replace with the published Parent issue)

### Purpose

建立所有 Host、工具、Session 和 evaluator 将共同使用的第一条完整运行 seam，使一个 scripted model response 可以通过 Headless Kernel 成为结构化事件和最终结果。

### What to build

- 提供 `AgentKernel` 创建 `AgentRun` 的公开 interface；`AgentRun` 可异步迭代、可等待最终结果，并具有明确的 active、settled、cancelled 和 failed 终态。
- 建立 Provider Stream Event、AgentEvent 与公开 AgentSessionEvent 的分层转换，至少完整表达 agent、turn、message、text/thinking update、done 和 error。
- 保证 `message_update` 同时携带累计 AssistantMessage 与当前 Provider Stream Event，`message_end` 是外部消费者看到的权威消息。
- 提供 scripted Fake Provider，能够确定性产生增量文本、thinking、完成和失败。
- 建立薄 Terminal CLI 与稳定开发入口 `python -m coding_agent`，CLI 只驱动 AgentRun 和渲染事件。

### Resulting effect

用户可以亲自运行一个完全确定性的 Headless Agent Run，看到从 Provider 增量到公开事件再到最终结果的完整路径；后续 tickets 无需创建第二套运行入口。

### Acceptance

- [ ] 运行 `python -m coding_agent demo streamed-run`，可以看到 agent/turn/message 的开始、增量、结束和 settled 顺序，并获得与累计 AssistantMessage 一致的最终结果。
- [ ] 运行 `python -m coding_agent demo streamed-run --case provider-error`，Provider 失败成为结构化失败事件和 AgentRun 终态，不以未处理异常破坏 CLI。
- [ ] 通过 AgentKernel/AgentRun seam 的少量聚焦集成测试固定上述事件顺序、权威 `message_end` 与单一终态。
- [ ] ticket 交付说明列出实际命令、输入/输出、关键对象与状态、失败路径，以及“继承 Pi 的 Run/Turn 与事件语义；简化产品外壳；以 AgentRun facade 作为 Python 必要差异”。

### Non-goals

- Tool Execution、持久 Session、真实 Provider、Permission Policy 和 Extension。

### Blocked by

- None.

## Ticket 02 — 完成模型—工具—模型执行循环

### Parent

Parent: `Independent Python Coding Agent Kernel` (draft reference; replace with the published Parent issue)

### Purpose

让 Agent 不再只生成文本，而是能够从 ToolCall 到真实 workspace 操作再回到下一 Turn，形成最小但完整的 Coding Agent 执行闭环。

### What to build

- 扩展 Provider/Agent event path 以增量组装 ToolCall，并让 AgentLoop 在一个 Agent Run 中执行多个 Turn。
- 提供 `read/write/edit/bash/grep/find/ls` 的结构化 schema、参数准备、取消与 ToolResult；默认启用前四个，后三个显式启用。
- 提供 Local CodingEnvironment，统一 workspace 路径、文件操作、命令进程、stdout/stderr 增量、timeout、取消与退出状态。
- 实现 `parallel/sequential` 调度：混合批次只要包含 sequential Tool 就整体串行；完成事件可以按完成顺序产生，模型可见 ToolResult 必须按原始 ToolCall 顺序返回。
- 将未知 Tool、无效参数、文件/进程错误、timeout 和取消规范化为模型可见 ToolResult，使模型可以继续处理失败。

### Resulting effect

用户可以在 disposable workspace 中运行一个 scripted coding task，让 Agent 读取代码、修改文件、执行命令、观察增量输出并在下一 Turn 总结结果。

### Acceptance

- [ ] 运行 `python -m coding_agent demo tool-loop`，Agent 在 disposable workspace 中完成一次读取、编辑、命令验证和最终总结，并输出可检查的代码 diff。
- [ ] 运行 `python -m coding_agent demo tool-loop --case mixed-batch`，可观察并行只读批次、包含 sequential Tool 的全批串行行为，以及按原始 ToolCall 顺序返回的 ToolResult。
- [ ] 运行 `python -m coding_agent demo tool-loop --case failure`，无效参数、未知 Tool 或失败命令产生错误 ToolResult，Fake Provider 随后能够选择修复或结束。
- [ ] 聚焦集成测试只固定批调度、结果顺序、错误归一化和取消/timeout 等关键确定性规则。
- [ ] ticket 交付说明列出命令、Event Stream、ToolCall/ToolResult、workspace 变化与故障路径，并说明“借鉴 Pi 的工具批语义；保留完整七工具集合；用 CodingEnvironment 深化 Python Kernel 的执行边界”。

### Non-goals

- 对不受信任 workspace 提供安全承诺、Permission Mode、持久 Session、真实 DeepSeek 和 Provider 比较。

### Blocked by

- Ticket 01.

## Ticket 03 — 持久化、恢复和分支 Session

### Parent

Parent: `Independent Python Coding Agent Kernel` (draft reference; replace with the published Parent issue)

### Purpose

把瞬时 Agent Run 转换为可恢复、可分支和可检查的权威历史，使长任务和不同解决路线不依赖进程内 transcript。

### What to build

- 实现不可变 SessionEntry、entry ID、parent、active leaf 与 Active Branch 组成的 append-only Session tree。
- 提供 JSONL SessionStore 与 in-memory SessionStore，并版本化本项目自己的 Python persistence schema。
- 只在权威 `message_end` 后持久化消息；Provider 增量、工具进度和尚未注入的 queue 状态不伪装成消息历史。
- 通过同一 Terminal CLI 支持创建、关闭、resume、fork 和 tree navigation；sibling branch 不自动进入当前模型历史。
- 产生 Host 可观察的 SessionEntry、active branch、resume 和 configuration 事件，并对损坏 entry、非法 parent 或不完整恢复给出明确错误。

### Resulting effect

用户可以终止进程后恢复同一对话、从旧节点创建另一条路线并检查 JSONL，而不会覆盖或串入 sibling branch 的历史。

### Acceptance

- [ ] 运行 `python -m coding_agent demo session-tree`，场景创建 Session、关闭并恢复、从旧节点 fork，再展示两条 branch 与当前 Active Branch。
- [ ] 演示中可以检查 JSONL entry，且只看到权威消息、分支选择和配置记录，不包含 streaming 临时状态。
- [ ] 运行 `python -m coding_agent demo session-tree --case invalid-entry`，损坏或非法关系被显式拒绝，不静默改写历史或恢复到错误 branch。
- [ ] 使用同一行为场景分别驱动 JSONL 与 in-memory SessionStore，聚焦验证 append、resume 和 Active Branch projection 的必要契约一致性。
- [ ] ticket 交付说明解释 Session、Agent Run、Active Branch 与 Model Context 的区别，并说明“借鉴 Pi 的 tree 语义；文件格式独立；通过双 SessionStore seam 深化可恢复性和可测性”。

### Non-goals

- Compaction、branch summary、长期记忆、向量检索和自动记忆沉淀。

### Blocked by

- Ticket 01.

## Ticket 04 — 构造确定性 Model Context 并压缩长分支

### Parent

Parent: `Independent Python Coding Agent Kernel` (draft reference; replace with the published Parent issue)

### Purpose

建立唯一的模型输入投影规则，使 Session tree、当前运行态和 Provider 消息不会混为一体，并让长 Active Branch 可以在保留权威历史的同时控制上下文大小。

### What to build

- 在每次模型调用前执行一条 canonical Context pipeline，依次组装 system prompt、active Tool 描述/guideline、项目级指令与资源、Active Branch messages、Compaction/branch summary、当前注入消息和 Provider 转换。
- 明确完整 Session tree、Active Branch、Agent Run pending state 和最终 Model Context 是不同对象；sibling branch 与未注入 queue 不进入当前 Context。
- 实现 Compaction 与 branch summary 的持久表示和对应 AgentSessionEvent，保留被摘要历史的 SessionEntry，而不是删除或创建另一套 ContextBuilder。
- 在 Context 构造和摘要失败时遵循固定、可观察的失败/降级语义，不进行 provider-specific prompt 选型项目。
- 为未来 Extension context Hook 保留一个固定输入/输出契约，但本 ticket 不实现 Extension registration。

### Resulting effect

用户可以运行一条超出普通上下文窗口的确定性 Session，观察 Compaction 后模型只接收当前 Active Branch 的有界表示，同时仍能导航完整原始历史。

### Acceptance

- [ ] 运行 `python -m coding_agent demo context-compaction`，输出 compaction 前后 Model Context 摘要、对应 SessionEntry/Event 和完整历史仍可导航的证据。
- [ ] 场景创建 sibling branch 和 pending queue，并证明两者不会错误进入当前 Provider request。
- [ ] 运行 `python -m coding_agent demo context-compaction --case summary-error`，失败路径产生明确事件并保持 Session 可恢复，不删除原始 entry 或切换到第二套 Context pipeline。
- [ ] 聚焦测试固定组装顺序、Active Branch projection、Compaction 后 Context 与主要失败路径。
- [ ] ticket 交付说明解释 Session 与 Model Context 的分离，并说明“借鉴 Pi 的确定性投影与 compaction 语义；简化 provider-specific prompt 优化；深化可观察的 Python Context seam”。

### Non-goals

- 多套可插拔 ContextBuilder、长期记忆、向量检索、Provider prompt benchmark 和自动无条件摘要沉淀。

### Blocked by

- Ticket 02.
- Ticket 03.

## Ticket 05 — 控制进行中的 Agent Run

### Parent

Parent: `Independent Python Coding Agent Kernel` (draft reference; replace with the published Parent issue)

### Purpose

让用户能够控制长时间运行的 Agent，而不通过修改 Session 或绕开 AgentRun 状态机注入消息和终止进程。

### What to build

- 实现独立 Steering Message 与 Follow-up Message queue，并固定各自 drain point：steering 在当前 Tool batch 后、下一模型请求前；follow-up 在自然 settled 且无待处理 steering 后。
- pending message 保持为 Agent Run 运行态，仅在实际注入时进入 Session；queue 变化和注入通过 AgentSessionEvent 对 Host 可见。
- 让 Terminal CLI 在 Agent Run 进行期间提交 steering、follow-up 和 cancel，并保持 AgentRun 是唯一控制入口。
- 将取消传播到 Provider stream、Tool Execution 和等待状态，保证最终只有一个 cancelled/failed/settled 结果。
- 实现 Pi 基线范围内的 Provider retry 与 settled 语义，使 retry 事件、可恢复错误和最终失败对 Host 可见。

### Resulting effect

用户可以在工具运行期间改变下一步方向、排队自然结束后的追加任务，或取消整个 Agent Run，并从事件和 Session 中看清消息何时真正生效。

### Acceptance

- [ ] 运行 `python -m coding_agent demo run-control --case steering`，在 Tool batch 进行中提交消息，并观察它只在 batch 完成后进入下一次 Model Context 和 Session。
- [ ] 运行 `python -m coding_agent demo run-control --case follow-up`，消息只在 Agent 自然 settled 且 steering 已清空后启动后续工作。
- [ ] 运行 `python -m coding_agent demo run-control --case cancel`，取消传播至当前 Provider/Tool，pending queue 不被错误持久化，AgentRun 只产生一个终态。
- [ ] 运行 scripted retry 场景，观察 retry event、成功恢复或最终失败；聚焦测试只固定 drain point、取消传播、retry 与 settled 不变量。
- [ ] ticket 交付说明解释两个 queue、注入时点和取消边界，并说明“直接借鉴 Pi 的 steering/follow-up 与 settled 语义；不增加 Step 或多 Agent；通过统一 AgentRun control interface 形成 Python 必要差异”。

### Non-goals

- 多 Agent 协调、写入型 subagent、任务调度平台和长期后台运行服务。

### Blocked by

- Ticket 04.

## Ticket 06 — 通过固定 Extension 合约扩展 Kernel

### Parent

Parent: `Independent Python Coding Agent Kernel` (draft reference; replace with the published Parent issue)

### Purpose

证明 Kernel 能够增加能力和拦截生命周期，同时保持状态所有权、事件边界和确定性组合规则由 Kernel 控制。

### What to build

- 让 AgentKernel 接受显式构造的 Python Extension 实例，并允许注册 Tool、Provider、SessionEntry type 和固定 Hook handler。
- 实现 canonical Spec 中固定的 input、before-agent-start、context、Provider、agent/turn/message、ToolCall/ToolResult、Tool Execution、Session/Compaction/tree 与 agent-settled Hook 集合。
- 按注册顺序确定性组合 handler，明确 observe、transform、block 和 supplement 的允许结果，并在每次转换后重新验证受影响的契约。
- 保持 ExtensionEvent 为独立 dispatch/control contract，不把它自动混入公开 AgentSessionEvent stream。
- 将 Extension 异常、非法改写和阻断转换为明确 Kernel 行为；Extension 不取得任意可写 Kernel 状态。
- 提供一个显式加载的示例 Extension，至少展示自定义 Tool、Context 补充、ToolCall 阻断和自定义 SessionEntry 中的真实能力。

### Resulting effect

用户可以用普通 Python 对象扩展 Kernel 并观察确定性结果，同时能够解释 Extension 为何不能绕过 AgentRun、Session 或 ToolRuntime 的所有权边界。

### Acceptance

- [ ] 运行 `python -m coding_agent demo extensions`，显式传入示例 Extension，完成自定义 Tool 调用、Context 补充、阻断一次 ToolCall 并持久化自定义 SessionEntry。
- [ ] 运行 `python -m coding_agent demo extensions --case ordering`，两个 Extension 的 transform/supplement 结果按注册顺序稳定组合。
- [ ] 运行 `python -m coding_agent demo extensions --case invalid-mutation`，非法状态改写或 handler 异常被拒绝并产生明确事件/ToolResult，不损坏 Agent Run 或 Session。
- [ ] 聚焦测试固定 Hook 顺序、block/transform 组合、重新验证和 ExtensionEvent/AgentSessionEvent 分离。
- [ ] ticket 交付说明逐项标注“借鉴 Pi/Tau 的 registration 与 hook 思路；简化自动发现、TUI 与热重载；深化固定状态所有权和确定性组合规则”。

### Non-goals

- 目录扫描、Python entry point、动态 hot reload、TUI renderer、快捷键、主题、overlay、完整 CLI 插件宿主和 Cordis lifecycle。

### Blocked by

- Ticket 05.

## Ticket 07 — 以 Host 权限模式约束 Tool Execution

### Parent

Parent: `Independent Python Coding Agent Kernel` (draft reference; replace with the published Parent issue)

### Purpose

让真实 workspace 操作经过明确且不可由模型绕过的授权边界，并把批准、拒绝和执行之间的关系暴露给 Host 与 Session。

### What to build

- 为每个 Agent Run 提供 Host-selected `plan/ask/auto/full` Permission Mode；默认 `auto`，运行中模型/Tool/Extension 不可更改，恢复 Session 时不自动恢复 `full`。
- 从 Hook 修改后的最终 ToolCall 参数生成 Operation Intent，并通过 Permission Policy 返回 allow/deny/ask；该分类与 `parallel/sequential` 调度正交。
- 固定 pipeline：参数校验 → ToolCall Hook → 最终参数重新校验/规范化 → Operation Intent → Policy → Host decision → Tool Execution → ToolResult Hook。
- 实现与 ToolCall ID、最终参数和 Operation Intent 绑定的一次性 Permission Request，以及 `permission_requested`/`permission_resolved` AgentSessionEvent 和 `AgentRun.resolve_permission`。
- 让内置文件 Tool 精确分类规范化目标；对 bash 检查 command/cwd/可识别目标，无法可靠判断时标记 unknown；`plan` 仅在 CodingEnvironment 可真实保证只读时运行诊断命令。
- 在非 `full` 模式执行 workspace containment；`full` 跳过 Kernel approval/containment，但保留 OS 权限、取消、timeout 和进程生命周期。
- 将 permission decision 持久化为不含秘密的 SessionEntry；取消、Host 断开、参数失效或 Session resume 都不能执行旧的 pending request。
- 让 CLI 选择并持续显示 mode、展示最终 Tool/参数/目标/原因、接受单次 approve/deny，并对 `full` 显示明确风险。

### Resulting effect

用户可以亲自选择自动化程度、检查并批准确切 ToolCall，并从 Event Stream 与 Session 证明模型或 Extension 没有自行提权；同一 Tool 的授权与调度仍保持两个独立维度。

### Acceptance

- [ ] 运行 `python -m coding_agent demo permissions --mode plan`，workspace read 被允许，write/network/outside/unknown 被拒绝；当前 Local CodingEnvironment 若不能保证 shell 只读，则诊断 bash 也被拒绝。
- [ ] 运行 `python -m coding_agent demo permissions --mode ask`，CLI 展示最终参数与 Operation Intent；approve 后只执行该 ToolCall，deny 后生成无副作用的错误 ToolResult。
- [ ] 运行 `python -m coding_agent demo permissions --mode auto` 与 `--mode full`，观察 workspace 常规操作、越界请求和 `full` 风险提示的不同，同时证明 `full` 不提升 OS 权限。
- [ ] 运行 `python -m coding_agent demo permissions --case extension-rewrite`，Extension 改写目标后重新分类；旧 Approval 失效，Extension 不能把 denied operation 改写为成功。
- [ ] 运行取消、Host 断开和 Session resume 场景，pending Permission Request 不执行，`full` 不自动恢复，Session 记录不包含环境秘密。
- [ ] 聚焦测试以 canonical permission matrix 固定 allow/deny/ask、无副作用拒绝、approval binding、调度正交性和主要恢复路径；不为每种 mode 建独立 ticket。
- [ ] ticket 交付说明明确“这是项目自有必要差异，不声称 Pi 默认能力；深化点是 Host-owned approval 与 Hook 后授权；安全主张止于 Kernel policy/containment”。

### Non-goals

- 生产级 Sandbox、完美 shell 静态分析、恶意代码检测、永久 trust rules、模型/Extension 自批准和操作系统提权。

### Blocked by

- Ticket 06.

## Ticket 08 — 用 DeepSeek 完成真实本地编码任务

### Parent

Parent: `Independent Python Coding Agent Kernel` (draft reference; replace with the published Parent issue)

### Purpose

把确定性 Fake Provider 驱动的 Kernel 连接到真实模型，使完整机制在实际代码仓库中产生代码修改、命令结果、可恢复 Session 和 patch。

### What to build

- 提供 DeepSeek ModelProvider Adapter，规范化标准 streaming、thinking、ToolCall、usage、done 和 error，不向 AgentLoop 泄露 Provider-specific 分支。
- 将用户配置与凭据保持在仓库外，通过清晰的启动错误和配置说明选择可用 DeepSeek 模型；不开展 Provider 比较。
- 让 retry、取消、ToolCall 多 Turn、Permission Request 和 Session persistence 在真实 Provider path 上使用既有 Kernel 语义。
- 完成薄 Terminal CLI 的真实任务入口、Event Stream 渲染、Session create/resume、mode 选择、最终结果和 workspace diff/patch 展示。
- 提供一个 bounded local coding scenario，使模型检查代码、编辑缺陷、运行验证命令并解释结果；场景运行在 disposable 或明确授权的 workspace。

### Resulting effect

用户可以用自己的 DeepSeek 凭据运行一个真实 Coding Agent 任务，并从 CLI、Session、代码 diff 和故障信息看到所有 Kernel 机制确实装配在同一路径上。

### Acceptance

- [ ] 在 disposable coding fixture 上运行 `python -m coding_agent run --provider deepseek --workspace <workspace> --mode ask "<coding-task>"`，模型完成检查、批准后的编辑、命令验证和最终说明。
- [ ] 同一运行保留 JSONL Session、结构化 AgentSessionEvent、ToolResult、permission decisions 和最终 patch；关闭后可用 CLI resume 并继续任务。
- [ ] 缺失凭据、Provider streaming 中断或 ToolCall 格式错误产生可操作错误和既定 retry/failure 事件，不泄露凭据或损坏 Session。
- [ ] 只增加覆盖真实 Adapter contract 与完整 CLI path 所需的聚焦验证，不建立模型质量比较或 prompt 选型票。
- [ ] ticket 交付说明标注“借鉴 Pi 的 Provider normalization；简化为 DeepSeek + Fake 两个 Adapter；深化点是把同一 Python Kernel seam 用于真实 CLI、权限和恢复”。

### Non-goals

- Provider 生态、模型质量排行、固定模型版本 benchmark、provider-specific prompt 优化和外部用户产品化。

### Blocked by

- Ticket 07.

## Ticket 09 — 在官方 Harness 中完成一个 SWE-bench Verified 实例

### Parent

Parent: `Independent Python Coding Agent Kernel` (draft reference; replace with the published Parent issue)

### Purpose

建立 evaluator/container 产品边界，使同一 Kernel 可以接收标准真实 issue、操作其目标仓库、输出 prediction/patch，并由官方 Harness 给出结果，而不是把 benchmark 当作脱离产品的报告。

### What to build

- 提供 SWE-bench evaluator/container CodingEnvironment Adapter，负责实例 workspace 准备、命令执行、取消、timeout、输出和结果收集，同时复用现有 AgentKernel/AgentRun。
- 将一个 Verified instance 的 problem statement 转为 Kernel 输入，并把最终 workspace changes 序列化为官方 Harness 所需 prediction/patch 形式。
- 提供可重复的 evaluator CLI 入口，允许调用者提供任意 Verified instance ID、DeepSeek 配置与运行产物目录，不在设计中固定实例或分数。
- 保留运行所需的 Kernel 配置、Session、事件、ToolResult、patch/prediction 和官方 Harness result，使用户可以复核同一候选代码的完整路径。
- 对环境准备失败、模型失败、timeout、无 patch 和 Harness rejection 提供明确状态，不把运行失败包装为分数结果。

### Resulting effect

用户可以亲自选择一个 SWE-bench Verified instance，完整运行独立 Python Kernel 并取得官方 Harness 结果，以真实任务证明系统已经具备必要的 Coding Agent 闭环。

### Acceptance

- [ ] 运行 `python -m coding_agent swebench run --instance <verified-instance>`，从实例准备、Agent Run、代码修改、prediction 生成到官方 Harness 评估完整结束并产生结果。
- [ ] 运行 artifacts 同时保留 Session、关键 Event Stream、ToolResult、最终 patch/prediction、Harness result 和精确 Kernel 配置，且不包含 Provider 凭据。
- [ ] 若环境准备、Agent、timeout、无 patch 或 Harness 失败，命令返回可区分的失败状态和可操作诊断，不伪造成功分数。
- [ ] 验收只要求完整执行一个实例并取得官方结果，不规定实例数量、最低分数、排行榜位置或后续得分优化。
- [ ] ticket 交付说明解释 Kernel 与 evaluator Adapter 的边界，并说明“Pi Kernel 机制继续复用；SWE-bench CodingEnvironment 与输出契约是项目必要差异；结果证明能力但不反向扩大 Kernel 范围”。

### Non-goals

- SWE-bench 排行榜优化、批量固定实例集、最低得分、模型比较、评测费用决策和为 benchmark 特化 Kernel prompt/架构。

### Blocked by

- Ticket 08.

## Dependency graph

| Ticket | Genuine blockers | Reason |
| --- | --- | --- |
| 01 | None | 建立所有后续能力共享的 AgentRun/Event/CLI seam |
| 02 | 01 | Tool loop 消费 AgentRun 与 Provider event path |
| 03 | 01 | Session 持久化消费权威 message lifecycle，但不依赖 ToolRuntime |
| 04 | 02, 03 | Context 同时需要完整 Tool 描述/消息循环与 Active Branch projection |
| 05 | 04 | queue 注入、retry 和取消必须进入已确定的 Context/Session 路径 |
| 06 | 05 | 固定 Hook 集合覆盖已经存在的完整 runtime/session/control lifecycle |
| 07 | 06 | 权限必须发生在 ToolCall Hook 最终改写之后，并持久化到既有 Session/control path |
| 08 | 07 | 真实 Provider path 必须使用已经闭合的 tools、session、extensions 和 permissions |
| 09 | 08 | evaluator 必须消费已证明的真实 DeepSeek coding path，而非另一套 Agent |

Publishing order is blockers-first: Parent, 01, 02, 03, 04, 05, 06, 07, 08, 09. Tickets 02 and 03 are independent after 01 and may be implemented in parallel. Every ticket is an implementation ticket and may receive `ready-for-agent`; the Parent must not receive that implementation label or be closed/rewritten as a side effect.

## Build / Effect / Proof obligation ledger

| Parent obligation | Full build owner | Resulting effect | Proportionate proof | Terminal consumption |
| --- | --- | --- | --- | --- |
| Public AgentRun and layered events | 01 | Host can observe one deterministic run | `demo streamed-run` + event-order fault case | DeepSeek and evaluator consume AgentRun |
| Complete ToolRuntime and Local CodingEnvironment | 02 | Agent changes and verifies code | `demo tool-loop` + scheduling/error cases | Local task and evaluator use same ToolRuntime contract |
| Durable Session tree | 03 | Runs resume, fork and remain auditable | `demo session-tree` + invalid-entry case | DeepSeek/evaluator retain JSONL Session |
| Deterministic Context and Compaction | 04 | Long Active Branch yields bounded model input | `demo context-compaction` + summary failure | All later Provider calls consume canonical Context pipeline |
| Steering/follow-up/cancel/retry | 05 | Long Agent Run is controllable | `demo run-control` scenarios | Candidate retains these controls; not forced into one benchmark instance |
| Fixed Extension contract | 06 | Kernel gains controlled capabilities | `demo extensions` scenarios | Candidate retains hooks; benchmark need not trigger every hook |
| Host-controlled permissions | 07 | Side effects require correct run-scoped authority | permission matrix + approval/rewrite/resume scenarios | Local/benchmark path runs under an explicit mode |
| Real DeepSeek local coding | 08 | Real model completes a code task and emits patch | one bounded disposable-workspace task + failure path | SWE-bench uses same Provider/Kernel path |
| SWE-bench evaluator boundary | 09 | Official Harness completes one Verified instance | full instance command and official result artifacts | This is terminal system acceptance |

Every Parent capability has one full build owner. Later tickets consume earlier capabilities but do not silently redefine their scope. There are no validation-only, research-only, Provider-comparison, source-audit, report-only, refactor-only, or release-management tickets.

## Convergence audit

- **Closed product loop:** Terminal CLI and evaluator both drive the same `AgentKernel`/`AgentRun`; no alternate benchmark Agent exists.
- **Build before proof:** each ticket states the runtime capability and user effect before acceptance evidence.
- **Vertical slices:** every ticket leaves a runnable behavior at the public CLI or AgentRun seam; layers are introduced only inside the earliest capability that needs them.
- **Complete ownership:** C1-C9 each have one full build owner and an observable failure path.
- **Genuine dependencies:** only Context waits for both ToolRuntime and Session; the remaining chain follows actual lifecycle consumption. Tickets 02 and 03 form the only independent frontier after 01.
- **Focused validation:** tests live inside capability tickets and cover deterministic rules/faults; no ticket exists merely to compare Providers, verify sources, write reports, or run a module test inventory.
- **Terminal proof:** Ticket 09 owns official SWE-bench execution. It does not set score/count targets or force orthogonal controls/hooks into one instance.
- **Scope preservation:** no research scenario, long-term memory, multi-Agent, MCP, IDE/TUI, Cordis, plugin discovery, Provider ecosystem or production Sandbox re-enters through a ticket.
- **No planning overreach:** there are no dates, time estimates, version labels, cost decisions or benchmark instance selections.
- **Tracker readiness:** publication can create one Parent and nine independent issues, add native sub-issue/dependency relations, apply `ready-for-agent` only to implementation tickets, and verify the graph without modifying or closing the Parent.

## Publication record

- Parent #1 and tickets #2–#10 were published to `Ev3rGan/coding-agent-kernel` after exact user approval.
- Tickets #2–#10 are native sub-issues of Parent #1 and carry only `ready-for-agent`.
- Native blocking relationships match the approved graph: #3/#4 depend on #2, #5 depends on #3/#4, and #6 through #10 form the approved convergence chain.
- Parent #1 remains open, has no implementation label, and was not rewritten or closed during publication.
