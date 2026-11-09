---
name: daily-routine
description: 例行播报。被 cron 在每个整点唤醒时，按"星期×小时"判断该发汇报/跟进/总结还是静默，并按固定格式输出。
---

# Daily Routine

被 cron 唤醒（`isolated` 会话），时间点是 `{{ROLLCALL_HOURS}}` 点整（见 `rules/routines.example.md`）。

**同一套 cron 覆盖工作日和周末，差异全部靠门控判断。**

## §1 先判该不该说话

1. 判今天的日期类型：读 `rules/routines.example.md` 的日期类型与覆盖表 → `workday` / `weekend` / `holiday`
2. 查 `rules/routines.example.md` 的播报表，看**当前小时**有没有条目
3. 没有 → 输出 `NO_REPLY` 四个字，结束

会静默的组合（记牢，别自作主张补发）：

| 时间 | 周末/节假日 |
|---|---|
| `{{REPORT_HOUR}}` | 静默 |
| `{{FOLLOW_HOUR_1}}` | 静默 |
| `{{FOLLOW_HOUR_2}}` | 静默 |
| `{{FOLLOW_HOUR_3}}` | 静默 |

> 工作日与周末在同一时点可以有不同的播报类型，具体映射见 `rules/routines.example.md` 的表格。

## §2 再取数据

| 类型 | 数据源 |
|---|---|
| 汇报 | `tasks.md` 的 Today（今天）+ `tasks-completed.md` 最近一天（昨天） |
| 跟进 | `tasks.md` 的 Today，只看未完成 |
| 总结 | `tasks.md` 的 Today（做完/没做）+ 明日预告（`待安排（有日期）` 里明天到期的、以及明天是周六/周日的专项） |

**只读这两个文件 + `{{STATE_DIR}}/alarms.json`（仅 20:00 用）。** 不要去翻 `memory/`、`errors.md`，除非用户问。

## §3 按格式输出

格式在 `rules/formats.md`，照抄。要点：

- 空字段写「无」，**不删行、不编内容**
- 不写"以下是你的今日待办"这类引导句
- 不加表情包、不加鼓励语
- 条数多的，列前 5 条 + `…还有 N 条`

**看看有没有可执行动作**：如果某条任务明显卡住了（连续 3 天没动、或截止日已过），在`📝 备注`里点一句，不要说教。

## §4 20:00 额外做三件事

**第一件：落账（只读旁路，失败不影响输出）**

```
node {{CODE_DIR}}/metrics.mjs sync
```

成功会返回一行 `metrics: 写入 N 行…`。**如果这条命令报错，跳过，继续往下**——它只是记账，不参与提醒。别重试，别修它。

工作目录不是工作区根时，先 `cd` 过去再加 `node {{CODE_DIR}}/metrics.mjs sync`。

**第二件：自检行**

读 `state/alarms.json`，在总结末尾加一行——

```
今日闹钟：{{WINDOW_COUNT}} 个窗口，响 18 次，确认 3 / 漏 1 / 未到 0
```

算法：

- 窗口数 = `windows` 长度
- 响次数 = 所有 `fireCount` 之和
- 确认 = `state == "acked"` 的个数
- 漏 = `state == "missed"` 的个数
- 未到 = `state ∈ {pending, skipped}` 的个数

如果窗口数 ≠ 4，或 `planDate` 不是今天，在这行后面补一句 `⚠️ 窗口表异常`，并往 `errors.md` 记一条。**但不要在 20:00 去修它。**

**第三件：周日再加三行本周复盘**（格式见 `rules/formats.md`）：完成 / 顺延 / 反复没动的 ≥2 次的，最多各 3 条，最后一行给下周优先。数据从 `tasks-completed.md` 最近 7 天 + `tasks.md` 里连续多天没动的项来。

## §5 周末专项

周六 14:00 汇报的末尾加一句{{SATURDAY_CHORE}}；周日 14:00 汇报的末尾加一句 {{SUNDAY_CHORE}}。各一句，不加感叹号。

## §6 游戏签到（开关见 routines.md 第五节）

`game_checkin: true` 且今天是工作日时，每次播报末尾加一行：

`🎮 顺带：{{WEEKDAY_HABIT}}`

周末不加。

## §7 禁止

- ❌ 改任何任务状态（你可能看到任务该勾了，但不许勾——勾任务由主会话或 `daily-task-manager` 做）
- ❌ 新建/删除 cron
- ❌ 在静默时点"补发"上一档
- ❌ 自创格式
- ❌ 把三条播报合并成一条（用户要的是节奏，不是效率）
