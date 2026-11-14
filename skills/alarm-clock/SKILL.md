---
name: alarm-clock
description: 闹钟门控。判断「该不该响」由 code/rtc.py 完成，本技能负责确认、改期与解释。也负责接收用户 ack。
---

# Alarm Clock

**判断不归你。** `rtc.py due` 每 5 分钟被定时器调用一次，纯代码判断；命中窗口就打印文案，没命中就什么都不打印。模型只在用户开口时被调用。

如果你的宿主没有 `rtc.py` 的运行环境，见文末「无代码回退」。

## §0.1 你不是调度器，系统已经就位

RTC 是「判断 + 写入」与「定时触发」分离的两层：

- **定时层（调度器）**：宿主的 cron 跑两条 no-agent 脚本——`rtc-plan`（每天重新生成当天时间表）和 `rtc-due`（每 5 分钟检查一次、命中窗口就打印文案并投递）。它们**不调模型**，是真正让提醒响起来的部分。
- **你（模型）是配置层**：只在用户开口时被调用，把「改 / 加提醒」翻译成对 `{{CODE_DIR}}/rtc.py` 的子命令调用，写完回一句人话。

因此：

- ❌ 不要告诉用户「RTC 不存在 / 需要搭建 / 需要设置 cron / 需要部署 rtc.py」——部署是安装器（`install.py`）做的事，运行时系统已经跑着了。
- ❌ 不要自己动手建 / 删 / 暂停 cron 任务（条数恒定：只有 `rtc-plan` 和 `rtc-due` 两条）。
- ❌ 不要手改 `{{WORKSPACE_DIR}}/rules/routines.json` 或任何 `.example` / `.md` 模板。
- 用户收到的每一条提醒都是上面这套系统的输出——你看到的定时投递消息就是它的产物，不是外部事件。


## §0 只读一个文件

需要看状态时，只读 `{{STATE_DIR}}/alarms.json`，或直接用 `rtc.py status --json`。

不要读 `tasks.md`、`AGENTS.md`、`MEMORY.md`、`HEARTBEAT.md`。多读一个文件就多一份延迟，也多一次判断错误的可能。

## §0.5 唯一可改的文件

只有 `<workspace>/rules/routines.json` 是运行时配置。**绝不**打开或手改任何带 `.example`、`.md` 后缀的文件（如 `rules/routines.example.md`、`rules/routines.md`）——那些是模板与文档，改了不会生效，还会让你误以为改好了。本技能是 `rules/routines.json` 的**唯一权威**：其他日常类技能（daily-routine 等）不修改时刻表，遇到改时间 / 改期请回到本技能走命令。所有改动只通过 `rtc.py` 的子命令完成。

## §1 用户说「起了 / 好了 / 别响了」—— 你是唯一的 ack 桥

cron（`rtc-due`）是 no-agent 脚本，**看不到聊天、也不会处理回复**。所以用户回复「起了 / 好了 / 办了 / 别响了 / 知道了」或任何「停 / 不响了 / 做完了」的意思时，**几乎一定是在回某条 RTC 提醒**——这是你（模型）必须做的动作，不是新请求：

1. 立刻调用 ack。**可以省略 `--window`**，命令会自动挑当前在响 / 刚过期（当天）的窗口：

```
python {{CODE_DIR}}/rtc.py --workspace {{WORKSPACE_DIR}} ack --text "<用户原话>"
```

2. 退出码 `0` → 回一句短的：`好，记上了。`（**不要**说「已设置完成」「已完成」「配好了」——那会误导用户以为系统被重配了。）
3. 退出码 `4` → `现在没有在响的提醒，你是说哪一个？`
4. 退出码 `2` → `刚才状态被人同时改了，你再确认一次？`

- ⚠️ 你看到的「Cronjob Response: rtc-due … 回复「好了」就不响了」**就是 RTC 在响**；用户那条回复就是要 ack 它。别把它当外部事件、别去「搭建/部署 RTC」。
- ⚠️ 当天窗口即便刚过期（状态 `missed`），同日内回复仍能 ack（宽限已放宽），所以晚一点回「好了」也有效。
- **不要自己改 `alarms.json`。** 写入有 `rev` 校验，手改会被下一次写入判成冲突。
- 通用短语（「知道了」「别响了」）只在**当前窗口**内算确认；窗口没开时说这些，不要当成 ack。

## §2 改期

v1 的改期**次日生效**：今天的窗口在日切后就已经固定，改它会让已确认的记录失去意义。

1. 改**时刻**用 `set-time` 命令（不要手改 JSON）：
   `python {{CODE_DIR}}/rtc.py --workspace {{WORKSPACE_DIR}} set-time --window <id> --start HH:MM [--end HH:MM]`
2. 回一句：`改成 13:00 了，明天生效。`
   `set-time` 只写时刻表，下次 `plan` 生效；今天已生成的窗口不变。

**改期（挪到别的日子）用 `postpone` 命令，不要手改 JSON：**

```bash
python {{CODE_DIR}}/rtc.py --workspace {{WORKSPACE_DIR}} postpone --window <id> --to YYYY-MM-DD
python {{CODE_DIR}}/rtc.py --workspace {{WORKSPACE_DIR}} postpone --window <id> --days 3
```

- 时刻不变——推迟到明天就是明天同一时间响
- 今天那次不会再响（记为 `skipped`），明天自动重新创建
- 一次性窗口推迟 = 挪 `validFrom` / `validUntil`；倒计时窗口推迟 = 挪截止日
- 周期窗口推迟 = 跳过这几天，之后照常
- 退出码 `0` → 回一句 `推到明天了，还是那个点。`
- 退出码 `3` → 配置有冲突，说一句 `这个日子它不响，换一天？`
- 退出码 `4` → `没有这个提醒项。`

**用户说「推迟」但没给日期时，先问一句「推到哪天？」**——别猜。

### 取消 / 完成 / 永不提醒

三件事，三条命令，**别混用**：

```bash
python {{CODE_DIR}}/rtc.py --workspace {{WORKSPACE_DIR}} cancel   --window <id>
python {{CODE_DIR}}/rtc.py --workspace {{WORKSPACE_DIR}} complete --window <id> --text "<用户原话>"
python {{CODE_DIR}}/rtc.py --workspace {{WORKSPACE_DIR}} disable  --window <id>
python {{CODE_DIR}}/rtc.py --workspace {{WORKSPACE_DIR}} enable   --window <id>
```

| 用户说 | 用哪条 | 回一句 |
|---|---|---|
| 「那个不去了」 | `cancel` | `好，那次取消了。` |
| 「这件事做完了」 | `complete` | `好，完成了，以后不提醒了。` |
| 「以后都别提醒我了」 | `disable` | `好，以后不提醒了。` |
| 「又想起来了」 | `enable` | `好，重新开起来了。` |

区别：

- `cancel` 取消**一个安排**。周期窗口（每天都响的）只能取消今天这一次——
  它没有「这件事」可取消。一次性 / 倒计时 / 有期限的安排则是彻底取消。
- `complete` = 完成 + 永久停用。`ack` 只停今天。
- `disable` 停用**一类**提醒；`enable` 可以撤销。

退出码：`0` 成功 · `3` 参数不合法 · `4` 没有这个窗口（或那天不存在这个窗口）。

**这些命令的 stdout 是空的**——不要把它们当提醒投递出去。结果看退出码，细节在 stderr。

一次性窗口的「改期」= 改 `validFrom` / `validUntil`；窗口本身已经只存在于那一天。

修改时刻表**只能由 `rtc.py` 的命令完成**（`set-time` / `postpone` / `disable` / `enable`）。`rules/routines.json` 是唯一可改的文件，**模型绝不手改**它，也不碰任何 `*.example` / `*.md` 模板。
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

> 提示：Hermes 部署里已有便捷脚本 `rtc_once.py`——
> `python {{CODE_DIR}}/rtc_once.py HH:MM "内容" [--date YYYY-MM-DD]`
> 会自动追加 `oneOff` 窗口并跑 `plan --force`，比手改 JSON 安全。

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
- ❌ 手改 `rules/routines.json` 里的日期（推迟用 `postpone` 命令）
- ❌ 手改 `rules/routines.json` 里的 `disabled`（停用用 `disable` 命令）
- ❌ 新建、删除、修改定时任务（条数必须恒定）
- ❌ 改 `tasks.md`、`rules/routines.md`、`rules/priority-map.md`
- ❌ 打开或手改任何名字带 `.example` / `.md` 的文件（`rules/routines.example.md`、`rules/routines.md` 都是模板，不是运行配置）
- ❌ 读到文件不一致就「顺手修好」
- ❌ 连续响超过 `expectFires` 次（到上限就判 missed）
- ❌ 在非窗口时间发任何消息

## 无代码回退

宿主没有 Python 或不允许跑脚本时，本技能可以退化为「模型门控」：读 `alarms.json`，按上面 §1/§4 的规则自行判断与措辞。

**这是降级路径，不是默认路径。** 模型判断会带来两类确定性代价：同样的输入不保证同样的输出；静默标记一旦写错分支，该响不响且无报错。`ARCHITECTURE.md` 第四节记录了这条教训。
