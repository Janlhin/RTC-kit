# TOOLS.example.md — 本机事实

> 环境专属的东西放这里。规则是通用的，这台机器是具体的。
> 复制为 `TOOLS.md` 后填写。

## 运行时

| 项 | 值 |
|---|---|
| 平台 / 版本 | `{{RUNTIME_NAME}}` `{{RUNTIME_VERSION}}` |
| 工作区 | `{{WORKSPACE_DIR}}` |
| 配置文件 | `{{CONFIG_PATH}}` |
| 调度状态存储 | `{{SCHEDULER_STORE}}` |
| 可用 node/python | `{{SCRIPT_RUNTIME}}` |

## 投递渠道

| 项 | 值 |
|---|---|
| 渠道 | `{{DELIVERY_CHANNEL}}` |
| 目标 | `{{DELIVERY_TARGET}}` |
| 是否允许主动推送 | `{{PROACTIVE_DELIVERY}}` |

> 若平台要求先登记投递目标才能创建推送任务，先完成登记再建任务。
> **禁止创建没有 `to` 的推送任务**——它会投递失败，且你可能等很久才发现。

## 调度器的硬约束

按你的运行时填。以下是常见坑，逐条确认：

- 触发器的「会话目标」与「载荷类型」是否需要配对？配错是否静默失效？
- 是否需要显式时区？系统时区是否可信？
- 创建后如何核对下次运行时间？
- 静默返回值是什么？出现在该响的分支里会不会被吞掉？
- 任务数量是否允许被自动重建？（建议禁止）

## 时间与编码

- 时区 `{{TIMEZONE}}`，时间戳一律写全 `YYYY-MM-DD HH:MM {{TZ_OFFSET}}`
- 文本文件统一 UTF-8 无 BOM
- 脚本里避免 emoji（部分终端默认编码会报错），用 `[OK]` / `[ERROR]`

## 路径速查

| 用途 | 路径 |
|---|---|
| 工作区 | `{{WORKSPACE_DIR}}` |
| 状态账本 | `{{STATE_DIR}}/alarms.json` |
| 统计库 | `{{STATE_DIR}}/metrics.db` |
| 日记 | `{{WORKSPACE_DIR}}/memory/YYYY-MM-DD.md` |
