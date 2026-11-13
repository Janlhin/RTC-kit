# CONTRIBUTING

## 先读两页

1. `README.md` —— 这是什么
2. `ARCHITECTURE.md` —— 哪层归谁管

改动前先判断你的修改属于哪一层。**最常见的错误是把运行时细节写进 `rules/`**。

---

## 占位符

本仓库所有 `{{...}}` 都是占位符，不是配置错误。填自己的值即可，不要提交填好的版本。

| 占位符 | 含义 | 示例性质 |
|---|---|---|
| `{{TIMEZONE}}` | IANA 时区 | 任意合法时区名 |
| `{{TZ_OFFSET}}` | UTC 偏移 | 如 `+00:00` |
| `{{WAKE_TIME}}` | 第一个窗口起点 | 任意本地时刻 |
| `{{ALARM_WINDOWS}}` | 提醒窗口列表 | 见 `contract/examples/` |
| `{{ROLLCALL_HOURS}}` | 例行播报时点 | 任意小时集合 |
| `{{DELIVERY_CHANNEL}}` | 投递渠道 | 由运行时决定 |
| `{{DELIVERY_TARGET}}` | 投递目标 | 由运行时决定 |
| `{{WORKSPACE_DIR}}` | 工作区目录 | 由运行时决定 |
| `{{AGENT_ID}}` | 目标 agent | 由运行时决定 |
| `{{SERVICE_USER}}` | 运行服务的系统账户 | 由部署决定 |
| `{{LOCAL_USER}}` | 操作者账户 | 由部署决定 |
| `{{MODEL}}` | 模型标识 | 由部署决定 |
| `{{WEEKDAY_HABIT}}` | 每日附带习惯 | 任意短句 |
| `{{SATURDAY_CHORE}}` / `{{SUNDAY_CHORE}}` | 周末专项 | 任意短句 |
| `{{PROJECT_A}}` / `{{PROJECT_B}}` | 项目名 | 任意短句 |
| `{{DATE}}` / `{{DATE_PREV}}` / `{{DATE_COMPACT}}` / `{{DATE_DUE}}` | 示例日期 | 任意日期 |
| `{{TIME_COMPACT}}` | ID 里的 HHMM | 24 小时制四位，如 `0900` |
| `{{WAKE_END}}` / `{{MORNING_TIME}}` / `{{NOON_TIME}}` / `{{EVENING_TIME}}` 等 | 各窗口起止 | 任意本地时刻 |
| `{{ACTIVE_START}}` / `{{ACTIVE_END}}` / `{{REPORT_HOUR}}` / `{{FOLLOW_HOURS}}` / `{{SUMMARY_HOUR}}` | 相位与播报时点 | 任意时刻集合 |
| `{{WINDOW_COUNT}}` / `{{EXPECTED_FIRES}}` / `{{WINDOW_IDS}}` | 窗口数量与标识 | 由你的配置决定 |
| `{{ACK_WORD*}}` / `{{ACK_HINTS_*}}` | 确认关键词 | 任意短词 |
| `{{STATE_DIR}}` / `{{CODE_DIR}}` / `{{RULES_DIR}}` | 运行时目录 | 由部署决定 |
| `{{RUNTIME_NAME}}` / `{{RUNTIME_VERSION}}` / `{{SCRIPT_RUNTIME}}` / `{{SCHEDULER_STORE}}` | 运行时事实 | 由部署决定 |
| `{{LANGUAGE}}` / `{{FORM_OF_ADDRESS}}` / `{{PREFERENCE_*}}` / `{{NEW_TIME}}` | 用户偏好 | 任意值 |

---

## 什么不能提交

- 任何真实姓名、账号、邮箱、时区之外的地理位置
- 任何本机绝对路径（`C:\...`、`/home/<name>/...`）
- 任何 token、密钥、`channel-defaults`、投递目标 ID、chat_id
- 任何运行时数据库（`*.db` / `*.sqlite`）与 `alarms.json` 的真实内容
- 任何真实使用记录、日志、任务内容、错误记录

`.gitignore` 已经挡掉一部分，**但它挡不住你手写进 Markdown 的内容**。提交前跑一遍：

```
grep -rniE 'token|secret|password|api[_-]?key|C:\\Users|/home/' .
```

再人工看一遍 `templates/` 和 `rules/` 里有没有具体人名或项目名。

---

## 改规则的三条约束

1. **声明式**：`rules/` 里不出现路径、平台命令、投递渠道
2. **字段对齐**：改 `rules/` 的字段就要同步改 `contract/*.schema.json`，反之亦然
3. **一次只改一处**：规则、技能、代码是三层，别在同一提交里同时改三层

## 加新功能前先问一句

> 这条改动让用户少做了什么事？

答不上来就别加。历史上另一套同类系统写了 1600 行规则，其中大部分是「规则管理规则」——打卡、分级、自我惩罚、三套自愈巡检，一次事故也没拦住，最后整套被关掉。

---

## 建议的贡献顺序

0. **改 `code/` 或契约前先跑 `python code/tests/test_rtc.py`**，新增行为配一条用例
1. 补 `contract/` 的校验与更多示例
2. 补 `platform/` 下其他运行时的适配示例
3. 把 `code/metrics.mjs` 的查询做得更有用
4. 改进 `rules/` 的措辞，让模型不容易误读

暂时**不要**加：主动关心、情绪判断、优先级打分算法、推测式 nudge。这些应该等基础稳定后由使用者自己长出来。

---

## 提交规范

- 一个提交一件事
- 提交信息用中文或英文均可，但要写清「改了哪一层、为什么」
- 不要提交生成物或本机状态

## 许可

贡献即视为同意以 AGPL-3.0 授权你的原创内容。请勿提交你无权以该许可发布的内容。
