# 文档索引

本目录存放架构、契约、协作、故障处置与审计记录；面向使用者的说明见根目录 [README.md](../README.md)。

## 架构与契约

| 文档 | 用途 | 路径 |
|---|---|---|
| 架构说明 | 分层架构、职责边界与可移植性 | [architecture.md](architecture.md) |
| 业务契约 | plan → due → ack → rollover 的状态转换、输出与退出码 | [behavior-contract.md](behavior-contract.md) |
| 时区模型 | IANA 时区、旧固定偏移回退、DST 与双时区换算口径 | [timezone.md](timezone.md) |
| NLU L0/L1 | 确定性中文时间解析：支持范围、不支持清单、歧义口径与依赖纪律 | [nlu-l0-l1.md](nlu-l0-l1.md) |
| 来源与许可边界 | 与上游项目的关系、许可结论 | [attribution.md](attribution.md) |

## 协作与故障处置

| 文档 | 用途 | 路径 |
|---|---|---|
| 贡献指南 | 怎么改、占位符怎么填、什么不能提交 | [contributing.md](contributing.md) |
| 故障记录与上报（英文） | 本地错误到经批准提交 issue 的流程 | [incident-reporting.md](incident-reporting.md) |
| 故障记录与上报（简体中文） | 同上，中文版 | [incident-reporting.zh-CN.md](incident-reporting.zh-CN.md) |
| 排查指南 | 故障排查、备份与恢复 | [troubleshooting.md](troubleshooting.md) |
| 恢复流程 | 备份与还原步骤 | [RESTORE.md](RESTORE.md) |

## 记录与实测

| 文档 | 用途 | 路径 |
|---|---|---|
| 文档命令实测记录（2026-11-16） | 文档中命令面的实测留痕 | [verification-2026-11-16.md](verification-2026-11-16.md) |
| 审计回应 · 2026-11-09 | 外部审计包的逐条处置记录 | [audit/2026-11-09-response.md](audit/2026-11-09-response.md) |
