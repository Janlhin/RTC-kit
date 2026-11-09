---
name: alarm-clock
description: 闹钟门控。每次被 cron 唤醒时判断当前该不该响：读 state/alarms.json，三个条件全真才响，否则静默。也负责接收用户 ack。
---

# Alarm Clock

被 cron 每 5 分钟唤醒一次（`isolated` 会话）。**只做一件事：判断这一分钟该不该响。**

## §0 只读一个文件

醒来第一件事，也是唯一必读：`{{STATE_DIR}}/alarms.json`。

不要读 `tasks.md`、`AGENTS.md`、`MEMORY.md`、`HEARTBEAT.md`。多读一个文件就多一份延迟，也多一次判断错误的可能。

## §1 先处理日期

读 `planDate`：

- **等于今天** → 进 §3
- **不等于今天**（昨天、更早、或文件不存在/损坏）→ 进 §2 补建

判断"今天"用 `TOOLS.md` 里的时区（`{{TZ_OFFSET}}`）。不要用 UTC。

## §2 补建（只增不改）

按 `rules/routines.example.md` 的日切规则生成今天的窗口表：

1. 判今天日期类型（workday / weekend / holiday）
2. 生成 {{WINDOW_COUNT}} 个窗口：`{{WINDOW_IDS}}`
3. 已经过去的窗口 → `state: "skipped"`，`missedReason: "late_plan"`，`fireCount: 0`
4. 还没到的窗口 → `state: "pending"`
5. `planDate` 换今天、`planKind` 换类型、`rev + 1`、`generatedBy: "alarm-clock:catchup"`
6. 昨天以前遗留的 `pending`/`ringing` → 压进 `history`，`state: "missed"`，`reason: day_rollover`（`history` 只留 14 天）
7. 往 `{{WORKSPACE_DIR}}/errors.md` 追加一条：`prep 没跑，alarm-clock 补建了 YYYY-MM-DD 的窗口`

补建完继续往下判：如果当前时间正好落在某个窗口内，正常响。

**补建是唯一允许的"跨窗口写操作"，且只能新增，绝不修改已有窗口。**

## §3 三个判断条件

找到 `now ∈ [start, end]` 的窗口（闭区间，含 `end` 那一分钟）。可能命中的只有一个——窗口之间不重叠。

**全部为真才响：**

1. 命中了某个窗口，且 `state ∈ {pending, ringing}`
2. `fireCount < expectFires`
3. `lastFiredAt` 为空，或 `now - lastFiredAt ≥ intervalMin 分钟 - 60 秒`

**有一个为假就不响。**

另外两种情况必须先改状态、然后静默：

| 情况 | 动作 |
|---|---|
| `now > end` 且 `state ∈ {pending, ringing}` | 改 `state: "missed"`，`missedReason: "window_expired"`，静默 |
| `state ∈ {acked, missed, skipped}` | 什么都不写，静默 |

## §4 响的时候说什么

先算这是第几次：`fireCount + 1`。

```
☀️ 起床 —— 08:10（第 3 次）
洗澡刷牙，有条件洗个头。
回复「{{ACK_WORD}}」就不响了。
```

规则：

- **第 1 次**：必须带 `ackHint` 那句（`回复「{{ACK_WORD}}」就不响了`）
- **第 2 次起**：开头加 `（第 N 次）`，`ackHint` 可以省，但**每隔一次要再提一次**怎么停
- 不增加语气强度，不加感叹号，不写"已经提醒你很多次了"
- 不解释系统原理，不写"这是自动提醒"
- 整段控制在 2–3 行

**响铃分支里绝对不许出现 `NO_REPLY`、`HEARTBEAT_OK` 这两个词。** 出现一次这个提醒就没了。

## §5 静默的时候说什么

只输出四个字符：

```
NO_REPLY
```

不加解释、不加句号、不加"（今天已确认）"。平台会把 `NO_REPLY` 当作"无需投递"吞掉——这正是我们要的。

> 这是整个系统最危险的机制：`NO_REPLY` 一旦出现在该响的分支里，任务照跑、`runCount` 照涨、`state` 一切正常，但你什么都收不到，且没有任何报错。
> 所以：**响 = 绝不静默标记；不响 = 只有 NO_REPLY 四个字。**

## §6 写状态（严格按字段所有权）

响完之后写回 `{{STATE_DIR}}/alarms.json`，**只写这四个字段**：

| 字段 | 写什么 |
|---|---|
| `state` | `"ringing"`（若是从 pending 来的）或保持 `"ringing"` |
| `fireCount` | 原值 + 1 |
| `lastFiredAt` | 现在的时间（带 `{{TZ_OFFSET}}`） |
| `rev` | 原值 + 1 |

**你绝对不能写**：`ackedAt`、`ackText`、`start`、`end`、`expectFires`、`planDate`、`planKind`、其他窗口、`history`。

写之前先读一次 `rev` 并记住；写入后 `rev` 必须是你读到的值 + 1。如果发现文件里的 `rev` 已经变了（说明有人同时改了），**放弃写入，往 `errors.md` 记一条 `concurrent_write`，然后静默**。不要重试，不要合并。

## §7 收 ack（主会话里触发，不是 cron）

用户在正常对话里说「起了」「吃了」「别响了」「知道了」，就是 ack：

1. 找 `now ≤ end + 15 分钟` 的未确认窗口（`state ∈ {pending, ringing}`）
2. 找到 → 写 `ackedAt`、`ackText`（用户的原话）、`state: "acked"`、`rev + 1`
3. 回一句短的：`好，这顿记上了。`
4. 找不到匹配窗口 → 问一句：`现在没有在响的提醒，你是说哪一顿？`
5. 如果 `now > end + 15 分钟` → **不要改 state**（它可能已经被判 missed），只回：`这个窗口已经过了，记下了。` 并往 `errors.md` 记一条迟到的 ack

**ack 只写 `{{STATE_DIR}}/alarms.json`，绝不写 `tasks.md`。** 提醒和任务是两回事，不要顺手帮我勾任务。

## §8 禁止

- ❌ 新建、删除、修改 cron（cron 条数必须恒定）
- ❌ 改 `tasks.md`、`rules/routines.example.md`、`rules/priority-map.md`
- ❌ 读到文件不一致就"顺手修好"
- ❌ 连续响超过 `expectFires` 次（到上限就判 missed）
- ❌ 在非窗口时间发任何消息
- ❌ 一次唤醒里处理多个窗口（只处理命中的那一个）
