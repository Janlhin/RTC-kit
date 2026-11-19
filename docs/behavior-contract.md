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

`postpone` reads and validates the existing ledger before changing the routine. An unreadable or malformed ledger therefore rejects the operation before the schedule write.

If the routine commits but the subsequent state write raises an I/O or validation error, the command returns `3`, keeps stdout empty, and reports partial application on stderr. Today's existing occurrence may still fire. Inspect and repair the storage/state problem first, then retry using the same explicit `--window` and `--to` date printed in the diagnostic. Do not blindly retry a relative date after the day has changed.

This error path does not roll back over another writer's routine edits. The existing `Conflict` path retains its digest-guarded rollback. These are two separate file writes, not a crash-atomic transaction; a process interruption between them still requires recovery. No live notification-delivery guarantee is implied.
