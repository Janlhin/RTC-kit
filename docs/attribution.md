# ATTRIBUTION — 来源与许可边界

## 上游项目

| 项 | 值 |
|---|---|
| 名称 | [snarktank/clawchief](https://github.com/snarktank/clawchief) |
| 说明 | Turn your OpenClaw into a Chief of Staff |
| 参考版本 | commit `1fcd1b6`，2026-04-09（此后未更新） |
| 许可状态 | **仓库内没有 `LICENSE` / `NOTICE` / `COPYING`**（已核对 27 个文件） |

## 本仓库与上游的关系

**沿用的**：把优先级判断与自动决策分成两个文件的思路、单一真相源与单写者原则、P0–P3 分级、人/项目四字段模板、`auto-resolver` 的四级判定、实时任务与归档分离、cron 提示词写短而把逻辑放在技能里。这些是架构思想，不是文本表达。

**没有沿用的**：上游的任何英文原文段落；`executive-assistant` / `business-development` 两个技能；470 行 `priority-map` 正文（含大量作者个人标识）；`SETUP-GOG.md` 及所有 Google Workspace 依赖；会议纪要摄入、位置感知等模块。

本仓库的规则与技能均为中文独立撰写。检索上游特有表述（`Canonical sources`、`Auto-resolve now`、`Safe auto-resolve lane`、`Draft and ask`、`principal` 等）在本仓库零命中。

`HEARTBEAT_OK` / `NO_REPLY` 是 OpenClaw 平台的通用哨兵值，不是上游原创表达。

## 关于许可

上游没有附带许可证，按惯例意味着保留所有权利。因此：

- 本仓库**不分发上游任何原文**
- 本仓库的 `LICENSE`（AGPL-3.0）**仅覆盖本仓库原创内容**，不覆盖任何第三方材料
- 如果你打算基于本仓库做二次分发，建议自行向上游确认引用范围，或把同源的目录名 / 技能名改掉，使源码层面完全独立

架构思想与抽象方法不受著作权约束；受保护的是具体表达。本文件的定位是工程溯源说明，不是法律意见。

## 本仓库独立实现的部分

- 闹钟窗口状态机与 ack 语义
- 时刻表与「一天内相位」的声明式表达
- 汇报 / 跟进 / 总结的固定格式
- 删除前检查闸门（抽象自一次真实的文件清理事故）
- `metrics.mjs` 统计记账层
- 三段式任务格式、稳定 ID、错误升级门槛
- `contract/` 下的 JSON Schema 契约

规模对照：上游约 2046 行 Markdown / 27 文件；本仓库规模相近但重心不同——上游处理外部信息（邮件、日历、外联），本仓库处理「提醒可靠性」。

## 外部审计与贡献

| 贡献者 | 范围 | 落点 |
|---|---|---|
| [@nqble](https://github.com/nqble) | 2026-11 外部审计：独立复现审计发现 ①（按 mtime 判陈旧、接管锁会丢写入），并给出「持久 sidecar + 操作系统持锁」的替换方案（Windows `msvcrt.locking` / POSIX `fcntl.flock`），附 Windows 侧六项针对性检查与既有行为测试 | PR [#19](https://github.com/Janlhin/RTC-kit/pull/19)：重写 `_exclusive_file_lock`，删除按 mtime 接管的旧实现 |

审计提交的是**讨论用补丁，不是可直接部署的成品**。本仓库采纳时做了四处改动，与原补丁不同：

1. 补 POSIX inode 守卫：`flock` 锁的是 inode，sidecar 被删除后重建，锁的是旧 inode——必须用 `st_ino` 自检并重试。
2. 重写锁协议测试（8 条）：sidecar 常驻、mtime 再旧也不接管、跨进程互斥、持有者被强杀即刻可获取、并发不丢更新、Windows 持锁期间 sidecar 删不掉、POSIX 侧 inode 守卫。
3. `.gitignore` 忽略 `*.lock`（sidecar 是本机运行时状态，不入库）。
4. 注释与文档统一中文。

**已知边界**：只解决「同一文件的互斥写入」，不覆盖多文件回滚恢复；POSIX 分支沿用审计补丁的结论，未在 real POSIX 宿主上跑过（CI 的 ubuntu 作业覆盖了这部分）；迁移到本协议要求先停掉所有旧写入者，两套锁协议不能同时跑。

