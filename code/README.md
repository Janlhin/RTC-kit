# code — 确定性脚本

这一层只放**不依赖模型**的代码。两个文件，职责不同：

| 文件 | 角色 | 依赖 |
|---|---|---|
| `rtc.py` | 提醒主链路：`plan` / `due` / `ack` / `status` / `metrics` | Python 3.10+，仅标准库 |
| `metrics.mjs` | 统计旁路（旧版，保留兼容） | Node，需要 `node:sqlite` |

## rtc.py

主链路的确定性实现。把「读状态 → 判断 → 写回」从模型手里拿走，宿主只负责按时调用、把 stdout 投递出去。

```
python rtc.py --workspace <dir> plan     # 生成/补建当天窗口（只增不改）
python rtc.py --workspace <dir> due      # 该响就打印文案，不该响就什么都不打印
python rtc.py --workspace <dir> ack      # 写入确认
python rtc.py --workspace <dir> status   # 人读 / --json 机器读
python rtc.py --workspace <dir> metrics  # sync / report / schedule
python rtc.py --workspace <dir> check --window <ID> --item <名称或序号>   # 清单打勾
python rtc.py --workspace <dir> schedule 2026-12-15      # 查那天的详细日程
python rtc.py --workspace <dir> schedule --month 2026-12 # 那个月的概览
python rtc.py --workspace <dir> postpone travel --to 2026-12-16  # 把提醒挪到别的日子
python rtc.py --workspace <dir> cancel   travel                # 取消一个安排
python rtc.py --workspace <dir> complete travel --text "打完了"  # 完成 + 停用
python rtc.py --workspace <dir> disable  tea                   # 永不提醒
python rtc.py --workspace <dir> enable   tea                   # 又想起来了
```

契约：**stdout 为空即静默**；诊断信息全部走 stderr；退出码 `0` 正常 / `2` 状态冲突 / `3` 校验失败 / `4` 无匹配窗口。
写入是「临时文件 + 原子改名」，并在写入前比对 `rev`，不一致就放弃而不是覆盖。

适配示例见 `platform/hermes.md`。

## 日程存档

`state/alarms.json` 只保存**当天**的计划加最近 14 天的执行摘要。想查一年前某天的日程安排，
靠的是另一份东西：**每日快照**。

```
state/schedule/2026-12-15.json
```

`plan` 成功后写一份，写一次留永久，不清理。每份约 0.5–2 KB，一年不到 1 MB。

一份快照里有：

| 字段 | 内容 |
|---|---|
| `date` / `planKind` / `timezone` | 那天是哪天、什么日期类型 |
| `windows[]` | 全部窗口：label、起止时刻、间隔、期望次数、是否一次性、有效期、确认词 |
| `rollcalls[]` | 那天的播报时点与类型（report / follow / summary） |
| `weeklyChores[]` | 那天的周专项（如周六的洗衣服） |

查法：

```bash
python rtc.py schedule 2026-12-15        # 详细日程
python rtc.py schedule --month 2026-12   # 整月概览，每天一行
python rtc.py schedule 2026-12-15 --json # 机器可读
```

**没有快照时按配置推算。** 未来日期本来就没有快照（快照只有那天才生成），
所以查未来时改为读 `rules/routines.json` 推算那天会是什么安排，输出会标明
「按配置推算」。快照回答「那天实际是什么」，推算回答「那天将会是什么」——
两者不混。推算也推不出来时返回退出码 `4`。

**为什么要单独存一份**：删掉「每天重建」是对的，但旧系统那套里真正有价值的一条是
「每天存档 + 随时能翻历史」。`history` 只存计数摘要且 14 天截断，`metrics.db` 存的是
执行结果——**两者都答不出「那天到底是什么安排」**。

快照由主链路（`plan`）写，不是旁路——旁路可能没跑，快照不会缺。写失败不阻断提醒，
但会在 stderr 留一行诊断。

## 推迟（改到别的日子）

推迟 = **改日期，不改时间**。一条命令做完三件事：

```bash
python rtc.py postpone --window travel --to 2026-05-11   # 挪到指定日期
python rtc.py postpone --window travel --days 3             # 往后推 3 天
```

| 步骤 | 做什么 |
|---|---|
| 1 | 时刻表的日期字段平移到目标日（`start` / `end` 一律不动） |
| 2 | 今天已生成的那一次标为 `skipped` + 原因 `postponed` |
| 3 | 明天由 `plan` 从时刻表重新生成——**不需要重建任何定时任务** |

三种窗口的语义：

| 窗口类型 | 推迟做了什么 |
|---|---|
| 一次性（`oneOff`） | `validFrom` / `validUntil` 一起挪；今天那次标 `skipped` |
| 有效期区间 | 整个区间平移，时长不变（`--deadline` 则移动截止日） |
| 倒计时（`dueDate`） | 截止日挪到目标日，剩余天数由代码重算 |
| 周期（无日期字段） | 目标日之前的日期写进 `skipDates`，跳过这几天，之后照常 |

**写入时刻表时用内容摘要做守卫**（`file_digest`）：读完之后若有人手改过
`routines.json`，拒绝写入并退出 `2`。`routines.json` 是人会编辑的文件，
只用 `rev` 挡不住「读—改—写」之间的手改。

**会拒绝的情况**（退出码 `3`），宁可当场报错也不「推迟成功但明天不响」：

- 目标日期不晚于今天
- 目标日的日期类型与该窗口的 `dateTypes` 冲突（例如窗口只在工作日生效，却推到周六）
- 目标日是显式禁用的日期类型（`weekendAlarms` / `holidayAlarms` 为 `false`）

`postpone` 的 **stdout 保持为空**：它由用户交互触发，不是定时任务，不该被投递出去。
结果看退出码，细节在 stderr。

## 取消 / 完成 / 永不提醒

三个不同意图，三条命令：

| 用户说 | 命令 | 效果 |
|---|---|---|
| 「那个不去了」 | `cancel` | 取消**一个安排** |
| 「这件事做完了」 | `complete` | 记录完成 + 永久停用 |
| 「以后都别提醒我了」 | `disable` | 永久停用**一类**提醒（`enable` 可撤销） |

### cancel 对不同窗口的含义不同

| 窗口类型 | cancel 的效果 |
|---|---|
| 一次性（`oneOff`） | 这件事不做了 → 彻底不生成 |
| 倒计时（`dueDate`） | 截止日截断到今天 → 以后不再生成 |
| 有效期区间 | 有效期截断到今天 → 以后不再生成 |
| 周期（无限期） | **只取消今天这一次**，明天照常 |

最后一格是重点：周期窗口没有「这件事」可以取消——它每天都在响。
要永久停用请用 `disable`。

`cancel --date YYYY-MM-DD` 取消未来某天的那次。先检查那天确实有这个窗口，
不存在则退 `4` 并说明——否则「取消」会静默无效。

### complete 与 ack 的区别

- `ack` = 「这次我知道了，别再响了」→ 只停今天，明天照常
- `complete` = 「这件事做完了，以后也不用提醒」→ 记录完成 + 永久停用

### 契约

| 字段 | 位置 | 含义 |
|---|---|---|
| `disabled` | `routine.alarmWindows[]` | `true` = 永久停用，任何日期都不生成 |
| `disabled` | `alarm-state.windows[]` | 状态快照里的记录 |
| `cancelled` | `alarm-state` 的 `state` 枚举 | 新终态：被用户取消 |
| `cancelledAt` | `alarm-state.windows[]` | 被取消的时刻 |

全部窗口都被停用时，那一天是**合法空日**（静默），不是配置错误。

`cancelled` 与 `missed` 的区别很重要：被取消不是漏响，所以
`self_check` 不会把它报成「整段没响」（归档里保留 `missedReason: cancelled`）。

### stdout 约定

`cancel` / `complete` / `disable` / `enable` / `check` 的 **stdout 保持为空**：
它们由用户交互触发，不是定时任务，不该被投递出去。结果看退出码，细节在 stderr。

## 测试

仓库的第一个回归测试套件：**进程级 E2E**——每个用例在临时工作区里用真实 CLI 调用 `rtc.py`，断言退出码、stdout（投递内容）与状态文件。

```bash
python code/tests/test_rtc.py                          # 直接跑
python -m unittest discover -s code/tests -v           # 或用 discover
```

零外部依赖，只用标准库。契约相关的两个用例需要 `jsonschema`，没装就自动跳过。

改了 `rtc.py` 或契约之后先跑这个；**新增行为必须配一条用例**。

## metrics.mjs

执行情况的统计账本。用运行时的内置 SQLite 模块，**零外部依赖、零构建步骤**（JSDoc + `// @ts-check` 提供类型检查）。

> **运行时下限**：`node:sqlite` 自 Node 22.5 起提供（当时需 `--experimental-sqlite`），22.13 / 23.4 起不再需要该 flag。也就是说，不带 flag 直接跑的最低版本是 **Node 22.13 或 23.4**；Node 20 会直接 `ERR_UNKNOWN_BUILTIN_MODULE`。不想管版本下限就用 `rtc.py metrics`。

```
node metrics.mjs init                 建表
node metrics.mjs sync                 今天 + 全部历史（幂等，最安全）
node metrics.mjs sync today           只落今天的窗口
node metrics.mjs sync past            只补今天以前（日切用）
node metrics.mjs report [天数]        确认率、按窗口、最近 7 天
node metrics.mjs today                只看今天
```

表结构：

```sql
daily(date, window, label, expect, fired, acked_at, missed)
PRIMARY KEY (date, window)   -- upsert 幂等，跑几次结果一样

schedule(date, window, label, start, end, expect, one_off, valid_from, valid_until)
PRIMARY KEY (date, window)   -- 每日快照的 SQL 索引
```

`daily` 记**执行结果**（响了几次、有没有确认）；`schedule` 记**那天的安排**。
`sync` 时从快照目录补齐 `schedule`，因此它永远跟着快照走。

```bash
python rtc.py metrics schedule                 # 覆盖了多少天
python rtc.py metrics schedule --date 2026-12-15
```

**定位：只读旁路**

- 输入只读 `state/alarms.json`
- 输出只写自己的 `metrics.db`
- **挂了不影响提醒**。调用方应把它包在「失败即跳过」里，不重试、不修它

两个落账时点：日切后 `sync past`（把已终态的过去存下来）、总结时 `sync`（兜底补齐今天）。这样即使某天总结没跑，只要 14 天内开过一次机，历史也能补上。

## 为什么不用 TypeScript 编译

这份代码约 230 行，执行者是 agent 或调度器，只会「照一条命令跑」。多一个构建步骤就多一个会出错的地方。JSDoc + `@ts-check` 已经能拿到类型检查，运行时零构建。

将来如果它长成一个真正的 CLI（多子命令、要发布、要被别人安装），再按标准形态上 TypeScript + `tsc` → `dist/`。

## 尚未提供的部分

**提醒主链路的调度代码刻意不在这里**。理由见 `ARCHITECTURE.md` 第四节：它属于运行时适配层，不同平台的 payload 种类、会话目标、投递方式都不同。请按 `platform/` 下的示例自行实现，或用运行时的 `command` 类载荷调用你自己的 `due` 脚本。
