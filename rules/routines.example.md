# routines.example.md — 时刻表模板

> **全系统唯一的「何时响」真相源。** 机器可读版本见 `contract/routine.schema.json` 与 `contract/examples/routines.example.json`。
> 所有 `{{...}}` 都要换成你自己的值。本文件不出现任何路径或平台命令。

时区：`{{TIMEZONE}}`（`{{TZ_OFFSET}}`）。所有时间按本地时间理解。

---

## 一、日期类型

| 类型 | 判定 |
|---|---|
| `workday` | 周一到周五，且不在节假日覆盖表里 |
| `weekend` | 周六、周日，且不在覆盖表里 |
| `holiday` | 出现在下面的覆盖表里的日期 |

优先级：覆盖表 > 星期几。**覆盖表手工维护，不联网抓取**——网络失败会造成静默漏响。

```
YYYY-MM-DD holiday   说明
```

---

## 二、闹钟窗口

| id | 名称 | 窗口 | 间隔 | 次数 | ack 关键词 |
|---|---|---|---|---|---|
| `{{WINDOW_A_ID}}` | 起床 | `{{WAKE_TIME}}` – `{{WAKE_END}}` | 5 分钟 | 6 | {{ACK_HINTS_A}} |
| `meds-am` | 例行事项 A | `{{MORNING_TIME}}` – `{{MORNING_END}}` | 5 分钟 | 6 | 好了 / 办了 |
| `meds-noon` | 例行事项 B | `{{NOON_TIME}}` – `{{NOON_END}}` | 5 分钟 | 6 | 好了 / 办了 |
| `meds-pm` | 例行事项 C | `{{EVENING_TIME}}` – `{{EVENING_END}}` | 5 分钟 | 6 | 好了 / 办了 |

规则：

- 窗口是闭区间，末次也算
- 到 `end` 仍未确认 → 判 `missed`，不补响、不顺延
- 第二次及以后的响铃开头标「第 N 次」
- **确认（`ack`）是一个状态字段，不是「删掉剩余几条任务」**

开关：`weekendAlarms: true` / `holidayAlarms: true`（false = 该日期类型不响闹钟）。

这两个开关**已被 `rtc.py` 消费**：置为 `false` 的日期类型是**合法空日**——静默、不报配置错误，但照常落盘 state 与心跳。

---

## 三、例行播报

cron 只能给一套触发表达式，**日期类型差异由技能按「星期 × 小时」门控**。

| 时点 | 工作日 | 周末 / 节假日 |
|---|---|---|
| `{{REPORT_HOUR}}` | 汇报 | 静默 |
| `{{FOLLOW_HOURS}}` | 跟进 | 静默 |
| `{{WEEKEND_REPORT_HOUR}}` | 跟进 | **汇报** |
| `{{WEEKEND_FOLLOW_HOUR}}` | 跟进 | 跟进 |
| `{{SUMMARY_HOUR}}` | 总结（今天 + 明日预告） | 总结 |

数据源口径：汇报 = 今天 + 昨天；跟进 = 今天；总结 = 今天 + 明日预告。

周末专项挂在周末汇报末尾各一句，不单独发消息：周六 `{{SATURDAY_CHORE}}`，周日 `{{SUNDAY_CHORE}}`。

开关：`weekdayHabit: false`（播报末尾是否附带 `{{WEEKDAY_HABIT}}`）。

---

## 四、一天里的相位

约束的是**主动发起**的消息；用户问话时不受限制。

| 相位 | 时段 | 能否主动发消息 |
|---|---|---|
| 活跃 | `{{ACTIVE_START}}` – `{{ACTIVE_END}}` | 可以 |
| 收尾 | `{{SUMMARY_HOUR}}` – `{{ACTIVE_END}}` | 只有总结可以 |
| 安静 | `{{ACTIVE_END}}` – `{{ACTIVE_START}}` | 不许（P0 例外） |

判定方式：**看当前时间**，不用记状态，因为表是固定的。

没做完的事留到第二天汇报里说，不要在深夜补发。

---

## 五、日切生成规则

1. 判今天的日期类型 → 决定窗口集与播报集
2. 生成窗口，`start` / `end` 补齐年月日与 `{{TZ_OFFSET}}`
3. `expectFires` = 窗口分钟数 ÷ 间隔 + 1
4. `planDate` = 今天，`rev` + 1
5. 昨天遗留的 `pending` / `ringing` 判 `missed`，压一行进 `history`（保留 14 天）
6. 校验窗口数与 `expectFires` 合计，不符就别写，记一条错误

**幂等**：`planDate` 已是今天且由日切生成过，直接跳过。

---

## 六、有效期与一次性提醒

窗口可以带有效期，也可以是一次性的。

| 字段 | 含义 |
|---|---|
| `validFrom` | 该窗口从哪一天开始生效（含）。省略 = 无限期 |
| `validUntil` | 该窗口到哪一天失效（含）。省略 = 无限期 |
| `oneOff` | `true` = 一次性提醒：只响一次，送达即完成（`state: delivered`），不需要确认 |

### 带截止日的周期任务（版本限时）

```
{
  "id": "season-event",
  "label": "版本限时任务",
  "start": "10:00", "end": "10:25", "intervalMin": 5, "expectFires": 6,
  "validFrom": "2026-11-10", "validUntil": "2026-12-31"
}
```

有效期内每天照常响；超出 `validUntil` 就不再生成窗口。

### 一次性提醒（只响一次）

```
{
  "id": "trip-shenzhen",
  "label": "深圳行程",
  "start": "09:00", "end": "09:15", "intervalMin": 5, "expectFires": 1,
  "ackHints": ["好了"],
  "oneOff": true,
  "validFrom": "2026-09-30", "validUntil": "2026-09-30"
}
```

- `oneOff: true` 时 `expectFires` 必须是 `1`（契约强制）
- `validFrom` 与 `validUntil` 同日 = 只在这一天出现
- 响过一次后状态是 `delivered`，不再等确认；第二天该窗口不再生成

**`end` 决定容错有多宽**，按提醒的性质选：

| 提醒性质 | 写法 | 错过 `start` 后 |
|---|---|---|
| 精确时刻（会议、航班） | `end` = `start` + 15 分钟 | 15 分钟内还能收到，之后静默跳过 |
| 当天提醒（旅游、生日、还款） | `end` = 当天 `23:45` | **当天任何时候开机都能收到** |

一次性提醒只响一次，错过了没有第二次——所以凡是「那天做就行」的事，都把 `end` 拉到当天末尾。

### 倒计时提醒（带截止日的事）

窗口加 `dueDate` 就是倒计时：**文案自动带剩余天数，每天提醒一次，直到截止日当天。**

```
{
  "id": "zzz",
  "label": "限时活动",
  "start": "20:00", "end": "20:15", "intervalMin": 5, "expectFires": 1,
  "ackHints": ["好了"],
  "dueDate": "2026-05-10",
  "validFrom": "2026-05-08", "validUntil": "2026-05-10"
}
```

文案（**天数由代码算，不由模型判断**）：

| 距离截止 | 文案 |
|---|---|
| 2 天以上 | `限时活动 —— 20:00（距截止还有 2 天）` |
| 1 天 | `限时活动 —— 20:00（明天截止）` |
| 当天 | `限时活动 —— 20:00（最后一天！）` |

规则：

- 给了 `dueDate` 的窗口，**每天只提醒一次**（`expectFires` 固定为 1）
- 从 `validFrom` 到 `validUntil` 每天重新提醒；**昨天确认过不代表事情做完了**，次日仍提醒
- 出了 `validUntil` 就不再生成窗口
- `validFrom` 可以留出提前量：距截止还有 2 天时就开始提醒

### 带清单的提醒（逐项打勾）

窗口加 `checklist` 就是一份要逐项打勾的单子。到点提醒会把整张单子发给你，并标出还剩几项。

```
{
  "id": "pack",
  "label": "清点并带走",
  "start": "20:00", "end": "20:15", "intervalMin": 5, "expectFires": 1,
  "ackHints": ["好了"],
  "checklist": ["电饭煲", "电脑", "被子枕头", "牙膏"]
}
```

到点提醒长这样：

```
清点并带走 —— 20:00（还剩 4 项）
☐ 电饭煲 ☐ 电脑 ☐ 被子枕头 ☐ 牙膏
逐项打勾说「电饭煲拿到了」；全部拿到说「好了」
```

逐项打勾：

```bash
python rtc.py --workspace <dir> check --window pack --item "电饭煲"   # 按名称
python rtc.py --workspace <dir> check --window pack --item 2          # 按序号
python rtc.py --workspace <dir> check --window pack --item "电饭煲" --undo
```

规则：

- 清单窗口**每天只提醒一次**（`expectFires` 固定为 1）
- **勾选状态跨天延续**——昨天打了 3 项，今天还剩 15 项，清单不因日切重置
- 全部打完 → 窗口自动置为 `acked`，之后不再提醒
- `check` 的 **stdout 保持为空**：它由用户交互触发，不是定时任务，不该被投递出去；
  结果看退出码（0 成功），细节在 stderr

### 空日的两种含义

| 情况 | 行为 |
|---|---|
| 有效期内没有窗口（还没开始 / 已结束） | **合法空日**：静默，照常落盘 state |
| `weekendAlarms` / `holidayAlarms` 为 `false` | **合法空日**：静默 |
| 有窗口在有效期内，却被 `dateTypes` 全部排除 | **配置错误**：退出码 `3`，提示检查 `dateTypes` |
