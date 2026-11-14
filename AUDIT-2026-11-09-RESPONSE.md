# 审计回应 — 2026-11-09 审查包

- **被审提交**：`2918225feda84084bca2e68f91b1accac3c49871`
- **审计方**：nqble 协调，Claude Opus 5.5 执行，2026-11-09
- **本文性质**：对审计结论的逐条处置记录，不是对审计的背书或反驳
- **日期**：2026-11-11

---

## 一、这份审计可信吗

我按「不预设它正确」的方式独立复核了一遍：读完全部文件，用 `jsonschema 4.26.0` 重跑它声明的断言，并把仓库里唯一可执行的脚本 `code/metrics.mjs` 放进沙箱实跑（审计明确声明未运行任何脚本）。

结论：**9 条发现全部实质成立**，没有一条是无根据的。其中第 7 条从「静态推断」升级为「实测复现」：

```
$ node code/metrics.mjs sync          # 按 README 的部署布局：脚本在 CODE_DIR，状态在 STATE_DIR
metrics 失败：找不到 .../code/alarms.json
exit=1
```

两处表述需要修正，已在下方标注：

1. 审计开头称「未填充的示例必然失败」。在同一份审计自述的配置下（未开启 `format` 断言），`alarms.example.json` 的占位符全部落在 `date-time` 字段，**未填充也能通过**；另两个因 `pattern` 被拒。这暴露的真问题是：「通过 Schema 校验」在本仓库没有唯一定义，`ajv-cli` 与 Python 校验器对 `format` 的默认行为不同。
2. 第 8 条 `dateTypes` 一项的坐标不准。顶层 `dateTypes` 的键拼错**会被拒绝**（`additionalProperties` 生效），真正漏的是 `alarmWindows[].dateTypes`——裸字符串数组、无枚举。

---

## 二、逐条处置

| # | 原判 | 本次处置 | 落地位置 |
|---|---|---|---|
| 1 | 到期判定归谁，文档自相矛盾（高） | **已修**。判定收进 `code/rtc.py`，成为唯一判定者；`remind.mjs` / `due.mjs` 这类「文档里有、仓库里无」的引用不再作为路径描述 | `code/rtc.py`、`platform/hermes.md` |
| 2 | 自动重建计划会撤销 ack（高） | **已修**。`plan` 的跳过键改为 `planDate == today`（不再看 `generatedBy`）；重建时把同日同 id 的 `acked/missed/skipped` 原样带回 | `code/rtc.py:carry_forward` |
| 3 | ack 匹配会静默未来窗口（高/中） | **已修**。匹配条件改为 `start ≤ now ≤ end + 15min`，按 `start` 升序取最早一个；宽限期内允许 `missed → acked` | `code/rtc.py:cmd_ack` |
| 4 | 取消/改期/一次性提醒无持久表示（高） | **已修**（2026-11-13 追加）。改期沿用「次日生效」；一次性提醒以窗口字段 `oneOff` + `validFrom` / `validUntil` 持久表示，送达即完成（`state: delivered`）；`cancelled` 状态仍未加——取消属宿主侧操作，不进状态机 | `contract/routine.schema.json`、`contract/alarm-state.schema.json`、`code/rtc.py`、`skills/alarm-clock` |
| 5 | 状态所有权三套说法，ack 无并发检查（中） | **已修**。单一写者 `rtc.py`；`rev` 比对覆盖 `plan` / `due` / `ack` 三条写路径，不一致抛冲突、不覆盖 | `code/rtc.py:atomic_write` |
| 6 | 路径与章节引用不可解析（中） | **已修**。技能统一读部署名 `rules/routines.md`；`§六` 改为 `§一`；`game_checkin` 改为 `weekdayHabit`；`≠ 4` 改为 `≠ {{WINDOW_COUNT}}`；`weekend_alarms` 进 Schema | 见下「文档修正」 |
| 7 | 指标：输入路径错、口径混（中） | **已修**。`metrics.mjs` 接受 `--state` / `RTC_STATE`，确认率按已结算行计算，`fired` 改标为「发送尝试」；`rtc.py metrics` 提供同口径实现 | `code/metrics.mjs`、`code/rtc.py` |
| 8 | Schema 检查比 README 描述的薄（低/中） | **部分修**。补：`dateTypes` 枚举、`HH:MM` 严格模式（拒 `99:99`）、`acked` 时 `ackedAt` 必填、`history` 允许 `missedReason`。算术一致性与 id 唯一性仍由代码校验（Schema 表达不了） | 见下「Schema 修正」 |
| 9 | 时间处理假设未说明（低/中） | **部分修**。窗口不可跨午夜，`plan` 时直接报错而不是静默错算；时区用显式 `tzOffset`。**DST 仍不支持**（固定偏移），已在 `platform/hermes.md` 写明 | `code/rtc.py:expect_fires` |

### 复核中新增的 5 条

| # | 问题 | 处置 |
|---|---|---|
| A | 技能层已绑定宿主语义（`isolated` ×3、`NO_REPLY` ×6、`cron` ×11），与 `ARCHITECTURE.md:23`「没有任何一层依赖具体 CLI」矛盾 | **部分修**。新增 `platform/hermes.md` 把耦合面显式化；5 份技能的改写留给第二阶段 |
| B | `metrics.db` 也写错目录；`alarms.json.bak` 只写不读；`prep` 不做 `rev` 校验 | **已修**（前两项随第 7 条）；`.bak` 在 `rtc.py` 里被原子写取代，不再需要 |
| C | 运行时下限未声明：`node:sqlite` 需 Node ≥ 22.13 / ≥ 23.4，而 `{{SCRIPT_RUNTIME}}` 与 `.mjs` 扩展名互相矛盾 | **已修**。`code/README.md` 写明下限；`rtc.py` 提供零依赖的 Python 实现 |
| D | `contract/README.md` 规定 `nullable`（JSON Schema 2020-12 无此关键字）与 snake_case（实际全 camelCase） | **已修** |
| E | `alarmWindows[].dateTypes` 是裸字符串数组，无枚举 | **已修** |

---

## 三、文档修正

| 文件 | 改动 |
|---|---|
| `skills/alarm-clock/SKILL.md` | `rules/routines.example.md` → `rules/routines.md`；`reason` → `missedReason`；判定逻辑改为调用 CLI，不再让模型自行判断 |
| `skills/daily-task-prep/SKILL.md` | `routines.md §六` → `§一`；`rules/routines.example.md` → `rules/routines.md` |
| `skills/daily-routine/SKILL.md` | `game_checkin` → `weekdayHabit`；`§五` → `§三`；`≠ 4` → `≠ {{WINDOW_COUNT}}`；路径改部署名 |
| `rules/routines.example.md` | `weekend_alarms` / `holiday_alarms` / `weekday_habit` → `weekendAlarms` / `holidayAlarms` / `weekdayHabit`（与契约的 camelCase 一致） |
| `contract/README.md` | snake_case → camelCase；`nullable` → `"type": ["string", "null"]`；补一条开启 `format` 断言的校验命令 |
| `code/README.md` | 写明 Node 版本下限；补 `rtc.py` 的定位 |

---

## 四、Schema 修正

`contract/routine.schema.json`

- `alarmWindow.start` / `end`：`^([01][0-9]|2[0-3]):[0-5][0-9]$`（拒 `99:99`）
- `alarmWindow.dateTypes`：`items.enum = ["workday", "weekend", "holiday"]`
- 顶层新增 `weekendAlarms` / `holidayAlarms`（boolean），与 `routines.md` 的开关对齐
- `phase.start` / `end` 同用严格 `HH:MM`

`contract/alarm-state.schema.json`

- `historyEntry` 增加可选 `missedReason`（技能本来就在写，Schema 之前会拒）
- `window` 增加条件约束：`state == "acked"` 时 `ackedAt` 必须是字符串
- `historyEntry` 保留 `ackedAt`，配合第 7 条的日切全量归档

---

## 五、`code/rtc.py` 的定位

这是本次 PR 的主体：把「读状态 → 判断 → 写回」从模型手里拿走，让提醒主链路变成一次普通的脚本调用。

```
plan   生成/补建当天窗口（只增不改，保留终态）
due    判断这一刻该不该响；该响则原子更新计数并打印文案，否则不打印
ack    匹配并写入确认（含 15 分钟宽限、missed → acked）
status 人读与 JSON 两种输出
metrics 已结算口径的统计
```

宿主只承担两件事：按时调用、把 stdout 投递出去。stdout 为空即静默——**没有哨兵，也就没有「哨兵出现在该响分支」这类失败**。

已覆盖的回归场景（本地实测，非 CI）：

1. 首次触发输出、未到间隔静默、第二次输出带「第 N 次」
2. 窗口过期自动 `missed`
3. `ack` 后强制 `plan --force` 重建，确认记录不丢
4. 跨日归档保留全部窗口（含 `acked` 及其 `ackedAt`）
5. 当日重复 `plan` 幂等跳过
6. 空状态首次 `due` 自动补建计划
7. 心跳缺失 / 过期、昨日窗口 `fires == 0` 时，`plan` 输出告警且退出码保持 0

### 5.1 两个可靠性补丁

审计第 4 条的正确内核是：**换宿主不解决调度器单点**。`--no-agent` 只让单次执行不调模型，调度器本身仍活在宿主的网关里。网关挂了，任务不跑，而且不报错——这是这套系统最安静的失败方式。

补丁一，心跳：

- `rtc.py due` 每跑一次（无论响不响）写 `state/heartbeat.json`
- `rtc.py plan` 在日切时读它，超过 60 分钟没更新就输出一行告警
- `rtc.py status` 也显示心跳年龄，供人工排查

补丁二，昨日回看：

- 日切归档时保留 `missedReason`
- `plan` 检查昨天的归档：若某窗口 `state == "missed"` 且 `fires == 0`，说明该窗口整段没有一次触发——调度器当时不在

两条都走 **stdout**，所以 `rtc-plan` 可以直接接真实渠道：平时一个字不输出（静默），出问题才推一条。不需要额外的告警通道，也不需要哨兵。

**边界**：这只覆盖「调度器没跑」，覆盖不了「跑了但投递失败」。后者只有宿主知道（Hermes 里落在 `hermes cron incidents` 与网关日志），脚本层无法观测。要彻底解决，得加一条独立兜底通道，或在宿主侧把投递失败转成非零退出。

---

## 六、不在本次范围

- 5 份技能的宿主去耦合（第 A 条）——需要先定 phase 划分，避免半途改动导致行为漂移
- `cancelled` 状态（第 4 条）——取消属宿主侧操作，不进状态机
- DST（第 9 条）——需要 IANA 时区库，当前固定偏移对无 DST 地区够用
- `metrics.mjs` 与 `rtc.py metrics` 的职责合并——先并存，观察一个周期再决定删哪个

---

## 七、2026-11-13 追加：一次性提醒与有效期

第 4 条里被列为「不在本次范围」的 `oneOff`，本次落地。理由是外部真实场景里**正在使用的待办恰好都是一次性的**（行程、临时事项），而带截止日的周期任务（版本限时）同样有明确需求。

### 契约

| 字段 | 位置 | 含义 |
|---|---|---|
| `validFrom` / `validUntil` | `routine.schema.json` → `alarmWindows[]` | 有效期，闭区间，任一端省略即无限期 |
| `oneOff` | 同上 | `true` = 一次性提醒；契约强制 `expectFires` 为 `1` |
| `oneOff` | `alarm-state.schema.json` → `windows[]` | 状态文件里的快照，供 `due` 判断是否送达即完成 |
| `delivered` | 同上 → `state` 枚举 | 新终态：一次性窗口已送达，**不需要确认** |

### 行为

- **一次性窗口**：只响一次；`end` 建议比 `start` 晚 10–15 分钟，用来容忍 cron 恰好错过 `start` 那一分钟；送达后 `state = delivered`；第二天不再生成。
- **有效期**：窗口不在 `[validFrom, validUntil]` 内时不生成。
- **空日语义重排**（原语义只有一种）：
  - 有效期内没有窗口（还没开始 / 已结束）→ **合法空日**，静默
  - `weekendAlarms` / `holidayAlarms` 为 `false` → **合法空日**，静默
  - 有窗口在有效期内却被 `dateTypes` 全部排除 → **配置错误**，退出码 `3`
- **顺带修复**：契约里定义了 `weekendAlarms` / `holidayAlarms`，但 `rtc.py` 从未消费它们——设为 `false` 仍会照常生成窗口。现在已消费，「显式禁用日安静」这条原则才真正成立。

### 验证

新增 `code/tests/test_rtc.py`：18 个用例，进程级 E2E，零外部依赖。覆盖一次性窗口（送达 / 不重复 / 容忍错过 / 次日消失 / 无可确认匹配）、有效期（期内生成 / 未开始安静 / 已过期安静 / 空日仍写心跳）、空日语义（显式禁用安静 / 意外空报错）、契约约束，以及既有主线的回归（plan → due → ack → 幂等 → missed → 跨日归档）。

---

## 八、复现方式

```bash
# Schema（开启 format 断言，与 CI 一致）
npx ajv-cli validate -s contract/alarm-state.schema.json \
  -d contract/examples/alarms.example.json --spec=draft2020 -c ajv-formats

# 核心逻辑（不需要任何宿主）
export RTC_WORKSPACE=/tmp/rtc-check
mkdir -p $RTC_WORKSPACE/rules $RTC_WORKSPACE/state
cp contract/examples/routines.example.json $RTC_WORKSPACE/rules/routines.json
python code/rtc.py plan --now 2026-11-10T06:00:00+08:00
python code/rtc.py due  --now 2026-11-10T07:00:00+08:00
```
