# Coding Agent Kernel

## 用 DeepSeek 运行真实本地编码路径

先在进程环境中设置 `DEEPSEEK_API_KEY`，再对 disposable 或明确授权的 workspace
运行同一条公开 Kernel 路径：

```console
python -m coding_agent run --provider deepseek --workspace <workspace> --mode ask "<coding-task>"
```

CLI 使用固定的官方 `https://api.deepseek.com/chat/completions` endpoint，默认模型是
`deepseek-v4-pro`，也只允许当前明确支持的 `deepseek-v4-pro` 和
`deepseek-v4-flash` 标识。凭据不接受命令行参数或配置文件，只从当前进程环境读取；
缺失时命令在创建 Session 或发起网络请求前以 `deepseek_api_key_missing` 退出。
CLI 在 Provider 捕获凭据后，会在 Agent Run 期间从可被 Tool 子进程继承的环境中移除它，
结束后再恢复 Host 进程环境。不要把 API key 写入 task、workspace、Session 或 shell history。

每次新运行会创建 append-only JSONL Session，并在最终记录中打印 Session ID 和文件
路径。使用同一个 store 恢复已关闭的 Session：

```console
python -m coding_agent run --provider deepseek --workspace <workspace> --mode ask \
  --session-file <sessions.jsonl> --resume <session-id> "<next-coding-task>"
```

Host 持续把 `AgentSessionEvent` 渲染成 JSON Lines。`ask` 模式在
`permission_requested` 后从 stdin 接受一次 `approve` 或 `deny`；空输入和其他输入
默认拒绝。最终输出包含 authoritative result、Session 信息、changed paths、文本 patch
和单独列出的 binary paths。workspace snapshot 忽略 `.git`、symlink 和位于 workspace
内的 Session 文件，不会为了生成 patch 修改仓库。

Adapter 的协议依据是 DeepSeek 官方
[Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)、
[Thinking Mode](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)、
[Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/) 与
[Error Codes](https://api-docs.deepseek.com/quick_start/error_codes/)。请求使用 Bearer auth、
`stream=true` 和 `stream_options.include_usage=true`；SSE 可跨任意 byte boundary，
`reasoning_content`、`content`、增量 `tool_calls`、usage、finish reason 与 `[DONE]`
分别规范化到既有 Provider event contract。最终 ToolCall arguments 仍由 Kernel 的
`AssistantMessageAccumulator` 组装并验证，然后经过 Extension Hook、最终参数重验证和
Host permission resolution，Adapter 不执行 Tool 或复制 AgentLoop。

HTTP 429、500/503、timeout/transport interruption 映射到既有有限 retry 分类；格式、
认证、余额、API error 和 malformed stream 产生不可泄露 server body 或 key 的结构化
失败。确定性测试使用注入的 HTTP transport，不会访问 DeepSeek 或产生费用。
**带真实凭据的 live acceptance 尚未执行，仍是 Ready PR 前的明确 remaining gate。**

本能力借鉴 Pi 的 Provider normalization；简化为 DeepSeek + Fake 两个 Adapter；深化点
是把同一 Python Kernel seam 用于真实 CLI、权限和恢复。它不引入 Provider 生态、模型
比较、provider-specific prompt 优化、生产级 TUI/sandbox 或 SWE-bench 路径。

## 固定 Extension 合约

`AgentKernel` 接受调用方按顺序显式构造的普通 Python Extension 实例。Extension
只能通过固定 registry 注册 Tool、Provider、custom `SessionEntry` type 与 Hook
handler；没有目录扫描、entry point、自动发现或热重载。Hook 只接收不可变的类型化
快照，合法的 transform/supplement 会在交给下一 handler 前由 Kernel 重新验证。
Extension Tool 继续使用既有 `ToolRuntime` 的 schema、调度、取消与 structured
`ToolResult` 路径；custom entry 继续使用 append-only Session/Store 路径。

### 同步 callout 与线程契约

Extension 的 `register()`、Hook handler 和 custom `SessionEntry` validator 都是同步
callout，并在独立 worker thread 中运行。它们必须在有限时间内同步返回，且必须线程
安全：不得 `await`、不得访问或操作绑定到 Host event loop 的 asyncio 对象，也不得
在没有自行同步的情况下读写与 Host 或其他 callback 共享的可变状态。推荐只读取
Kernel 提供的 owned snapshot，并通过明确的 outcome 返回候选值。

Kernel 会把可检测的违规确定性转换为 registration/dispatch/validation failure：返回
awaitable、抛出 `CancelledError`、访问当前 running loop 或返回非法 outcome 都不会
取得 Host Task 的取消权，其中 handler cancellation 使用独立的
`handler_cancelled` 诊断码。Python thread 无法被宿主安全强制终止，因此 callback
死锁、无限阻塞和未同步 data race 不能由该合约自动修复；callout timeout 与 process
isolation 是后续 Host/plugin sandbox 的职责，不属于当前 Kernel 合约。

### `ToolResult` 权威与快照成本

`tool_result` handler 可以把已成功执行的输出降级为 `error`，用于在结果反馈给
Provider 前执行内容或策略校验；这不会回滚 Tool 已经发生的副作用。非成功结果不能
升级为 `success`，而 `cancelled` 是取消来源事实，handler 既不能引入也不能擦除该
状态。

为阻止别名逃逸，每个接收 Provider stream event 的 handler 都获得 request 与 event
的独立深快照；没有对应 handler 时不会复制 request。时间和临时内存开销因此随
`request size × handler count × event count` 线性增长。面向高频或大 Context 的 Host
应使用代表性 payload 做容量基准；在没有明确吞吐目标前，Kernel 优先保留所有权隔离，
不通过共享可变 request 来投机优化。

运行三个确定性场景：

```console
python -m coding_agent demo extensions
python -m coding_agent demo extensions --case ordering
python -m coding_agent demo extensions --case invalid-mutation
```

默认场景显式加载示例 Extension，执行自定义 Tool、向 canonical Model Context
补充资源、确定性阻断一个 ToolCall，并持久化已注册的 custom SessionEntry。
`ordering` 展示两个 Extension 按实例顺序和 handler 顺序组合，以及每次改变后的
revalidation；`invalid-mutation` 将 handler 异常转换为明确失败，不调用 Provider、
不污染 Session，也不打印 traceback。

`ExtensionEvent` 通过 `AgentKernel.drain_extension_events()` 独立消费；它记录
registration、dispatch、outcome/revalidation、block/rejection/failure，但绝不会自动
混入 `AgentRun` 的公开 `AgentSessionEvent` Event Stream。本能力借鉴 Pi/Tau 的
registration 与 Hook 思路，简化自动发现、TUI 与热重载，并深化固定状态所有权、
确定性组合和逐次重验证规则。

## 可恢复、可分支的 Session

Kernel 现在提供持久化的 append-only Session tree。每个不可变
`SessionEntry` 都有稳定 ID 和 parent；当前 `Active Branch` 是从根到所选
leaf 的可恢复路径。`InMemorySessionStore` 与 `JsonlSessionStore` 共享同一
契约，JSONL 使用本项目独立且显式版本化的 schema。

运行确定性的本地演示：

```console
python -m coding_agent demo session-tree
python -m coding_agent demo session-tree --case invalid-entry
```

成功场景实际创建 Session，在权威 `message_end` 后持久化消息，关闭并通过新
store 实例重新加载，从旧 entry fork，再展示两条 sibling branch、当前
Active Branch 和可检查的 JSONL 路径及记录。失败场景以结构化错误拒绝非法
parent，退出码为 1，不静默改写历史或选择其他 branch。

Session 是持久、权威的树；Agent Run 是一次活跃执行；Active Branch 是当前
选择的可恢复历史；Model Context 是为单次 Provider 请求构造的有界投影。
本实现借鉴 Pi 的 tree 与确定性投影语义，但使用独立 Python 文件格式，并通过
双 SessionStore seam 强化恢复性与可测性。

## 确定性 Model Context 与 Compaction

每次 Provider 调用都经过唯一的 Context pipeline，固定按 system prompt、active
Tool 描述/guideline、项目资源、Active Branch 投影、当前 injected messages 和
ProviderRequest conversion 组装。`ModelContext` 是不可变值，不持有完整 Session
或 mutable queue。sibling branch 与尚未注入的 pending message 不进入请求。

运行成功与确定性摘要失败场景：

```console
python -m coding_agent demo context-compaction
python -m coding_agent demo context-compaction --case summary-error
```

成功场景展示 compaction 前后以 canonical JSON characters 计量的预算（不是精确
token 数）、持久化 checkpoint、`compaction_succeeded` 事件、两条 sibling
branches、pending/injected 排除与包含证据，以及仍保留的原始 entries。Active
Branch 使用最近有效 checkpoint 的 summary 加 checkpoint 后 entries；checkpoint
记录版本、覆盖范围和 summary，不删除旧历史。

失败场景在 Provider 调用前发出结构化 `compaction_failed`，退出码为 1；它不写入
无效 checkpoint、不删除或改写原始 entries，也不会降级到第二套 builder。Session
仍可关闭、恢复和导航。这里借鉴 Pi 的确定性投影与 compaction 语义，简化
provider-specific prompt 优化，并深化为可观察的 Python Context seam。长期记忆、
向量检索与 Extension registration 仍不在本能力内。

## 控制进行中的 Agent Run

`AgentRun` 是 steering、follow-up、cancel、result/wait 与 Event Stream 的唯一公开
入口。Steering Message 与 Follow-up Message 使用两个独立的 run-scoped FIFO queue：
steering 在完整 Tool batch 之后、下一次 Context/Provider request 之前注入；follow-up
在当前 agent work 自然结束且 steering 已清空后启动后续工作。pending message 不是
Session history，只有实际 injection 才产生权威 user `SessionEntry`。

```console
python -m coding_agent demo run-control --case steering
python -m coding_agent demo run-control --case follow-up
python -m coding_agent demo run-control --case cancel
python -m coding_agent demo run-control --case retry-success
python -m coding_agent demo run-control --case retry-failure
```

JSON Lines 会显示 queue、injection/drop、Provider retry、Session 和唯一终态证据。
取消传播到 Provider、Tool Execution 与 retry wait，并丢弃尚未注入的消息。有限 retry
只处理明确分类为 retryable 的 Provider failure，复用同一 ProviderRequest，失败 attempt
的 partial delta 不会成为权威 Session message。本实现直接借鉴 Pi 的 inner steering、
outer follow-up 与 settled 语义，以统一 Python `AgentRun` interface 表达；不增加 Step、
第二 AgentLoop、多 Agent 或长期后台服务。

## Host 权限模式

每次 `AgentRun` 都由 Host 选择 `plan`、`ask`、`auto` 或 `full`，默认是 `auto`。
Host 必须持续消费 `AgentRun` Event Stream；收到 `permission_requested` 后，只能通过
`AgentRun.resolve_permission()` 对该次请求批准或拒绝。若 Host 既不处理请求也不取消或
关闭 run，run 会继续等待决策，不会由 Kernel 猜测超时或自动提升权限。`ask` 会自动
允许 workspace read；`ask/auto` 对 outside、network、unknown 和没有 target contract
的 custom Tool 请求一次性确认。

从早期无限制 shell 行为迁移的可信集成，应实现上述请求处理；只有在一次明确可信、
可丢弃的运行中才显式选择 `full`。`full` 仅跳过 Kernel approval/containment，仍受 OS
权限、取消、timeout 与进程生命周期约束；它不是生产级 sandbox 或 OS 提权。
`ToolRuntime.execute_batch()` 是供已完成授权的 Host adapter 使用的历史低层 seam；
产品权限边界是 `AgentKernel` 经最终参数重验证后调用 guarded runtime 的路径。

## English

An independently implemented Python kernel for coding agents, focused on
runtime semantics, tool execution, sessions, and observable event streams.

## Fixed Extension contract

Callers pass explicitly constructed Python Extension instances to `AgentKernel` in a
defined order. The fixed registry accepts Tools, Providers, custom SessionEntry types,
and Hook handlers only. Every transform or supplement is revalidated before the next
handler, custom Tools remain inside ToolRuntime, and custom entries remain inside the
append-only Session/Store path. `ExtensionEvent` is drained independently from the
Kernel and is never inserted into the public AgentSessionEvent stream.

Run `python -m coding_agent demo extensions`, then use `--case ordering` and
`--case invalid-mutation` to inspect successful capability use, deterministic
composition, and explicit rejection without state damage. The design borrows the
registration and Hook ideas from Pi/Tau, omits discovery/TUI/hot reload, and deepens
Kernel-owned state and deterministic revalidation.

The Kernel now provides a deterministic, observable model-tool-model loop. A
thin Terminal CLI drives the same public `AgentKernel`/`AgentRun` seam that
later Hosts will reuse.

## Development setup

Python 3.11 or newer is required.

```console
python -m pip install -e ".[dev]"
```

## Observable Headless Agent Run

Run the successful scripted Fake Provider case:

```console
python -m coding_agent demo streamed-run
```

The CLI prints JSON Lines in lifecycle order. Provider increments appear as
`message_update` events containing both the cumulative `AssistantMessage` and
the current Provider Stream Event. The later `message_end` contains the
authoritative message, followed by one `run_settled` terminal event and a final
result record.

Run the deterministic failure case:

```console
python -m coding_agent demo streamed-run --case provider-error
```

This case prints the provider's incremental error, a normalized Agent error,
and one `run_failed` terminal event. It exits with status 1 without an unhandled
exception or traceback.

## Model-tool-model loop

Run the disposable coding task:

```console
python -m coding_agent demo tool-loop
```

The Fake Provider incrementally constructs `read`, `edit`, and `bash`
ToolCalls. The core ToolRuntime executes them in a temporary
LocalCodingEnvironment, sends ordered ToolResults into the next Turn, and the
Provider summarizes the result. JSON Lines expose Provider events, Tool
Execution events, the authoritative final message, and a workspace
before/after/diff record.

Two additional deterministic cases expose scheduling and failure behavior:

```console
python -m coding_agent demo tool-loop --case mixed-batch
python -m coding_agent demo tool-loop --case failure
```

`mixed-batch` demonstrates a pure parallel read batch followed by a mixed batch
that runs entirely sequentially. `failure` normalizes an unknown Tool, invalid
arguments, and a non-zero command into ToolResults before the next Turn ends
normally.

The built-in Tool set is `read`, `write`, `edit`, `bash`, `grep`, `find`, and
`ls`. The first four are enabled by default; search and listing Tools require
explicit opt-in. LocalCodingEnvironment normalizes workspace paths and manages
local processes, incremental stdout/stderr, exit status, timeout, and
cancellation. It is not a production sandbox and makes no security promise for
malicious workspaces.

## Public runtime seam

```python
from coding_agent import AgentKernel, FakeProvider


async def observe_run() -> None:
    kernel = AgentKernel(FakeProvider.streamed_run())
    run = kernel.create_run("Demonstrate an observable run.")

    async for event in run:
        ...  # consume AgentSessionEvent values

    result = await run.result()
```

`AgentRun.state` is one of `active`, `settled`, `cancelled`, or `failed`.
`AgentRun.cancel()` cancels active Provider or Tool Execution work. Model
Providers remain later-ticket work.
The implemented Context pipeline projects only the selected Active Branch and
explicitly injected messages before every Provider call.

The Run/Turn and layered event semantics follow the selected Pi behavioral
baseline. This project keeps only a thin CLI product shell, while the
asynchronously iterable `AgentRun` facade is the Python-facing public boundary.

## Host permission modes

The Host selects `plan`, `ask`, `auto`, or `full` for every `AgentRun`; the
default is `auto`. A Host must keep consuming the run's Event Stream and resolve
each `permission_requested` event only through `AgentRun.resolve_permission()`.
If it neither resolves, cancels, nor closes the run, the run waits instead of
guessing a timeout or elevating itself. `ask` automatically allows workspace
reads; `ask/auto` request one-time confirmation for outside, network, unknown,
and custom Tools without a target contract.

Trusted integrations migrating from earlier unrestricted shell behavior should
handle those requests. Select `full` only for an explicitly trusted disposable
run. `full` skips Kernel approval and containment, but not OS authority,
cancellation, timeouts, or process-lifecycle controls; it is not a production
sandbox or OS elevation. `ToolRuntime.execute_batch()` is the historical
low-level seam for a Host adapter that has already authorized a batch. The
product permission boundary is the guarded path from `AgentKernel` after final
argument revalidation.

## Recoverable, branching Sessions

`python -m coding_agent demo session-tree` creates a versioned JSONL Session,
persists only authoritative messages after `message_end`, closes and reloads it,
forks from an old entry, and prints both sibling branches plus the selected
Active Branch. The `--case invalid-entry` case rejects an illegal parent with a
structured error. Session is durable authoritative history; Agent Run is one
active execution; Active Branch is one recoverable root-to-leaf path; Model
Context is one bounded Provider projection. The design borrows Pi's tree
semantics, uses an independent Python persistence format, and deepens
recoverability and testability through matching in-memory and JSONL store
seams.

## Deterministic Model Context and Compaction

`python -m coding_agent demo context-compaction` shows the single Context
pipeline, a character-count budget, a persisted versioned checkpoint, two
sibling branches, pending-message exclusion, explicit injection, and preserved
raw history. The final Fake Provider request contains only the bounded Active
Branch projection. The `--case summary-error` case fails before the Provider,
emits a structured compaction failure, writes no invalid checkpoint, and leaves
the Session resumable. The estimator counts canonical JSON characters and does
not claim tokenizer-exact token counts.

## Controlling an active Agent Run

`python -m coding_agent demo run-control --case <case>` exposes five deterministic
steering, follow-up, cancellation, retry-recovery, and retry-exhaustion scenarios.
The thin CLI observes the public Event Stream and invokes only `AgentRun` controls.
Pending messages stay in two run-scoped FIFO queues and enter Session history only
when injected at their authoritative drain point. Cancellation drops uninjected
messages and converges Provider, Tool, and retry work on one terminal result.
