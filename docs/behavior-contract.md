# 业务契约：plan → due → ack → rollover

> 数据契约管**形态**（`contract/*.schema.json`，见 [contract/README.md](../contract/README.md)）；
> 本文管**行为**。两者不共用一把尺子，也不建两套门禁：行为测试与契约测试都跑在
> `scripts/verify.ps1` 的同一条链上。

**一句话**：本仓库不靠“文档写得对”来保证行为，而靠**每条行为在测试里有一条断言**。

## 1. 真实链路

```
plan（宿主 cron 每天一次）     从 rules/routines.json 确定性生成今天窗口
  ↓
due（宿主 cron 每 5 分钟）     到点投递：stdout = 正文；空 = 静默
  ↓
ack / check / cancel / …       用户交互：只改状态，stdout 永远为空
  ↓
rollover（次日 plan 时）       跨日：昨天终态归档进 history.db，今天重建
```

**没有**任务创建 API、任务数据库、进程内 Scheduler、失败重试队列、Agent 调用。
`--no-agent` 是设计决定：提醒到点由代码直接产出文案，不经过模型。

## 2. 状态转换表

窗口状态（`alarm-state.schema.json` 的 `state` 枚举）：

| 状态 | 含义 | 终态 |
|---|---|---|
| `pending` | 已生成，还没到点 | 否 |
| `ringing` | 正在响（`fireCount` 递增） | 否 |
| `delivered` | 一次性/复盘已送达，不等确认 | 是 |
| `acked` | 用户确认（或清单全部勾完） | 是 |
| `missed` | 到点没投递成功（`missedReason` 说明原因） | 是 |
| `skipped` | 生成时已过期 / 暂停日 / 显式跳过 | 是 |
| `cancelled` | 用户取消 | 是 |

允许的转换（`→` 之外的组合一律视为 bug）：

| 起点 | 允许到 | 触发者 |
|---|---|---|
| `pending` | `ringing` / `skipped` / `missed` / `cancelled` | `due` / `plan` / `cancel` |
| `ringing` | `ringing`（下一 tick）/ `acked` / `delivered` / `missed` / `cancelled` | `due` / `ack` / `cancel` |
| `acked` / `delivered` / `missed` / `skipped` | `pending`（**仅** `check --undo` 撤销打勾，清单未勾完） | `check` |
| `cancelled` | 不动 | — |

三条不许发生的转换：

1. 终态 → `ringing`（除 `check --undo` 那条受控回退）
2. `acked` → 任何「重新响」
3. 同一 `(window_id, 日期)` 同时存在两个状态

## 3. 输出与退出码契约

| 命令 | stdout | 退出码 | 诊断 |
|---|---|---|---|
| `plan` | 空 | 0（含冲突）/ 3（配置非法） | stderr 一行 + `state/conflicts.jsonl` |
| `due` | 正文或空（**冲突时为空**） | 0 / 3 | stderr + `state/conflicts.jsonl` |
| `ack` | 空 | 0 / 2（重试耗尽）/ 3（外部回调参数不全）/ 4（找不到窗口或日期不符） | stderr + `state/conflicts.jsonl` |
| `check` / `cancel` / `postpone` / `complete` / `disable` / `enable` | 空 | 0 / 2 / 3 / 4 | stderr |
| `history` / `events` / `stats` / `status` | 正文 | 0 / 4 | stderr |

**stdout 就是投递通道。** 非空 = 用户会收到一条消息；空 = 静默。所以：
交互命令的 stdout 必须为空，冲突时 `due` 的 stdout 也必须为空。

**退出码 2 不等于“这次失败要通知用户”**：宿主 `--failure-deliver` 会把非零退出变
成一条失败通知。正常竞态不是失败，因此 `due` / `plan` 冲突**退出 0**；
只有 `ack` 在 3 次重读都写不进去时才退出 2。

### 3.1 时区口径（#91）

- 时刻表时区以 `timezone`（IANA 名）为准；缺省回退旧格式 `tzOffset` 固定偏移，再缺省回退 `+08:00`。回退链与 Windows 缺 tzdata 的限制见 `docs/timezone.md`。
- 运行时只用标准库 `zoneinfo`；`tzdata` 只在 dev/test 与 CI 测试层，不进运行时依赖。
- `.ics` 导出必须带时区：UTC 瞬时用 `Z` 后缀，本地事件用 `TZID` + `VTIMEZONE`，**不允许浮动时间**。
- `schedule --json` 仍是纯 JSON 到 stdout；退出码语义（0 / 3 / 4 等）不变。

### 3.2 L2 事件台账存储口径（P2 / D-015）

- **源真相**：`calendar/<YYYY>.ics`（RFC 5545，UTF-8 无 BOM、CRLF、75 octet 折行）。按事件 `DTSTART` 年份归文件；跨年事件在结束年份文件留一段 `X-RTC-CROSS-YEAR` 交叉引用，合并查询按 `UID` 去重，主记录取开始年份那份。
- **只读索引**：`state/calendar-index.db` 是派生缓存，不是真相；删除整个库后 `rebuild` 可从全部 `.ics` 零成本重建。索引查询与直扫 `.ics` 逐条对账，字段不一致即以 `.ics` 为准。
- **冲突口径**：索引与 `.ics` 不一致时**以 `.ics` 为准**；陈旧记录只剔除并往 stderr 留一行可读告警，索引不反向改写 `.ics`，读取路径不做静默重建。
- **影子派生视图**：`state/events.json` 是面向旧读路径（P3 occurrence 适配器）的派生影子副本，不是真相。`--to-ics --apply` 在写完 `.ics`、重建索引后，**从 `.ics` 逐条重建这份影子视图**，保证适配器读到的核心判定字段与真相一致，不读到迁移前的陈旧快照。影子视图只背 §5c 契约字段；`.ics` 专有扩展（`summary` / `categories` / `tzid` / `unknown` / `crossYear` / `rev` / `dtstamp`）留在真相层，不进派生视图。重建幂等；迁移前已有的影子账本随备份留存，回滚时按 manifest 恢复或删除。
- **迁移**：`rtc_migrate.py --to-ics` 默认 dry-run；`--apply` 真跑前自动备份（`.ics` 目录、索引、影子账本 + 旧文件快照与哈希 manifest）；写完 `.ics`、重建索引后接着从 `.ics` 重建影子派生视图；重跑幂等（旧 id 命中别名保留原 `UID`，不重生；影子视图事件集合与核心字段不漂移）；`--rollback <备份目录>` 按 manifest 恢复，影子产物一并还原或删除。旧 `routines.json` / `alarms.json` 全程只读，迁移后留在原地，不删不改。
- **本批边界**：P2 只落地 `.ics` 真相层、索引重建与迁移工具；不改 `plan` / `due` / `ack` 的判定，不做 RRULE 实例展开（P3）、不接 NLU（P4）、不开放 CRUD 命令面（P5）、不做查询筛选与导出命令（P6）。

## 4. 冲突策略（乐观锁）

写入前读 `rev`，写入时比对，不符即放弃：

| 命令 | 冲突行为 | 为什么 |
|---|---|---|
| `plan` | 不重试，退出 0，stderr 一行 | 冲突说明别人刚写过，下一次 cron 会重跑 |
| `due` | 不重试，**空 stdout**，退出 0 | 跳过一 tick 最坏是晚 5 分钟，`fireCount ≥ expectFires` 保证不会无限重复 |
| `ack` | 有界重读重试，最多 3 次（10–50 ms 退避） | 用户不会再说一遍，丢了就是真丢 |

`ack` 重试的三条硬要求：

1. 每次重读 state 后按**同一个 window id** 重选窗口，不能匹配到别的窗口也算成功
2. **幂等守卫**：重试时目标窗口已是 `acked` → 视为成功，不重复 bump `rev`，不产生第二条 history
3. 3 次耗尽 → 退出 2，stderr 给尝试次数与最后的 `rev`

> `alarm-state.schema.json` 里 `rev` 的描述与本节是同一份契约的两处渲染，改一处必须改另一处。

### 4.1 外部回调 ack（#70）

平台适配器收到「对方回了」时，用带日期与幂等键的方式结束当天窗口：

```bash
python rtc.py --workspace <dir> ack \
  --window <id> --date YYYY-MM-DD \
  --source external --event-id <事件id> --text "<可选>"
```

- `--source external` 必须同时给 `--date` 与 `--event-id`，否则退出 `3`
- `--date` 必须等于当前 `state.planDate`，否则退出 `4`，不写任何东西
- 同一 `--event-id` 重复回调：退出 `0`，不 bump `rev`，不写第二条 history
- 同一 `--event-id` 指向另一个窗口：退出 `4`（不允许改绑）
- 幂等键记在窗口的 `ackEventId` 上，和 `ackedAt` / `ackText` 同一次 `atomic_write` 落盘
- 鉴权在适配器侧完成；RTC 只接收已经验过签的内部调用，不对外暴露无鉴权入口

## 5. 空日契约

空日（当天本来就没有该响的东西）**不是错误**，但必须留下痕迹：

- 显式禁用（`weekendAlarms: false` + 周末 / `holidayAlarms: false` + 节假日）→ 合法空日
- 合法空日仍要落盘 state（`windows: []`、`planDate`、`planKind`、`rev+1`），
  否则 `due` 每次醒来都重跑 `plan`
- 合法空日**不得跳过成功判定心跳**：`due` 的判定主体无论是否命中窗口都正常收尾，
  由外层包装统一写 `lastOkAt`（含静默），否则次日 `self_check` 会误报“调度器停过”。
  `attemptAt` 在进入 `due` 时即写；失败路径（非 0 退出 / 冒泡异常）不推进 `lastOkAt`
- 其它原因造成的空窗口 → 配置错误，退出 3（`Validation`）

## 5b. 停摆恢复契约（issue #94）

- 判定要两条证据凑齐：`attemptAt` 陈旧超过两个 tick（10 分钟）**且**账本滞后于今天（旧 `planDate` 还没补建），才进入恢复路径。心跳陈旧但账本是今天的（同日稀疏 tick、有意静默）不算停摆；心跳新鲜的连续调度一次都不进入。
- 恢复归档是确定性账本修复，不依赖模型在线：旧 `planDate` 与今天之间错过的窗口记 `missed` / `host_outage`，暂停期与显式禁用日不算漏响。
- 同一停摆以停摆前最后一次 `attemptAt` 为身份，`events` 记一条 `kind=recovery`；重复核对不二次归档、不重复投递。
- 恢复提示只有一条，走 `due` stdout；状态冲突未写成时不投递（沿用 due 静默口径）。
- `due --bootstrap` 是宿主重启钩子的显式核对入口，不新增子命令；心跳新鲜或账本是今天时不伪造停摆。

## 5c. L2 事件模型契约（P1 影子账本）

> 状态：P1 落地离线投影；**P2 起 `.ics` 为源真相**，`state/events.json` 转为只读缓存视图。本章节字段口径在 P2 不推翻。

- 事件一等公民字段：`uid`（RFC 5545 §3.8.4.7 风格，UUIDv4 + `@rtc-kit` 后缀；改期、取消、状态迁移都不换）、`kind`（`once` / `recurring`）、`start` / `end`（带偏移 ISO 8601）、`rrule`（RFC 5545 标准字符串，一次性为 `null`，不另造自研 DSL）、`owner`（默认 `self`；替别人记 `other:<name>`，事件级字段）、`deadline`（可空日期）、`status`（`scheduled` / `ringing` / `done` / `cancelled` / `missed`）、`reminderPolicy`（`preRemind` / `ddlLadder` / `snooze` 预留为 `null`，P5 接入）、`aliases`（旧 id / ocid 列表）。`.ics` 往返字段：`summary`（标题）、`categories`（分类）、`tzid`（事件级 IANA 时区名）、`dtstamp`（修订时间戳）、`unknown`（不认识属性的开放兜底对象）、`rev`（修订号）、`crossYear`（跨年标记）；P1 投影除 `summary` / `dtstamp` / `rev=0` 外均为 `null`，形状以 `contract/event.schema.json` 为准。
- 投影口径：旧 `oneOff` → `once`；周期窗口 → `recurring`，RRULE 由 `dateTypes` 生成（workday → `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR`，weekend → `SA,SU`，两者皆无 → `FREQ=DAILY`，`validUntil` → `UNTIL`）；`dueDate` → `deadline`。`UNTIL` 按事件时区产出 **date-time**（当日 `23:59:59` + 偏移），不用 date-only——否则展开器兜成当天 `00:00:00`，截止日当天晚间实例会被误判过期。旧账本 state 映射：`pending→scheduled`、`ringing→ringing`、`acked/delivered→done`、`cancelled→cancelled`、`missed→missed`、`skipped→scheduled`。
- `rev` 口径：事件每次写操作 +1，新建/投影为 0；序列化进 `.ics` 的 `X-RTC-REV` 并在索引层同步；不从旧账本 `seq` 继承（兼容读取由 `aliases` 承担）。
- `end` 缺省时长（A5）：`end` 缺省默认与 `start` 同刻（零时长事件），不在适配器里藏默认分钟数；旧窗口 `end` 缺省同样回退 `start`。
- `.ics` 往返边界（A6）：`createdAt` / `updatedAt` / `source` 是本地投影留痕，**不随 `.ics` 走**；交换格式只带 RFC `DTSTAMP`（修订时间戳）与 `X-RTC-REV`；`source`（`routineId` / `ledgerDate`）属内部溯源，不进 `.ics` 属性。
- UID 算法取舍（A7）：P1 不收敛新旧两套算法。新层 `uid` 是 UUIDv4 + `@rtc-kit` 的稳定身份；旧 `ocid` / `id` 进 `aliases` 兼容读取；旧导出的 `id-<开始时间>@rtc-kit` 回退算法只作兼容读取，P2 `.ics` 改读新模型后统一使用新 `uid`。
- 真相边界：P1 旧 `rules/routines.json` + `state/alarms.json` 仍是唯一真相；`events.json` 是派生影子副本，**派生永不反写真相**（沿用 §五 规则 1）。迁移只读旧文件，不回写；影子视图由 `.ics` 重建，同样不反向回写 `.ics` 或旧文件。
- 迁移四条性质：默认 dry-run；真跑前自动备份（manifest 留源文件哈希）；幂等（别名 / uid 已存在则跳过，不换 uid、不产生第二份）；`--rollback <备份目录>` 可恢复（迁移前无影子账本的，回滚删除本次产物）。P2 `--to-ics` 在写 `.ics`、重建索引后接着从 `.ics` 重建影子视图（见 §3.2）。
- 热路径边界：`rtc_events` / `rtc_migrate` **不得**被 `plan` / `due` / `ack` tick 路径 import；有静态断言测试守住。
- 旧命令面零改动：`status` / `history` / `schedule` / `due` / `report` 的 stdout + stderr + 退出码，迁移前后逐字节一致（golden 用例按 SHA256 比对）。P2 另补空工作区（无 `events.json` / 无 `calendar/`）五条命令与 `origin/main` 基线的 SHA256 golden 对照；CI 浅克隆拿不到 main ref 时退化为当前 code 两遍自比。

## 5d. L2 事件 CRUD 与提醒策略契约（P5）

> 状态：P5 落地事件级写命令；occurrence 粒度完成 / 平移留 P3，`.ics` 真相源留 P2，自然语言短语解析留 P4。本章节命令面在后续 phase 只增不改旧语义。

### 5d.1 命令面（事件级，结构化参数）

- `create`：新建一条事件。参数 `--title`（必填）、`--start` / `--end`（带偏移 ISO 8601）、`--rrule`（RFC 5545 字符串，省略 = 一次性）、`--owner`（默认 `self`，替别人记 `other:<name>`）、`--deadline`（`YYYY-MM-DD`，可空）、`--pre-remind N`、`--ddl-ladder N`（可重复）、`--snooze N`、`--uid`（幂等重放用，缺省自动生成）、`--now`。校验不通过退出 `3`；同 uid 已存在则幂等返回 `0`，不产生第二条、不涨审计 / history。
- 解析器映射口径（P4 接 create 时）：解析器输出 `dtstart`（tz-aware datetime），落账为 `event.start = parsed.dtstart.isoformat()`；`end` 按窗口宽度 / 入参派生。`dtstart` 与契约字段 `start` 是命名差异、不是语义冲突；契约不迁就解析器改名，解析器也不反改影子账本字段。
- `edit`：按 `--uid` 或 `--alias`（旧 id / 别名）局部更新；只改给了的参数，未给的字段保持原样。改完整包过事件契约；找不到事件退出 `4`。
- `complete`：`--uid` / `--alias` 把事件标 `done`；已是 `done` 时幂等返回 `0`，不重复留痕。occurrence 粒度的「只完成这一次」留 P3 接线。
- `cancel`：窗口级取消行为一字不变；`--uid` / `--alias` 取消事件，`done` / `missed` 终态不可再取消（退出 `4`）。`--undo` 撤销当天的事件取消（见 5d.3）。
- 旧子命令与退出码语义不变：窗口级 `cancel --window` / `complete --window` 走原路径；窗口级撤销（#113）与窗口级 snooze（#114）未在本 lane 接线。

### 5d.2 写操作留痕与并发

- 每个写命令成功后落一条审计（`action` + `target` + `old` / `new` + `actor` + `source`），并写一条 history 事件；审计 / history 失败不阻断主流程，只在 stderr 打一行（沿用 #110 口径）。
- 并发守卫：读账本时记内容摘要，写盘前在排他锁内复核摘要；被别人改过就抛 `Conflict`（退出 `2`），磁盘内容不被静默覆盖。`events.json` 写临时文件后原子替换，半写 / 坏写可由加载层兜底识别。
- 审计动作名：`create` / `edit` / `complete` / `cancel` / `cancel-undo`；`source` 前缀 `event:`。

### 5d.3 撤销（undo）口径

- `cancel --uid <uid> --undo` 按当天审计快照逐字段还原取消前状态；`uid` / `aliases` / `createdAt` 保持不变。
- 只当天有效：痕迹时间戳换算到运行时区日历日，跨日拒绝（退出 `4`）；跨日恢复走后续 `enable` 口径，本版本不提供。
- 无痕迹 / 事件当前不是 `cancelled`：无痕迹失败，退出 `4`，账本与审计不新增条目。

### 5d.4 提醒策略（`reminderPolicy`）

- `preRemind`：`{ "minutesBefore": N }`，锚点为事件 `start`；到点单独一档，不与到点提醒合并。
- `ddlLadder`：截止阶梯提前量分钟数数组（`≥0`，去重升序）；以 `deadline` 为锚点。
- `snooze`：`{ "minutes": N }` 默认 snooze 时长（`≥1`）。
- 同 tick 档位优先级：阶梯预警 > 提前提醒 > 到点提醒；同一时刻重合只取最早一条。实际档位计算与 occurrence 平移由 P3 / due lane 落地；本版本只把策略形状落进事件记录。
- 事件级 occurrence snooze 的幂等键 =（事件 uid，日期，目标时刻）；只往后推、同一天，occurrence 粒度实现留 P3，届时与窗口级 #114 收敛去重。

### 5d.5 边界与缺口

- 本阶段不写 `.ics`；`events.json` 仍是影子账本，`create` 落库时标注「待 P2 接管」。
- 不做 occurrence 展开；`recurring` 事件的 `complete` / `cancel` 只改事件级状态，后续 occurrence 不受影响（P3 接线后再分粒度）。
- 不接 NLU 短语；命令面只收结构化参数，自然语言解析在 P4。
- 不做 year / range 查询与 `.ics` 导出（P6）。

## 6. 三条硬规矩（跨类型）

| 代号 | 规矩 | 落地方式 |
|---|---|---|
| **I1** 不复活 | 取消了就不再提醒 | 取消写 `(window_id, 日期)` 粘性状态 + 窗口参数截断（一次性 → `disabled`；倒计时 → `dueDate` 截到今天 + `canceled`） |
| **I2** 不误时 | 推迟后原时刻不再响 | 推迟 = 把火点移到新槽，原日期进 `skipDates`；不是“复制一份” |
| **I3** 不误消 | 取消只针对精确实例 | 取消按 `(window_id, 日期)` 锁定；`cancel --date` 只动那天，20 号那次取消不影响 15 号 |

日程类型（`fixed` / `oneoff` / `appointment`，`cancelled` 是状态覆盖）与倒计时
生命周期（`dueDate+1` 复盘一次后永停）见 `rules/routines.example.md` 与 issue #9 §10。

## 7. 契约 → 测试对照（每一条都能指到断言）

| 契约 | 测试 |
|---|---|
| 一次性只活自己的日期，跨天不重生 | `TestOneOffNoRegen`、`TestPruneExpiredOneOff` |
| 倒计时最后一天 → 次日复盘 → 永停 | `TestScheduleTypes`、`TestCountdown` |
| 取消 → 整段停（含复盘日） | `TestScheduleTypes.test_cancelled_countdown_has_no_review` |
| 推迟不误时（原槽静默） | `TestPostpone`、`TestPostponePeriodic`、`TestPostponeCountdown` |
| 取消不误消（只动指定日） | `TestCancelCompleteDisable.test_cancel_future_date` |
| `due` 冲突静默 + 退出 0 | `TestDueConflictSilent` |
| 状态文件始终符合契约 | `TestSevenOpsSameDay.test_seven_ops_keep_state_contract_valid`、`TestSchema` |
| 合法空日落盘、不误报 | `TestEmptyDaySemantics` |
| 清单撤销回退（不留“已确认未勾完”） | `TestChecklist.test_undo_after_all_checked_restores_reminder` |
| 清单勾选跨天延续 | `TestChecklist.test_checked_survives_next_day` |
| 稳定 ID / orphaned 事件 | `TestStableIds` |
| 历史可查（删快照也能查） | `TestHistoryDB`、`TestOneOffNoRegen.test_history_keeps_the_record` |
| 并发写不覆盖 | `TestPostponeRollbackPreservesConcurrentEdit`、`TestLockOwnership` |
| 重启后按 `planDate` 补建 | `TestMainlineRegression` |
| L2 P1 事件投影 / 别名 / 迁移四条性质 | `test_events_p1.py` 的 `TestWindowProjection`、`TestMigrationSkeleton` |
| `UNTIL` 为 date-time，截止日当天晚间实例仍在窗口内 | `TestWindowProjection.test_until_is_date_time_and_keeps_last_day_evening` |
| 往返七字段过契约；`rev` 重入 +1、JSON 往返守恒、负 `rev` 被拒 | `TestEventSchema.test_roundtrip_fields_validate`、`TestWindowProjection.test_rev_increments_on_reupsert_and_survives_round_trip`、`TestEventSchema.test_rejects_negative_rev` |
| 迁移前后五条旧命令逐字节一致 | `TestGoldenRegression.test_five_commands_byte_identical_after_migration` |
| tick 热路径不 import 事件栈 | `TestHotPathImportGuard` |

## 7.1 RRULE 周期规则语义（`code/rtc_rrule.py`）

纯解析 / 展开引擎：输入 RRULE 字符串与 DTSTART，输出 occurrence 列表。引擎本身不读写账本；接入 plan / due / ack 的口径见 §7.2。

### 支持的键

| 键 | 取值 | 语义 |
|---|---|---|
| `FREQ` | `DAILY` / `WEEKLY` / `MONTHLY` / `YEARLY` | 必填，重复粒度 |
| `INTERVAL` | 正整数 | 间隔，默认 `1` |
| `COUNT` | 正整数 | 全局 occurrence 预算；与 `UNTIL` 互斥 |
| `UNTIL` | `YYYYMMDDTHHMMSSZ`（UTC）或 `YYYYMMDDTHHMMSS`（本地浮动） | 截止边界，**含边界**；与 `COUNT` 互斥 |
| `BYDAY` | `MO/TU/WE/TH/FR/SA/SU`，可带序号 `1MO` / `-1FR` | 序号前缀仅 `MONTHLY` / `YEARLY` 有效 |
| `BYMONTHDAY` | `1..31` 或负数（`-1` = 当月最后一天） | 月内日号过滤 |
| `BYMONTH` | `1..12` | 月份过滤 |
| `WKST` | `MO..SU` | 周起点，默认 `MO`；影响 `WEEKLY` + `INTERVAL>1` 的跳周锚点 |

### 展开规则

- 窗口为半开区间 `[window_from, window_to)`；返回列表按时间升序，每个 occurrence 沿用 `dtstart` 的 `tzinfo` 与时刻。
- `COUNT` / `UNTIL` 是**全局预算**：从 `dtstart` 起数满 `COUNT` 条或越过 `UNTIL` 即停，不会因为查询窗口只截一段就重新计数。
- 时区感知：`dtstart` 带时区时，每个 occurrence 保留同一 `tzinfo`；跨 DST 春切 / 秋切不漂移、不重复、不跳过。
- 闰年 `2/29`：平年直接跳过，**不挪到 3/1**；`BYMONTHDAY=-1` 取当月最后一天；无效日（`2/30`、`4/31` 等）跳过该次。
- 序号 `BYDAY`：`MONTHLY` / `YEARLY` 下按「当月第 n 个周几」解释，负号从尾数；裸周几在 `DAILY` / `WEEKLY` 下做星期过滤。

### 错误与防御

- 非法字符串、未知键、重复键、数值越界、`COUNT` 与 `UNTIL` 同给、时区口径不一致 → 抛 `RruleError`，消息可读；**不静默返回空列表**。
- 双硬上限：条数 `MAX_OCCURRENCES`、扫描年数 `MAX_SCAN_YEARS`；超限抛 `ExpansionLimit`，防止规则写错导致死循环。

### 明确不支持（本阶段）

`BYSETPOS`、`BYHOUR` / `BYMINUTE` / `BYSECOND`、`BYWEEKNO`、`BYYEARDAY`、`RDATE` / `EXRULE`。引擎零第三方依赖，只用标准库 `datetime` / `zoneinfo` / `calendar`，不引入 dateutil。

## 7.2 occurrence 实例化与去重（`code/rtc_occurrence.py`）

P3 下半段把影子事件账本里 `kind=recurring` 的事件实例化进当天账本，供既有 plan / due / ack 机器判定；`once` 事件仍走旧 `routines.json` 链路，本模块不投影。

### 接入口径

- 适配层唯一入口 `occurrence_windows(workspace, tz, day, now, errors=...)`；`rtc_core` 只调本模块，不直接 import `rtc_events` / `rtc_rrule`（热路径守卫口径见 P1 测试 `TestHotPathImportGuard`）。
- 只展开当天半开区间 `[00:00, 24:00)` 内的 occurrence，不无限展开；`COUNT` / `UNTIL` 沿用引擎全局预算，不从当天重新计数。
- 事件账本内部去重键 = `(uid, occurrence start)`；同一事件同一天多条实例按起点区分。
- 跨源去重（迁移期旧 `routines.json` 窗口与投影事件并存）：在 `rtc_core` 装配层按 `(旧 id / 别名, 日期, 起点时刻)` 比对；事件窗口 `label`（= 事件 `aliases[0]`）命中旧窗口 `id` / `label` 且当天起点时刻相同，认定是同一事项——保留旧窗口、丢弃该事件 occurrence。身份对不上的两条窗口即使同时刻也不去重。
- 账本窗口身份：`id` = uid 去掉 `@rtc-kit` 后缀的 slug；`ocid` = `<slug>@YYYY-MM-DD`；确认 / 归档走既有 ocid 机器。
- 状态过滤：仅 `scheduled` / `ringing` 实例化；`done` / `cancelled` / `missed` 跳过。
- 时区：事件 `start` 自带偏移则沿用；朴素时刻挂工作区时区。产出 `start` / `end` 一律带偏移，不写裸 naive。
- 单条事件解析 / 展开失败只跳过它自己并记一条诊断，不拖垮整天计划；账本缺失 / 损坏 / 空信封 → 返回 `[]`。

### 语义

- due 命中按 occurrence 起点所在窗口判定；`expectFires=1`，重复 tick 达上限后静默，不重复响。
- ack 只确认当前这一条 occurrence；次日重新生成的新 occurrence 是 `pending`，昨天的确认不带过去。
- 跨年照常出现，跨年查询不重复不丢。

### 兼容红线

工作区没有 `events.json`（或空信封）时，`plan / due / ack / status / history / schedule / report` 输出与基线逐字节一致；既有测试一条不改全绿。

### 已知缺口

- occurrence 级取消 / 改期（单条 occurrence 推迟、取消）未接；旧 routine 与事件账本的别名映射全量对齐、以及读切换阶段的跨源合并，留待后续阶段；当前跨源去重只认 `aliases[0]` ↔ 旧窗口 `id` / `label` 这一条链。
- 带固定偏移的事件时刻不跟随工作区 ZoneInfo 的 DST 切换；朴素时刻挂 ZoneInfo 的路径才跟随秋切 / 春切。

## 8. 明确不做

- Rules Engine 框架、进程内 Scheduler、宿主侧故障注入、BDD 运行框架
- 断网 / 网关崩溃 / Agent 超时类测试：宿主侧，脚本观测不到，写进来就是永远红的测试
- 自建重试队列：`due` 的下一个 tick 就是重试；真正的失败（投递层）由 `delivery` 记录，不静默重发

### 早报的模型调用是唯一的例外（issue #60 方案 B）

§1 说「没有 Agent 调用」，指的是**判定链路**：`plan` / `due` 永远是纯代码。早报是另一件事——
它要说「昨天做成了什么、没做成什么」，这些信息账本里没有，只有人或模型知道。所以：

- **允许**一条 09:00 的 agent 模式 cron（`rtc-report`）调模型写汇报
- 该 cron **不参与任何判定**：不算火点、不改 `alarms.json`、不碰 `routines.json`，只写 `state/reports/<日期>.md`
- **模型不在线不能导致不响**：09:10 的 `report-fallback` 窗口用同一批账本数据发静态汇报
- **兜底只被投递回执压住，不被文件存在压住**：09:00 的 cron 投完要记一条 `--window report --result ok`；没有回执时，兜底窗口必须再发一次（有文件发文件正文，没文件发静态版）。文件存在只说明模型写了，不说明用户收到了
- **被回执压住的兜底分两种账**：没响过（`fireCount=0`）记 `skipped`（原因 `covered_by_receipt`），它从未投递，只是无需再响；已经响过（`fireCount>0`）的迟到回执只能止损，记 `delivered`，不能把已经发出去的文本抹成 `skipped`
- **兜底截止点以第一次实际兜底判定为准**：09:10 是窗口起点，不是墙钟硬截止；09:10:30 写好的汇报会被 09:11 的第一次兜底 tick 使用。第一次兜底判定之后才写好的内容，当天不再采用
- **明确选「至少一次」，不选「至多一次」**：回执晚到（渠道已送达但回执在 09:10 之后才落盘）会多出一条汇报；反向选择则是无证据就静默，用户可能整天收不到。有据可查的重复优于无据可查的失踪
- **单一输出所有者**：`format: report`（due 直接出稿）与 `format: report-fallback`（模型先写 + 回执兜底）只能二选一，`plan` 同时看到两者直接拒绝——否则同一份汇报会在 09:00 与 09:10 各发一遍
- 因此「cron 恒定两条」的旧约束在此处放宽为三条，且仅此一处

## 9. 这份文档怎么维护

**行为变了，先改这里，再改测试，最后改代码。**
只改代码不改这里，等于把契约藏回 `if` 分支里——那正是 #4 要结束的状态。

### Postpone storage failures

`postpone` checks the existing ledger's JSON, revision, windows list and window id/state fields before changing the routine. A missing ledger or empty `{}` retains the existing no-plan semantics. Invalid required fields in a nonempty ledger reject the operation before the schedule write. This is scoped validation for postpone, not exhaustive schema validation. After the schedule commits, the ledger is read and checked again so a concurrent plan is not missed.

### Postpone ledger day match

`postpone` mutates only the ledger that belongs to today (calendar date compared in the routine timezone). A missing ledger or empty `{}` keeps the existing schedule-only semantics: the routine moves, and no ledger, snapshot or occurrence history is fabricated. A nonempty ledger whose `planDate` is an earlier date is left byte-identical: neither its occurrence nor that day's snapshot is rewritten. The routine still moves; the command returns `0` with empty stdout and an explicit stderr note, and the normal `plan` generates today's ledger. A nonempty ledger with a missing, malformed or future `planDate` is rejected before any routine write (exit `3`). After the routine commits the ledger is reloaded and the date rechecked: a plan that appeared meanwhile is updated under the existing conflict protection, and a different day's ledger is never mutated on retry.

### Postpone snapshot exclusion

When postpone rebuilds today's snapshot, its `windows` list is the remaining schedule: every ledger occurrence that is `skipped` with `missedReason: postponed` is excluded, across successive postponements — not only the window just postponed. Other skipped reasons (disabled, date-type filtered, expired, cancelled, acked) stay visible with their reason. Existing snapshot `results` are preserved on rebuild; postponed outcomes remain recorded in `results` / history with their original day identity and reason.

If the routine commits but the subsequent state write raises an I/O or validation error, the command returns `3`, keeps stdout empty, and reports partial application on stderr. Today's existing occurrence may still fire. Inspect and repair the storage/state problem first, then retry using the same explicit `--window`, `--to` date and `--deadline` mode (when used) printed in the diagnostic. Do not blindly retry a relative date after the day has changed.

This error path does not roll back over another writer's routine edits. The existing `Conflict` path retains its digest-guarded rollback. These are two separate file writes, not a crash-atomic transaction; a process interruption between them still requires recovery. No live notification-delivery guarantee is implied.

If the ledger write returns successfully but the subsequent history call raises an error, the command still attempts snapshot synchronization. It returns `3` with empty stdout and reports that the occurrence is already skipped, whether snapshot synchronization succeeded, and that history needs inspection. It does not claim the occurrence may still fire or advise retrying postpone to repair missing history. The history helper normally catches its documented storage failures and emits its own warning; that existing best-effort policy is unchanged.
