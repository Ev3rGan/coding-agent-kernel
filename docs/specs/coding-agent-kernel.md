# Independent Python Coding Agent Kernel

**Status:** Canonical

**Frozen:** 2026-08-24

## Problem Statement

项目作者正在申请 Coding Agent、企业内部代码 Agent、IDE 集成和自动代码生成相关岗位。现有 `ai-ledger` 已经可以证明业务侧模型应用与架构落地能力，但不能充分回答以下面试质疑：

- 是否只会调用模型 API，而不了解 AgentLoop、Tool Execution、Session 与 Context 等内核机制。
- 是否能够独立设计和实现具有异步流式调用、工具调度、持久会话、恢复路径与 Extension 的复杂系统。
- 是否能够把对成熟 Coding Agent 的理解迁移为自己拥有的 Python 实现。
- 是否能够通过真实编码任务而不是模块测试或架构图证明系统完整工作。

该项目不试图提出新的 Agent 架构，也不以超越 Pi/Tau 为目标。它需要证明的是：项目作者能够理解成熟 Coding Agent 的关键机制，以 Python 独立实现一个完整 Kernel，并通过可运行行为、Session 证据和真实 SWE-bench 实例说明实现结果。

## Solution

构建一个独立实现的、Headless 的 Python Coding Agent Kernel。

项目在选定的 Kernel 范围内，以 Pi commit `5cd93f6` 的行为和技术机制作为默认基线，不重新发明 AgentLoop、Session、Context、Tool 调度和 Extension 语义。仅在以下情况引入差异：

- TypeScript 机制必须转换为 Python 表达。
- 已确定的公开 Kernel interface 与 Pi 产品组织方式不同。
- 完整 Pi 产品功能超出本项目范围。
- SWE-bench 执行需要独立的 CodingEnvironment seam。
- 权限模式需要在 Tool Execution 与 CodingEnvironment 之间增加项目自有的 Permission Policy。

Kernel 通过 `AgentKernel` 创建 `AgentRun`。`AgentRun` 是可异步迭代的运行句柄，对外产生 `AgentSessionEvent`，同时提供 steering、follow-up、取消、权限响应和最终结果控制。Terminal CLI、SWE-bench evaluator 和未来其他 Host 均通过这一 interface 使用 Kernel。

Host 为每次 Agent Run 选择 Permission Mode。Kernel 根据 ToolCall 的最终参数、目标位置和 Operation Intent 决定允许、拒绝或请求确认。权限模式只控制授权，不改变工具的 `parallel/sequential` 调度属性；模型与 Extension 均不能自行提升权限。

完成后的系统能够：

1. 接收真实代码任务。
2. 流式调用 DeepSeek。
3. 组装文本、thinking 和 ToolCall。
4. 调度并执行文件与命令工具。
5. 将 ToolResult 按确定性顺序返回模型。
6. 持久化和恢复 Session。
7. 处理 steering、follow-up、compaction、重试和取消。
8. 允许 Extension 通过受控 registration 与 Hook 参与 Kernel。
9. 通过 Host 控制的权限模式约束或批准 Tool Execution。
10. 修改目标代码仓库并生成可评估的 patch。
11. 完整运行至少一个 SWE-bench Verified 实例并取得官方 Harness 结果。

## User Stories

1. 作为求职者，我希望独立实现 Coding Agent Kernel，使我能够向面试官解释 Agent 从模型调用到工具执行和 Session 持久化的完整路径。

2. 作为 Kernel 使用者，我希望通过 Terminal CLI 提交真实代码任务，使 Agent 能够检查仓库、修改文件、运行命令并返回结果。

3. 作为 Host 开发者，我希望异步消费结构化 `AgentSessionEvent`，使 CLI、evaluator 和未来集成不需要理解 AgentLoop 内部状态。

4. 作为 Host 开发者，我希望在 Agent Run 进行期间发送 Steering Message、Follow-up Message 或取消请求，使长时间运行的任务可以被控制。

5. 作为使用者，我希望实时看到文本、thinking、ToolCall 构造和 Tool Execution 进度，而不是只能等待最终字符串。

6. 作为 Kernel 开发者，我希望 Provider streaming 被规范化为稳定事件协议，使 DeepSeek 与确定性 Fake Adapter 能够驱动同一 AgentLoop。

7. 作为使用者，我希望只读工具能够并行执行，同时串行工具能够保持安全顺序，并保证最终 ToolResult 按原始 ToolCall 顺序返回模型。

8. 作为使用者，我希望工具失败成为结构化错误 ToolResult，使模型可以观察失败并决定修复、重试或更换方法。

9. 作为使用者，我希望 Session 可以恢复、分支和导航，使中断后的任务和不同解决路线不需要破坏已有历史。

10. 作为使用者，我希望长 Session 可以进行 Compaction，使旧历史被摘要表示而不是无限占用 Model Context。

11. 作为 Extension 作者，我希望显式注册 Tool、Provider、SessionEntry 类型和固定 Hook，使 Kernel 可以扩展而不允许任意修改内部状态。

12. 作为 Kernel 开发者，我希望使用 Fake Provider 与内存 SessionStore 重现关键故障路径，使核心确定性规则可以低成本验证。

13. 作为面试官，我希望看到某项能力的运行命令、可观察事件、Session 结果和故障行为，使我能够判断候选人是否真正实现了 Kernel。

14. 作为项目作者，我希望完整运行一个 SWE-bench Verified 实例，使项目具备真实编码任务的端到端结果，而不依赖自定义玩具基准。

15. 作为使用者，我希望为每次 Agent Run 选择权限模式，使同一个 Kernel 可以安全地用于分析、交互式开发和受信任项目。

16. 作为使用者，我希望在 `plan` 模式下禁止代码修改和外部副作用，使 Agent 可以安全地分析仓库并制定计划。

17. 作为使用者，我希望在 `ask` 模式下，在写入、网络访问或越出 workspace 前确认具体操作，使我能够逐项控制副作用。

18. 作为使用者，我希望在 `auto` 模式下自动执行 workspace 内可识别的常规操作，仅在越界或无法判断影响时请求确认，使 Coding Agent 保持实用性。

19. 作为明确授权可信项目的使用者，我希望选择 `full` 模式，使 Kernel 不再进行 workspace containment 和交互式批准。

20. 作为 Host 开发者，我希望权限请求和决定通过结构化事件与 `AgentRun` 控制 interface 传递，使 CLI 和未来 Host 可以实现一致的确认流程。

21. 作为项目作者，我希望权限决定与最终 ToolCall 参数关联并留下 Session 记录，使我可以向面试官解释某项操作为何被允许、拒绝或请求确认。

## Implementation Decisions

### 1. 项目定位

- 项目是独立实现的 Python Coding Agent Kernel，不是 Pi Extension，也不是 Pi TypeScript 源码的翻译。
- 项目价值来自独立实现、工程质量和对运行机制的掌握，不来自架构创新主张。
- “可解释”指项目作者能够通过对象、状态、事件、Session 和故障路径解释系统，不要求实现模型推理解释功能。
- 项目首先面向技术面试官和项目作者本人；不以外部用户采用量作为完成标准。
- Tau 可以作为 Python 表达方式的辅助参考，但不是第二个 parity 目标。
- DeepSeek Harness/Cordis 不属于项目架构或能力主张。

### 2. Pi 默认继承规则

在已选择的 Kernel 范围内，以下机制默认继承 Pi，不再作为待决架构问题：

- Agent Run 包含多个 Turn，不引入 Step。
- 一个 Turn 是一次模型响应及其 ToolCall 和 ToolResult。
- Provider streaming 保留 text、thinking、tool-call、done 和 error 的完整增量语义。
- AgentLoop 产生 agent、turn、message 和 tool-execution 生命周期事件。
- AgentSession 层增加 queue、retry、compaction、entry 和 settled 产品事件。
- ExtensionEvent 作为独立分发契约，不与公开 Event Stream 混为一体。
- steering 与 follow-up 使用不同 PendingMessageQueue 和 drain point。
- Session 是 append-only JSONL tree，具有 entry ID、parent、active leaf 和 Active Branch。
- Model Context 从 Active Branch、Compaction 和当前资源确定性投影。
- Context 只有一条 canonical assembly pipeline。
- Tool Execution 支持 `parallel/sequential`。
- 混合批次中只要存在 sequential 工具，整个批次串行执行。
- Tool Execution 完成事件可按完成顺序产生，最终 ToolResult 按原始 ToolCall 顺序写回。
- Tool、Context、Session 和 Extension 的失败、取消与重试语义默认遵循 Pi 基线。

默认继承仅适用于已经选择的 Kernel 能力，不会恢复完整 TUI、Provider 生态或其他明确非目标。

### 3. `AgentKernel` 与 `AgentRun`

`AgentKernel` 是主要公开 Module。它隐藏以下实现协调：

- AgentLoop
- Model Context 组装
- ToolRuntime
- Session
- ModelProvider
- CodingEnvironment
- Permission Policy
- Extension runtime
- retry、compaction 和 cancellation

`AgentKernel` 创建 `AgentRun`。`AgentRun`：

- 实现异步事件迭代。
- 产生 `AgentSessionEvent`。
- 接受 Steering Message。
- 接受 Follow-up Message。
- 支持取消。
- 接受一次性 Permission Request 响应。
- 提供最终运行结果。
- 持有且仅控制一次 Agent Run 的运行态。

Terminal CLI 是薄 Host，不复制 Kernel 内部调度逻辑。

### 4. Event 模型

系统区分四层事件：

1. **Provider Stream Event**

   表达原始模型增量，包括文本、thinking、ToolCall 构造、完成和错误。

2. **AgentEvent**

   表达低层 AgentLoop 的 agent、turn、message 和 Tool Execution 生命周期。

3. **AgentSessionEvent**

   在 AgentEvent 之外增加 queue、compaction、retry、SessionEntry、配置变化、权限请求与 settled 状态，是 `AgentRun` 的公开 Event Stream。

4. **ExtensionEvent**

   发送给 Extension runtime 的观察或受控拦截事件，不自动作为公开 Event Stream 输出。

`message_update` 同时保留累计 AssistantMessage 与当前 Provider Stream Event。`message_end` 在 Extension 修改完成后成为外部消费者与 Session 持久化看到的权威消息。

### 5. ModelProvider

ModelProvider 是一个真实 seam，至少具有两个 Adapter：

- DeepSeek Adapter：支持标准 streaming 和 ToolCall，面向可稳定访问的 DeepSeek 模型。
- Fake Adapter：按脚本确定性地产生文本、ToolCall、错误、取消和 usage 事件。

Provider 差异在 Adapter 内规范化。AgentLoop 不包含 DeepSeek 专用分支，也不进行 Provider 选择评测。

### 6. ToolRuntime

内置工具集合为：

- `read`
- `write`
- `edit`
- `bash`
- `grep`
- `find`
- `ls`

默认启用：

- `read`
- `write`
- `edit`
- `bash`

`grep/find/ls` 是已注册但需要显式启用的只读工具。

每个 Tool 包含：

- 名称、标签和模型可见描述。
- 结构化参数 schema。
- `parallel/sequential` execution mode。
- 参数准备与验证。
- 支持取消和增量输出的执行 interface。
- 结构化 ToolResult。

ToolRuntime 负责：

- 查找 Tool。
- 准备和验证参数。
- 调用执行前 Hook。
- 对 Hook 修改后的参数重新验证并生成 Operation Intent。
- 执行 Permission Policy 并在必要时等待 Host 响应。
- 执行串行或并行批次。
- 规范化成功、阻断、错误和取消结果。
- 调用执行后 Hook。
- 按 ToolCall 原始顺序生成模型可见 ToolResult。

工具权限、workspace 路径约束与进程控制不会编码为 READ/WRITE/EXECUTE 调度类别。

### 7. Session 与 SessionStore

Session 继承 Pi 的 append-only tree 语义：

- SessionEntry 是不可变持久记录。
- 每个 Entry 通过 ID 和 parent 形成树。
- active leaf 选择当前 Active Branch。
- sibling branch 不自动进入 Model Context。
- 支持 resume、fork 和 tree navigation。
- 消息在权威 `message_end` 后持久化。
- streaming、工具进度和临时 queue 状态不直接成为消息历史。

SessionStore 是显式 seam：

- JSONL Adapter 用于真实持久化与恢复。
- In-memory Adapter 用于确定性运行和聚焦验证。

Python 持久化 schema 由本项目版本化，不要求与 Pi 文件字节兼容，但必须保持已经决定的逻辑语义。

权限决定作为 SessionEntry 持久化，记录 Permission Mode、Operation Intent、decision 与关联 ToolCall，但不保存敏感凭据或环境秘密。未完成的 Permission Request 不会在恢复后自动执行。

### 8. Model Context

每次模型调用前重新构造 Model Context，保持以下状态彼此独立：

- 完整 Session tree。
- 当前 Active Branch。
- 当前 Agent Run 状态。
- 最终 Provider Model Context。

Context 组装包含：

- 基础或自定义 system prompt。
- 当前 active Tool 的描述与 guideline。
- 项目级指令和上下文资源。
- Active Branch message projection。
- Compaction 与 branch summary。
- 当前输入或注入消息。
- Extension context Hook。
- Provider 所需的最终消息转换。

Compaction 和 branch summary 是 Session 表示机制，不是多套 ContextBuilder。

### 9. steering 与 follow-up

- Steering Message 在当前工具批次结束后、下一次模型请求前注入。
- Follow-up Message 在当前 Agent 工作自然结束且没有待处理 steering 时注入。
- pending message 处于当前 Agent Run 的运行态。
- 消息真正注入后才进入 Session。
- queue 状态通过产品级事件对 Host 可见。

### 10. Extension

Kernel 接受显式构造的 Python Extension 实例，不扫描目录、不加载 Python entry point，也不支持热重载。

Extension 可以注册：

- Tool
- Provider
- SessionEntry 类型
- 固定 Hook handler

Kernel-focused Hook 覆盖：

- input
- before-agent-start
- context
- Provider request/response
- agent、turn 和 message lifecycle
- ToolCall 与 ToolResult
- Tool Execution lifecycle
- Session、Compaction 与 tree lifecycle
- agent-settled

Extension handler 按确定性注册顺序执行。Extension 不得直接取得并任意修改 Kernel 内部状态。

ToolCall Hook 发生在权限检查之前。Hook 修改后的最终参数必须重新校验；Permission Request 与这些最终参数绑定；Extension 不能在批准后再次修改目标参数，也不能把未执行或被拒绝的操作改写为执行成功。

不实现：

- TUI renderer
- shortcut
- theme
- overlay/dialog
- 完整 CLI command/flag 插件宿主
- 自动发现
- hot reload
- Cordis lifecycle

未来 Host 可以自行发现 Extension，然后把实例显式交给 Kernel。

### 11. CodingEnvironment

CodingEnvironment 将文件和进程操作与 AgentLoop 分离，负责：

- workspace 路径解析和 containment。
- 文件读取、写入和编辑。
- 命令启动。
- stdout/stderr 增量传递。
- timeout 与取消。
- 进程退出状态。
- evaluator 环境中的代码仓库操作。
- 为 Permission Policy 提供规范化 workspace、目标和可保证的隔离信息。

预期至少存在：

- Local workspace Adapter。
- SWE-bench evaluator/container Adapter。

在非 `full` 模式下，CodingEnvironment 执行 workspace containment；在 `plan` 模式下，仅在 Adapter 能够实际保证只读时运行诊断命令；在 `full` 模式下跳过 Kernel containment，但保留取消、timeout、输出流和操作系统错误。

CodingEnvironment 提供执行约束，但项目不声称实现 Docker、micro-VM 或操作系统级安全 Sandbox，也不把“命令看起来安全”当作真实隔离证明。

### 12. Terminal CLI

Terminal CLI 是主要可运行 Host，负责：

- 接收任务。
- 创建或恢复 Session。
- 启动 AgentRun。
- 选择并持续显示当前 Permission Mode。
- 渲染 AgentSessionEvent。
- 展示 Permission Request 的 Tool、最终参数、目标和原因，并提交单次批准或拒绝。
- 提交 Steering Message 和 Follow-up Message。
- 请求取消。
- 显示最终结果和代码修改。

CLI 不持有 AgentLoop、Tool 调度、ContextBuilder、Permission Policy 或 Session persistence 规则。进入 `full` 模式时必须明确展示风险；恢复 Session 时必须重新选择模式。

### 13. 源码归属与上游参考

- Pi/Tau 用于理解设计和行为，不复用其 runtime。
- 项目不复制或逐行翻译上游实现。
- 开发期间可以保留行为来源和语义对照，帮助实现审计。
- 如果未来实际复用上游代码，必须遵循相应许可证和 notice 要求。
- 公开项目描述不声称从未参考 Pi/Tau，也不将“复制 Pi”作为项目卖点。
- ADR 0001 作为必要的架构 provenance 保留在公开仓库中；README、简历和项目介绍不以“复现 Pi”作为核心卖点。

### 14. 完成定义

Kernel 完成时应能够：

- 通过 Terminal CLI 接收真实代码任务。
- 使用 DeepSeek streaming 和 ToolCall。
- 在本地仓库执行工具并产生修改。
- 持久化、关闭并恢复 Session。
- 展示 steering/follow-up、并发工具、工具失败、Permission Request 和 Extension Hook 的实际行为。
- 生成 patch 或 SWE-bench prediction 所需结果。
- 在官方 SWE-bench Harness 中完整执行至少一个 Verified 实例并获得结果。

不设最低得分、实例数量或排行榜目标。

### 15. Permission Mode 与 Permission Policy

每次 Agent Run 必须具有一个由 Host 选择的 Permission Mode：

| 模式 | Kernel 行为 |
|---|---|
| `plan` | 只允许已确定为只读且受约束的操作；拒绝写入、网络和 workspace 外操作 |
| `ask` | 自动允许 workspace 内只读操作；写入、网络、workspace 外操作和无法可靠分类的命令需要确认 |
| `auto` | 自动允许 workspace 内可识别的常规读写与命令；网络、workspace 外操作和无法可靠分类的命令需要确认 |
| `full` | 跳过 Kernel 的批准和 workspace containment；仍受操作系统权限、取消和进程生命周期约束 |

补充规则：

- 新 Agent Run 默认使用 `auto`。
- Permission Mode 在一次 Agent Run 内不可由模型、Tool 或 Extension 修改。
- Host 可以为新的 Agent Run 选择不同模式。
- `full` 不会从旧 Session 自动恢复；恢复 Session 时必须再次显式选择。
- `full` 不提升操作系统权限，也不绕过容器、账户或宿主系统本身的限制。

Permission Policy 是 ToolRuntime 与 CodingEnvironment 之间的固定 Kernel seam。它基于最终 ToolCall 产生的 Operation Intent 作出 `allow`、`deny` 或 `ask` 决定。

Operation Intent 至少表达：

- workspace 内只读文件操作。
- workspace 内文件修改。
- workspace 外操作。
- 命令执行。
- 网络访问。
- 无法可靠判断影响的操作。

该分类与 `parallel/sequential` Execution Mode 完全独立：Execution Mode 决定如何调度，Permission Mode 决定是否授权；不能重新使用 READ/WRITE/EXECUTE 作为工具调度分类。

### 16. ToolCall 授权顺序

ToolCall pipeline 固定为：

```text
参数准备与 schema validation
→ Extension tool_call Hook
→ 对修改后的最终参数重新校验和规范化
→ 生成 Operation Intent
→ Permission Policy
→ 必要时等待 Host 批准
→ Tool Execution
→ Extension tool_result Hook
→ ToolResult
```

约束：

- 权限检查必须发生在 Extension 修改参数之后。
- Permission Request 必须绑定 ToolCall ID、最终参数和 Operation Intent。
- 参数发生变化后，旧 Approval 失效。
- Extension 不能在 Approval 后再次修改目标参数。
- Permission denial 产生 Kernel 所有的错误 ToolResult。
- Extension 可以补充拒绝结果的模型可见说明，但不能把“未执行”改写为“已执行成功”。

这是加入权限系统后对 Pi Hook 顺序的必要安全收敛。

内置文件工具根据规范化路径和操作类型生成 Operation Intent。对于 `bash`：

- 不能仅根据 Tool 名称判断权限。
- Kernel 必须检查最终命令、工作目录和可识别目标。
- 已知只读诊断命令可以标记为只读。
- 无法可靠判断影响的命令必须标记为 `unknown`。
- `ask/auto` 中的 `unknown` 命令请求确认。
- `plan` 中的 `unknown` 命令直接拒绝。
- `full` 中不进行 Kernel 级批准。

`plan` 只能在 CodingEnvironment 能够实际保证只读约束时运行诊断命令；如果当前 Adapter 无法保证，只能拒绝该命令，不能仅依靠提示词声称它是“只读沙箱”。

### 17. Approval protocol

当 Permission Policy 返回 `ask` 时：

1. Tool Execution 暂停。
2. AgentRun 产生 `permission_requested` AgentSessionEvent。
3. Host 展示 Tool、最终参数、Operation Intent、目标和请求原因。
4. Host 通过 `AgentRun.resolve_permission` 提交一次性 `approve` 或 `deny`。
5. AgentRun 产生 `permission_resolved` AgentSessionEvent。
6. 批准后执行 ToolCall；拒绝后生成错误 ToolResult。

初始实现只提供单个 ToolCall 的一次性 Permission Request，不提供永久允许规则。

以下情况按拒绝处理：

- Host 取消 Agent Run。
- Host 与 Agent Run 断开且无法继续取得决定。
- ToolCall 参数在等待期间失效。
- Permission Request 无法对应当前活跃 ToolCall。

`permission_requested` 和 `permission_resolved` 至少携带 Permission Request ID、ToolCall ID、Tool 名称、当前 Permission Mode、规范化目标、Operation Intent、请求原因和最终决定。

只有持有 AgentRun 的 Host 可以响应 Permission Request。模型消息、ToolResult 和 Extension handler 都不能模拟 Host Approval。

## Testing Decisions

### 测试原则

好的验证证明公开行为与关键不变量，而不是复刻内部实现。测试、评测、Review 和报告都是交付证据，不是独立产品能力。

不为每个 Module 建立独立测试 ticket，也不先开展 Provider、策略或来源比较项目。

### 主要验收 seam

1. **Terminal CLI**

   最高产品 seam。使用真实 DeepSeek、Local CodingEnvironment 和 JSONL Session 执行代码任务，观察事件、代码修改、权限交互和恢复结果。

2. **AgentKernel/AgentRun**

   主要确定性 seam。使用 Fake Provider 与 In-memory SessionStore 验证事件顺序、ToolCall 循环、steering/follow-up、权限模式、取消、重试和 Extension 行为。

3. **SWE-bench evaluator**

   最终系统 seam。运行一个完整 SWE-bench Verified 实例，生成预测并获得官方 Harness 结果。

### 聚焦验证范围

自动验证集中在：

- Provider streaming 到 AssistantMessage 的增量组装。
- Agent Run 与 Turn 生命周期顺序。
- parallel/sequential ToolResult 顺序。
- Tool 错误转换为模型可见 ToolResult。
- steering 与 follow-up 的 drain point。
- Session append、resume 和 Active Branch projection。
- Compaction 后的 Model Context。
- Extension handler 的确定性组合、阻断和改写。
- cancellation 与主要恢复路径。
- JSONL 和 In-memory Adapter 的必要契约一致性。
- Permission Mode 与 Operation Intent 的授权矩阵。
- Extension 修改最终参数后重新授权及旧 Approval 失效。

权限矩阵至少覆盖：

| 场景 | `plan` | `ask` | `auto` | `full` |
|---|---|---|---|---|
| workspace 内读取 | allow | allow | allow | allow |
| workspace 内写入 | deny | ask | allow | allow |
| workspace 外操作 | deny | ask | ask | allow |
| 网络访问 | deny | ask | ask | allow |
| 无法分类的命令 | deny | ask | ask | allow |

还应验证：

- Permission Mode 不影响 `parallel/sequential` Tool 调度语义。
- Extension 修改参数后，权限检查使用修改后的最终参数。
- 参数变化会使已有 Approval 失效。
- deny 产生模型可见错误 ToolResult，且没有执行副作用。
- Extension 不能把 denied operation 伪装为已执行。
- Host 取消或断开时 pending Permission Request 不会继续执行。
- Session 恢复不会自动恢复 `full` 或执行旧 Permission Request。
- `plan` 模式在无法保证只读的 CodingEnvironment 中拒绝 shell command。
- `full` 只跳过 Kernel Policy，不提升操作系统权限。
- Permission events 与 Session 记录不泄露环境凭据。

不为四种模式分别创建独立测试 ticket；权限模式作为 Tool Execution 纵向能力交付。

### 每项能力的个人验收

每个实现 ticket 必须给出：

- 实际构建的 Kernel 能力。
- 用户可以亲自运行的命令或场景。
- 可观察的 Event Stream 或 Session 结果。
- 正常行为和主要故障行为。
- 该机制继承、简化或必要偏离 Pi 的说明。
- 面试时可以解释的对象、状态和调用路径。

不能以“完成了测试报告”代替真实能力。

### 既有测试先例

当前仓库尚无实现代码或测试。Pi 的行为语义和固定 ADR 是设计先例，但本项目测试必须通过自己的公开 interface 观察 Python 实现，而不是测试与 Pi 内部代码结构一致。

## Out of Scope

- 新 Agent 架构或超越 Pi/Tau 的主张。
- 科研、论文到代码或代码到数学语言功能。
- 长期记忆和向量检索。
- 自动无条件沉淀记忆。
- 多 Agent 和写入型 subagent。
- MCP。
- IDE 集成。
- 完整 TUI。
- TUI Extension、主题、快捷键和 renderer。
- Cordis 或 DeepSeek Harness 插件 runtime。
- Extension 自动发现、entry point 和 hot reload。
- 完整 Provider 生态。
- Provider 选型 benchmark。
- Provider-specific prompt 优化项目。
- SWE-bench 排行榜优化。
- 最低 SWE-bench 分数或固定实例数量。
- 生产级 Docker/micro-VM Sandbox 声明。
- 外部用户采用量、商业化和完整产品运营。
- 按 v0.1/v0.2/v0.3 分割功能范围。
- 由规划过程决定开发周期、日期或评测费用。
- 将 Permission Mode 描述为生产级安全 Sandbox。
- 通过提示词保证 shell command 只读。
- 对任意 shell command 进行完美静态副作用分析。
- 恶意代码检测、病毒扫描或完整代码审查。
- 永久信任某一命令、路径或网络域的复杂规则系统。
- 由模型或 Extension 自行批准或提升 Permission Mode。
- `full` 模式下的操作系统提权。
- 在 CodingEnvironment 不支持时声称存在文件系统或网络硬隔离。

## Further Notes

- 仓库冻结 Spec 时只有初始化 README、仓库指令、Glossary 与 ADR，没有实现代码；后续 planning 不得虚构现有模块、入口命令或测试工具。
- 所有 Pi 默认继承机制都属于已关闭设计节点。实现时可以继续核对源码事实，但不得把核对工作拆成无能力产出的研究或测试 tickets。
- 旧讨论中出现过开发周期限制、科研方向和“不追求 Pi/Tau 功能对等”的表述；这些均已被“不设周期约束、删除科研场景、在选定 Kernel 范围内默认继承 Pi”的决定覆盖。
- Permission Mode 是项目特有能力，不标记为 Pi 默认继承项，也不会重新引入 READ/WRITE/EXECUTE 工具调度分类。
- ADR 0001 保留为公开仓库中的架构 provenance；README、简历与面试主张不以“复现 Pi”作为卖点。
- 本文档是 `plan-product-loop` 的唯一 Parent 输入。后续范围变化必须作为显式 Spec amendment 记录，不能在 ticket 拆分时静默改变。
