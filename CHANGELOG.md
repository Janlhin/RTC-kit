# 变更记录

本项目所有值得记录的变更都在此登记。格式遵循
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循
[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- 2026-11-28：`cancel --undo` 支持撤销当天的误取消：按审计流里的取消前快照逐字段恢复今天账本实例，当天有效；跨日 / 无痕迹 / 永久停用窗口拒绝复活，写入走既有 rev 乐观锁（FR-3.4）。
- 2026-11-26：引入本仓治理骨架（安全政策、变更记录、文档索引、PR 模板与 Dependabot 配置）；既有 `tests` 工作流与校验脚本保持不变。
