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
