# Django 历史运行归档

本目录保存 v2 改造前 `django__django-11133` 的六次正式运行记录，用于和 Issue #33 合并后的真实验收对照。详细机制解释仍以 [系统调研文档](../../../../research/context-compaction-systems.md#4-swe-bench-verified-django-运行证据) 为准；这里重点说明归档范围与证据定位。

## 运行矩阵

下表的 Session entry 数不含最终 `closed` record：

| artifact id | 终态 | Session entries | v1 compactions | 官方 Harness |
| --- | --- | ---: | ---: | --- |
| `issue-10-deepseek-final-a78b-20260901-auth1` | agent_failed / turn limit | 62 | 1 | 未运行 |
| `issue-10-deepseek-final-a78b-20260901-auth2` | agent_failed / turn limit | 65 | 0 | 未运行 |
| `issue-10-deepseek-final-a78b-20260901-auth3` | agent_failed / turn limit | 61 | 0 | 未运行 |
| `issue-10-deepseek-final-a78b-20260901-auth4` | cancelled | 248 | 3 | 未运行 |
| `issue-10-deepseek-final-a78b-20260901-auth5` | success | 79 | 0 | resolved |
| `parent-1-fbab60f9-django-11133-final` | success | 58 | 0 | resolved |

## 为什么这些记录重要

`auth1` 在 sequence 56 已得到测试通过、sequence 58 已看到 diff，但 sequence 59 的 180 字符摘要只保留任务开头；sequence 60 随即重新探索仓库，最后触发 turn limit。

`auth4` 在 sequence 66、147、234 三次压缩，摘要依次出现 `user:`、`summary:user:`、`summary:summary:user:`。它们没有保留目标文件、已存在 diff、通过的 smoke test 或下一步，checkpoint 后均从理解任务和列目录重新开始。

这些记录证明旧机制存在语义丢失和递归包装，但不是严格 A/B：无压缩的 `auth2`、`auth3` 也失败，而两个 resolved 运行没有触发压缩。因此能确认的是“旧摘要导致了可观察的重复探索”，不能把所有失败都归因于 compaction。

本次 v2 验收的成功运行不再出现递归包装，并保留结构化目标、约束、完成状态、关键决定、准确文件/命令/错误与下一步；详见上级 [验收 README](../../README.md)。

## 每个运行保存的内容

- 原始 `session.jsonl` 与 `tool_results.jsonl`；
- 配置、manifest、provenance 与 Kernel 配置；
- 精简后的 `events.lifecycle.jsonl`；
- 若进入 Harness，则保存 prediction、patch、Harness summary/result/report 与 run log；
- `source-manifest.json` 固定外部源文件的大小与 SHA-256；
- `files.sha256` 固定仓库内去标识化副本。

未复制工作树和逐 token/update 原始事件流；原始 `events.jsonl` 的哈希、字节数与行数保存在各运行的 `source-manifest.json` 和 `events.source.json` 中。
