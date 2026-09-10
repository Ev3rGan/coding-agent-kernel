# Coding Agent Kernel 架构说明

本文面向希望理解 Kernel 运行机制和集成边界的开发者。规范术语以
[CONTEXT.md](../CONTEXT.md) 为准，完整范围和验收定义以
[Canonical Spec](specs/coding-agent-kernel.md) 为准。

## 责任边界

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| `AgentKernel` | 组装 Provider、ToolRuntime、Session、Context、Permission 和 Extension；创建 Run | UI、用户交互或产品级调度 |
| `AgentRun` | Event Stream、steering、follow-up、取消、权限响应、终态与结果等待 | 持久化格式或 Tool 实现 |
| `ModelProvider` | 把一个 `ProviderRequest` 规范化为 `ProviderStreamEvent` 序列 | Tool Execution 或 Session 写入 |
| `ToolRuntime` | Tool schema、参数验证、批次调度、取消与结构化 `ToolResult` | Host 权限选择 |
| `CodingEnvironment` | 文件和进程操作的具体执行环境 | 决定某个操作是否被授权 |
| `Session` / `SessionStore` | append-only tree、Active Branch、恢复、分支和持久化 | 流式进度或 pending queue |
| `ContextPipeline` | 为一次 Provider 请求投影有界、确定性的 Model Context | 删除原始 Session 历史 |
| `PermissionPolicy` | 根据最终参数和 Operation Intent 产生 allow、deny 或 ask 决策 | OS 提权或生产 Sandbox |
| `ExtensionRegistry` | 固定能力注册、Hook 调度、逐次重验证和诊断 | 自动发现、热重载或产品 Shell 生命周期 |

## 一次 Agent Run 的主路径

1. Host 通过 `AgentKernel.create_run()` 创建 `AgentRun`，并为这次 Run 选择 Permission Mode。
2. Kernel 把当前 Active Branch、权威 `ContextResource`、已注入消息和 active Tool schema
   投影为 `ProviderRequest`。
3. Provider 流式产生 thinking、text、ToolCall、usage、done 或 error 事件。
4. `AssistantMessageAccumulator` 组装增量；只有 `message_end` 对应的完整消息是权威消息。
5. ToolCall 经过 Extension Hook、最终参数重验证和 Permission Policy 判断。
6. 获得授权后，ToolRuntime 按 batch 规则执行 ToolCall，并生成有序、结构化的 ToolResults。
7. ToolResults 写入下一次 Model Context，循环继续，直到 Run settle、cancel 或 fail。
8. 权威消息、ToolResults、权限记录和 checkpoint 进入 Session；Host 从 Event Stream 观察过程，
   从 `run.result()` 取得唯一最终结果。

## 事件与权威事实

- `ProviderStreamEvent` 是变化输入；`AssistantMessage` 是不可变累计快照。
- `message_update` 用于观察过程，`message_end` 才确定一条可持久化的 assistant message。
- `ToolResult` 是一次 Tool Execution 的权威结果；进度和 stdout/stderr 增量不是结果替代品。
- `AgentSessionEvent` 是 Host 面向产品编排消费的公开 Event Stream。
- `ExtensionEvent` 单独排出，不会自动混入 `AgentSessionEvent`。
- `AgentRunResult` 是整次 Run 的唯一终态结果。

这些边界使失败、取消和恢复可以被确定性解释，也避免把 partial delta 或临时 queue 状态
错误持久化成历史事实。

## Session、Active Branch 与 Model Context

Session 是持久化的 append-only tree；Active Branch 是当前选中的 root-to-leaf 路径；
Model Context 是为一次 Provider 请求构造的有界投影。三者不能互换：

- sibling branch 不会进入当前请求；
- steering 和 follow-up 在实际注入前只是 run-scoped pending message，不属于 Session；
- Context 构造不会删除或重写原始 Session entries；
- Compaction 通过独立 checkpoint 表示较早历史，同时保留近期完整 turns；
- 每次请求都会重新投影当前 system prompt、Tool guideline、项目资源和 active Tool schema。

Context Compaction v2 的 checkpoint、coverage、lineage、预算和失败语义见
[设计说明](design/context-compaction-v2.md)。

## Run control

`AgentRun` 对外提供两条独立 FIFO 控制队列：

- **Steering Message**：在当前完整 Tool batch 之后、下一次 Provider 请求之前注入。
- **Follow-up Message**：当前工作自然结束且 steering 已清空后，启动后续工作。

取消会传播到 Provider、Tool Execution 和 retry wait，并丢弃尚未注入的消息。有限 retry
只处理明确标记为 retryable 的 Provider failure；新的 attempt 复用同一个
`ProviderRequest`，失败 attempt 的 partial delta 不会成为权威 Session message。

## Permission boundary

Host 为每次 Run 显式选择：

| Mode | Kernel 行为 |
| --- | --- |
| `plan` | 禁止代码修改和外部副作用 |
| `ask` | 自动允许 workspace read，其他受控操作请求一次性确认 |
| `auto` | 自动执行 workspace 内可识别的常规操作，越界或未知操作请求确认 |
| `full` | 跳过 Kernel approval 与 workspace containment，不改变 OS 权限 |

Permission Request 绑定 Extension transform 之后的最终 ToolCall 参数与 Operation Intent。
模型和 Extension 不能伪造批准或提高 Permission Mode。完整决策见
[ADR 0004](adr/0004-host-controlled-run-permissions.md)。

## Extension boundary

调用方按顺序把普通 Python Extension 实例交给 `AgentKernel`。Extension 只能通过固定
registry 注册 Tool、Provider、自定义 SessionEntry type 和 Hook handler：

- transform 或 supplement 在传给下一个 handler 前重新验证；
- Extension Tool 继续经过 ToolRuntime、Permission 与 structured ToolResult 路径；
- custom SessionEntry 继续经过 append-only Session/Store 路径；
- Hook 接收 owned、不可变的类型化快照，不能取得 Host Task 的取消权；
- 注册、Hook 和 validator 是同步 callout，必须有限返回且自行保证线程安全。

目录扫描、entry-point discovery、热重载、TUI 和插件 Sandbox 不属于 Kernel。完整决策见
[ADR 0003](adr/0003-kernel-focused-extensions.md)。

## 继续阅读

- [确定性演示](guides/demos.md)
- [DeepSeek 运行指南](guides/deepseek.md)
- [SWE-bench 指南](guides/swebench.md)
- [文档入口](README.md)
