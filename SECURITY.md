# 安全政策

## 威胁范围

RTC-kit 是一套本地规则包与确定性 Python 脚本：Markdown 规则、技能说明、JSON Schema 与 `code/` 下的脚本。它本身不是常驻服务，不联网、不收集账号信息；提醒投递取决于宿主运行时，本仓只给适配示例。

脚本在本地读写工作区数据（时刻表、状态、历史库、备份），相关风险面：本地文件读写、占位符被填成真实敏感信息后误提交、备份落点。

## 私密报告

如果发现可能危及使用者本机数据的问题（例如脚本越权读写路径、可被构造的时刻表注入、备份泄露），请不要公开开 issue。可通过仓库 owner 的 GitHub 主页私信，或使用仓库的 GitHub Security Advisories 私密渠道联系维护者，并附上：

- 受影响版本 / commit；
- 最小复现路径（临时工作区、命令序列）；
- 影响（可能读到 / 改动什么）；
- 建议的修复方式。

故障从本地错误到上报 issue 的完整流程见 [docs/incident-reporting.md](docs/incident-reporting.md)（[简体中文](docs/incident-reporting.zh-CN.md)）。

## 已知缺口

- 投递渠道与目标由宿主决定；本仓不校验宿主侧的推送链路与凭据保管。
- `code/metrics.mjs` 是可选旁路统计，不随主链路维护。
- 规则包是模板：填好的个人作息、路径、投递目标属于运行时数据，不应入库。

## 安全默认

- 仓库是干净模板，个人信息、真实作息、本机路径、账号与投递目标均为 `{{占位符}}`。
- 备份目标盘由 `rules/backup.json` 指定；恢复办法见 [docs/troubleshooting.md](docs/troubleshooting.md) 与 [docs/RESTORE.md](docs/RESTORE.md)。
- 本仓不提交凭据、令牌、内网地址或个人联系方式。
