---
name: alarm-clock
description: 闹钟门控。判断「该不该响」由 code/rtc.py 完成，本技能负责确认、改期与解释。也负责接收用户 ack。
---

# Alarm Clock

**判断不归你。** `rtc.py due` 每 5 分钟被定时器调用一次，纯代码判断；命中窗口就打印文案，没命中就什么都不打印。模型只在用户开口时被调用。

如果你的宿主没有 `rtc.py` 的运行环境，见文末「无代码回退」。

## §0 只读一个文件

需要看状态时，只读 `{{STATE_DIR}}/alarms.json`，或直接用 `rtc.py status --json`。

不要读 `tasks.md`、`AGENTS.md`、`MEMORY.md`、`HEARTBEAT.md`。多读一个文件就多一份延迟，也多一次判断错误的可能。

## §1 用户说「起了 / 好了 / 别响了」

先查有没有正在响的窗口：

```
python {{CODE_DIR}}/rtc.py --workspace {{WORKSPACE_DIR}} status --json
```

挑 `state ∈ {pending, ringing}` 且当前时间落在窗口内的那一个，然后：

```
python {{CODE_DIR}}/rtc.py --workspace {{WORKSPACE_DIR}} ack --window <id> --text "<用户原话>"
```

- 退出码 `0` → 回一句短的：`好，记上了。`
- 退出码 `4` → `现在没有在响的提醒，你是说哪一个？`
- 退出码 `2` → 有并发写入，说一句 `刚才有人同时改了状态，我这边没写进去，你再确认一次？`

一次性提醒送达后状态是 `delivered`，同样没有可确认的匹配（退出码 `4`）。

**不要自己改 `alarms.json`。** 写入有 `rev` 校验，手改会被下一次写入判成冲突。

通用短语（「知道了」「别响了」）只在**当前窗口**内算确认；窗口没开时说这些，不要当成 ack。

## §2 改期

v1 的改期**次日生效**：今天的窗口在日切后就已经固定，改它会让已确认的记录失去意义。

1. 改 `rules/routines.md` 里对应窗口的时间（`HH:MM`），并同步 `rules/routines.json`（机器可读的那份）
2. 改窗口长度时，`expectFires` 必须同步：`(end - start) ÷ intervalMin + 1`，否则 `rtc.py plan` 会拒绝
3. 回一句：`改成 13:00 了，明天生效。`

一次性窗口的「改期」= 改 `validFrom` / `validUntil`；窗口本身已经只存在于那一天。

修改时刻表**不需要额外仪式**——它就是一份数据文件，改动在版本控制里可见即可。
但用户若明确要求过「改之前先问我」，那就照做：那是用户的边界，不是契约的规定。

## §3 一次性提醒

**支持。** 用户说「下午三点提醒我一次」时：

> 这个要加进时刻表才会响，加吗？

同意了就加一个**一次性窗口**：

```json
{
  "id": "trip-<日期>",
  "label": "<用户说的事>",
  "start": "15:00", "end": "15:15", "intervalMin": 5, "expectFires": 1,
  "ackHints": ["好了"],
  "oneOff": true,
  "validFrom": "<那一天>", "validUntil": "<那一天>"
}
```

要点：

- `oneOff: true` → 只响一次，**送达即完成**（`state: delivered`），不需要用户确认
- `validFrom` 与 `validUntil` 同日 → 只在这一天存在，第二天不再生成
- `end` 决定容错有多宽：精确时刻的事（会议、航班）用 `start + 15` 分钟；
  「那天做就行」的事（旅游、生日、还款）用 `end` = 当天 `23:45`——**当天任何时候开机都能收到**
- `expectFires` 必须是 `1`，否则契约校验会拒绝

**带截止日的事**（版本限时活动、还款日、截止日之前要交的东西）用 `dueDate`：

```json
{
  "id": "zzz", "label": "<活动名>",
  "start": "20:00", "end": "20:15", "intervalMin": 5, "expectFires": 1,
  "ackHints": ["好了"],
  "dueDate": "<截止日>",
  "validFrom": "<开始提醒的日期>", "validUntil": "<截止日>"
}
```

`rtc.py` 会自动把剩余天数写进文案（「距截止还有 2 天」「明天截止」「最后一天！」）——
**不要自己算天数，也不要自己写「截止 X/X」**，那是代码的事。

**不要自己建定时任务**，也不要往 `alarms.json` 里手写窗口。

## §4 响的时候说什么

文案由 `rtc.py` 生成，格式固定：

```
☀️ 起床 —— 08:10（第 3 次）
洗澡刷牙，有条件洗个头。
回复「起了」就不响了。
```

规律：第 1 次带 `ackHint`，第 2 次起标「第 N 次」，每隔一次重提一次怎么停。不加强度、不加感叹号、不解释系统原理、整段 2–3 行。

**改文案请改 `rtc.py`，不要在这里加措辞规则**——否则两个地方各说一套。

## §4.1 带清单的提醒

窗口带 `checklist` 就是一份要逐项打勾的单子。到点提醒会把整张单子发给你，并标出还剩几项。

```json
{
  "id": "pack",
  "label": "清点并带走",
  "start": "20:00", "end": "20:15", "intervalMin": 5, "expectFires": 1,
  "ackHints": ["好了"],
  "checklist": ["电饭煲", "电脑", "被子枕头", "牙膏"]
}
```

用户说「电饭煲拿到了」时：

```
python {{CODE_DIR}}/rtc.py --workspace {{WORKSPACE_DIR}} check --window pack --item "电饭煲"
```

- 退出码 `0` → 简短确认：`打勾了，还剩 3 项`
- 退出码 `4` → `清单里没有这一项，你说的是哪个？`

`--item` 可以给名称或序号。**不要自己改 `alarms.json`**——勾选状态由 `check` 维护。
清单全部打完会自动置为完成，之后不再提醒。

## §5 静默

不响的时候，`rtc.py due` 不输出任何内容。宿主把「空输出」当作不投递。

> 如果宿主用哨兵字符串表示静默（例如 `NO_REPLY`），**该响的分支里绝不能出现这个字符串**。它一旦出现在该响的分支，任务照跑、计数照涨、用户什么都收不到，且没有任何报错。

## §6 写状态

只有 `rtc.py` 能写 `alarms.json`，且只写它负责的字段：

| 字段 | 谁写 |
|---|---|
| `state` / `fireCount` / `lastFiredAt` / `rev` | `rtc.py due` |
| `ackedAt` / `ackText` / `state` / `rev` | `rtc.py ack` |
| `planDate` / `planKind` / `windows` / `history` / `rev` | `rtc.py plan` |

写之前读 `rev`、写入后 `rev + 1`；发现 `rev` 已变则放弃写入、记一条 `concurrent_write`、不重试不合并。

## §7 禁止

- ❌ 自己判断「现在该不该响」
- ❌ 新建、删除、修改定时任务（条数必须恒定）
- ❌ 改 `tasks.md`、`rules/routines.md`、`rules/priority-map.md`
- ❌ 读到文件不一致就「顺手修好」
- ❌ 连续响超过 `expectFires` 次（到上限就判 missed）
- ❌ 在非窗口时间发任何消息

## 无代码回退

宿主没有 Python 或不允许跑脚本时，本技能可以退化为「模型门控」：读 `alarms.json`，按上面 §1/§4 的规则自行判断与措辞。

**这是降级路径，不是默认路径。** 模型判断会带来两类确定性代价：同样的输入不保证同样的输出；静默标记一旦写错分支，该响不响且无报错。`ARCHITECTURE.md` 第四节记录了这条教训。
