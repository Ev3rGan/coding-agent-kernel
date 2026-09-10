# Coding Agent Kernel 文档

这里保存 README 首页之外的稳定使用说明、架构边界、设计决策和验证证据。

## 从哪里开始

| 目标 | 文档 |
| --- | --- |
| 在本地观察一个确定性 Agent Run | [确定性演示](guides/demos.md) |
| 使用 DeepSeek 运行真实编码任务 | [DeepSeek 运行指南](guides/deepseek.md) |
| 运行一个 SWE-bench Verified 实例 | [SWE-bench 指南](guides/swebench.md) |
| 理解 Kernel 的责任边界 | [架构说明](architecture.md) |
| 查找规范术语 | [CONTEXT.md](../CONTEXT.md) |
| 阅读完整项目范围与验收定义 | [Canonical Spec](specs/coding-agent-kernel.md) |

## 架构与设计决策

- [架构说明](architecture.md)：`AgentKernel`、`AgentRun`、Provider、Tool、Session、Context、
  Permission 与 Extension 如何协作。
- [ADR 0001](adr/0001-pi-baseline-independent-python-kernel.md)：为什么以 Pi 为行为基线并保持
  Python 独立实现。
- [ADR 0002](adr/0002-headless-kernel-and-thin-host.md)：为什么 Kernel 保持 Headless、Host 保持薄。
- [ADR 0003](adr/0003-kernel-focused-extensions.md)：为什么 Extension 只通过固定 Kernel seam 扩展。
- [ADR 0004](adr/0004-host-controlled-run-permissions.md)：为什么权限由 Host 按 Run 控制。
- [Context Compaction v2](design/context-compaction-v2.md)：语义 checkpoint、近期窗口和权威资源重投影。

## 研究与计划

- [Context Compaction systems research](research/context-compaction-systems.md)
- [Coding Agent Kernel product loop](plans/coding-agent-kernel-product-loop.md)

这些文档记录问题分析、选择依据和实施顺序；它们不替代当前源码、Canonical Spec 或
最终验证证据。

## 验证证据

- [Issue #33：真实 SWE-bench Context Compaction 验收](validation/issue-33-context-compaction/README.md)
- [PR #35 合并前质量门禁](validation/issue-33-context-compaction/development-gates/pr-35.md)
- [PR #36 默认窗口补充门禁](validation/issue-33-context-compaction/development-gates/pr-36-default-window.md)

验证目录中的结论只适用于其中固定的候选版本、配置和实例。单元测试、smoke test、
失败样本和官方 Harness 结果必须按各自含义解释。

## 参与项目

- [贡献指南](../CONTRIBUTING.md)
- [安全策略](../SECURITY.md)
- [行为准则](../CODE_OF_CONDUCT.md)
- [Issue tracker 约定](agents/issue-tracker.md)
- [Triage label 词表](agents/triage-labels.md)

返回[中文 README](../README.md)或[English README](../README.en.md)。
