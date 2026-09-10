# SWE-bench Verified 运行指南

本入口通过与 Terminal CLI 相同的 `AgentKernel` / `AgentRun` seam 执行一个官方
SWE-bench Verified 实例，并保存可审计 prediction、Session、patch 与 Harness 结果。

> [!WARNING]
> SWE-bench 运行会启动容器、执行不受信任的实例代码并产生模型费用。只在隔离、可丢弃且
> 明确授权的环境中执行。不要把本指南理解为生产调度或 Sandbox 保证。

## 前置条件

- 可执行的 `git`，用于验证官方 base commit 并生成最终 prediction patch；
- Docker Desktop Linux daemon，由用户手动启动；
- 官方 SWE-bench 运行依赖；
- 已经准备好的目标 instance 官方镜像；
- 通过 Host 进程环境提供的 `DEEPSEEK_API_KEY`。

安装包含官方 Harness 的 optional dependency：

```console
python -m pip install -e ".[swebench]"
```

Windows Host 必须使用 Docker Desktop Linux containers，并在准备镜像和运行前启用
Windows Developer Mode，使官方实例仓库中的 symlink 可以正确创建和保留。

命令只执行 `docker image inspect`，不会自动 pull 或 build 大型镜像。镜像缺失时返回
`environment_preparation_failed`；应先完成环境准备，不能把降级 checkout 当作有效评测。

## 运行一个实例

```console
python -m coding_agent swebench run --instance <verified-instance> --artifacts <new-run-artifact-directory> --mode auto --timeout 1800 --harness-timeout 1800
```

`--artifacts` 必须指向新的 run artifact directory。入口固定使用官方
`SWE-bench/SWE-bench_Verified` 的 `test` split，并按代码中固定的 dataset revision 加载
instance metadata。

`--mode auto` 会在 elevated Tool Execution 前通过 stdin 请求 Host 审批，非交互环境的 EOF
按 deny 处理。如果在隔离、一次性且已明确授权的环境中需要无人值守运行，可以显式选择
`--mode full`；扩大 authority 是调用方的决定。

## 执行边界

Evaluator 会：

1. 按固定 revision 解析官方 instance metadata；
2. 将镜像中的 `/testbed` 复制到 artifact directory 拥有的新 workspace；
3. 验证 workspace 位于官方 `base_commit` 且初始状态 clean；
4. 以 `--network none` 把 workspace bind mount 给单一 agent container；
5. 让文件 Tool 只操作该 workspace，让 `bash` 通过严格 argv 的 `docker exec` 运行；
6. 从 evaluator-owned workspace 相对 `base_commit` 的真实 `git diff --binary` 生成 prediction；
7. 让官方 Harness 对同一固定 instance snapshot 进行评估。

Provider key 和 Host secret 不进入 container、Tool subprocess 或 Harness subprocess。无 patch、
非法 diff 或越界路径不会进入 Harness。

## Artifact bundle

一次完整运行可能包含：

| Artifact | 含义 |
| --- | --- |
| `config.json` | sanitized 用户运行配置 |
| `provenance.json` / `kernel_configuration.json` | 固定版本、数据来源和 Kernel 投影设置 |
| `official_instance.json` | 固定 revision 的官方实例快照 |
| `session.jsonl` / `events.jsonl` / `tool_results.jsonl` | Session、事件与 ToolResult 审计记录 |
| `workspace.patch` / `prediction.jsonl` | 实际工作区 diff 与官方 prediction contract |
| Harness stdout/stderr、summary、report | 官方评估调用和结果 |
| `manifest.json` | bundle 的唯一运行终态 |

Manifest 会区分环境准备失败、Agent failure、timeout、cancel、no patch、prediction invalid、
Harness invocation/rejection/failure 和 success。未执行 Harness 或未得到一致官方结果的运行
不会显示为通过。

## 如何解释结果

- 仓库单元测试证明实现契约，不替代真实 Harness；
- smoke run 证明路径可执行，不等同于 instance resolved；
- 一个 resolved instance 只证明对应固定候选、模型、参数和实例；
- 失败 artifact 应保留并按 manifest stage 分析，不能用后续成功结果覆盖；
- character budget 是 canonical JSON character estimate，不是精确 token budget。

当前归档的两个成功样本和一个保留失败样本见
[Issue #33 验收档案](../validation/issue-33-context-compaction/README.md)。

返回[文档入口](../README.md)或[中文 README](../../README.md)。
