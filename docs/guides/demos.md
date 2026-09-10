# 确定性演示指南

这些演示使用 Fake Provider 和临时资源重现成功、失败、取消与恢复行为。它们不访问模型
服务，也不会产生 API 费用，适合首次体验和回归验证。

## Agent Run 与 Provider failure

```console
python -m coding_agent demo streamed-run
python -m coding_agent demo streamed-run --case provider-error
```

成功场景按生命周期顺序输出 Provider 增量、累计 `AssistantMessage`、权威 `message_end`、
唯一 `run_settled` 和最终 result。失败场景输出 Provider error、规范化 Agent error 和唯一
`run_failed`，退出码为 1，不打印未处理 traceback。

## Model–tool–model loop

```console
python -m coding_agent demo tool-loop
python -m coding_agent demo tool-loop --case mixed-batch
python -m coding_agent demo tool-loop --case failure
```

默认场景在临时 workspace 中执行 `read`、`edit` 和 `bash`，把有序 ToolResults 送回下一
Turn，再由 Provider 总结结果。

- `mixed-batch`：纯读取批次并行；包含串行 Tool 的混合批次整体串行。
- `failure`：unknown Tool、非法参数和非零命令都规范化为 ToolResult，AgentLoop 可以继续。

内置 Tool 是 `read`、`write`、`edit`、`bash`、`grep`、`find` 和 `ls`。前四个默认启用，
搜索与列举 Tool 需要显式 opt-in。`LocalCodingEnvironment` 是本地执行抽象，不是生产 Sandbox。

## Session tree

```console
python -m coding_agent demo session-tree
python -m coding_agent demo session-tree --case invalid-entry
```

成功场景创建 JSONL Session，在权威消息后持久化，关闭并用新的 Store 实例重新加载，再从
旧 entry fork。输出包含两条 sibling branches、当前 Active Branch 和可检查文件路径。
失败场景以结构化错误拒绝非法 parent，不静默改写历史。

## Model Context 与 Compaction

```console
python -m coding_agent demo context-compaction
python -m coding_agent demo context-compaction --case summary-error
```

成功场景展示 canonical character budget、语义 checkpoint、sibling exclusion、pending
message exclusion、显式 injection 与原始历史保留。字符估算不是 tokenizer-exact token count。

失败场景在业务 Provider 调用前产生 `compaction_failed`，不写入非法 checkpoint，不删除原始
entries，Session 仍然可恢复和导航。

## Run control

```console
python -m coding_agent demo run-control --case steering
python -m coding_agent demo run-control --case follow-up
python -m coding_agent demo run-control --case cancel
python -m coding_agent demo run-control --case retry-success
python -m coding_agent demo run-control --case retry-failure
```

这些场景展示两条独立 FIFO queue、权威 injection 时点、取消传播、有限 Provider retry 和
唯一终态。pending message 只有实际注入后才成为 Session history。

## Extensions

```console
python -m coding_agent demo extensions
python -m coding_agent demo extensions --case ordering
python -m coding_agent demo extensions --case invalid-mutation
```

默认场景显式加载示例 Extension，执行自定义 Tool、补充 ContextResource、阻断一个 ToolCall，
并持久化自定义 SessionEntry。`ordering` 展示 handler 组合与逐次重验证；
`invalid-mutation` 展示非法 outcome 如何在 Provider 和 Session side effect 前被拒绝。

## Permission modes

```console
python -m coding_agent demo permissions --mode plan
python -m coding_agent demo permissions --mode ask
python -m coding_agent demo permissions --mode auto
python -m coding_agent demo permissions --mode full
```

四个场景使用同一组意图展示 allow、deny、ask 和 full risk，同时证明 `full` 不会改变 Host
进程的 OS authority。更多 permission edge cases 可通过 `--case extension-rewrite`、
`cancel`、`host-disconnect` 和 `resume` 运行。

返回[架构说明](../architecture.md)、[文档入口](../README.md)或[中文 README](../../README.md)。
