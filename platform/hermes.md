# hermes.md — 以 Hermes Agent 为例

> 适配示例，不是安装说明。字段名与命令随版本变化，以你所装版本的 `hermes <cmd> --help` 为准。
> 本文所有值都是占位符或通用描述。

## 为什么 Hermes 比「command payload + 静默哨兵」更省事

`hermes cron create --no-agent --script x.py` 是原生能力：

| 需要的能力 | OpenClaw 的做法 | Hermes 的做法 |
|---|---|---|
| 定时执行但**不调模型** | `command` / `--command-argv` payload | `--no-agent` |
| 静默（不投递） | 输出 `NO_REPLY` 哨兵，靠平台吞掉 | **空 stdout 即静默**，无需哨兵 |
| 投递 | 平台侧配置 | `--deliver <target>` |
| 脚本位置 | 任意 | 必须在 `~/.hermes/scripts/` 下 |
| 停机补跑 | 依赖平台语义 | `--catch-up on/off` 显式声明 |

「空输出即静默」这一条消掉了整个 `NO_REPLY` 风险面：不存在「哨兵写进了该响的分支、任务照跑但用户什么都没收到」这种失败。

## 三条触发器

前两条是**确定性链路**（`--no-agent`，不调模型）；第三条是**早报的模型生成**。

```
hermes cron create "0 6 * * *"  --name rtc-plan --no-agent --script rtc_plan.py \
      --deliver <target> --failure-deliver <target> --catch-up on  --timezone <TZ>

hermes cron create "*/5 * * * *" --name rtc-due  --no-agent --script rtc_due.py \
      --deliver <target> --failure-deliver <target> --catch-up off --timezone <TZ>

hermes cron create "0 9 * * *"  --name rtc-report --script rtc_report.py \
      --skill alarm-clock --deliver <target> --failure-deliver <target> \
      --catch-up off --timezone <TZ>
```

- `rtc-plan` 正常时**不输出**，所以接真实渠道也不会每天响一次；只有自检发现异常才推一条（见下节）
- `rtc-due` 的 `--catch-up off` 是刻意的：机器关机数小时后开机，不需要补一串过期提醒
- `rtc-report` **不带 `--no-agent`**：脚本 stdout 注入模型 prompt，模型据此写「汇报」并直接投递。这是 issue #60 的方案 B，**突破了「任务条数恒定两条」的旧约束**（用户已批准）

### 早报为什么是三条而不是两条

「9:00 现场叫模型」和「模型失败也不能不响」这两个要求互相冲突——模型不在线时，`rtc-report` 会失败，什么都不会发。解法是把兜底放在**已经存在的 due 机制**里，而不是再加第四条 cron：

| 时间 | 谁 | 做什么 |
|---|---|---|
| 09:00 | `rtc-report`（agent 模式） | 读 `rtc_report.py` 吐的素材，写 `state/reports/<日期>.md` 并投递；投完后调 `delivery-record --window report --result ok` 留回执 |
| 09:10 | `brief-morning-fallback` 窗口（`format: report-fallback`） | 当天有 **`result=ok` 的投递回执** → 没响过记 `skipped`（原因 `covered_by_receipt`）；已经响过则记 `delivered`（迟到回执只能止损，不能抹掉已发文本）；静默。**没有回执** → 有文件补发文件内容，没文件发静态汇报 |

时刻表里**只配一个**兜底窗口：

> 截止点以**第一次实际兜底判定**为准：09:10 是窗口起点，不是墙钟硬截止。09:10:30 写好的汇报会被 09:11 的第一次 tick 使用；之后再写的，当天不再采用。

```jsonc
{ "id": "brief-morning-fallback", "format": "report-fallback", "start": "09:10", "end": "09:14", ... }
```

**不要**同时再配一个 `format: report` 的 9:00 窗口——`plan` 会直接拒绝，因为两个窗口各发一次会让同一份汇报出现两遍（外部审计 2026-11 实测到过）。两种模式二选一：

| 模式 | 时刻表窗口 | 谁出稿 |
|---|---|---|
| 单窗口（不跑 agent cron） | `format: report` | `due` 直接读文件或拼静态版，一次搞定 |
| 双窗口（跑 `rtc-report`） | `format: report-fallback` | 09:00 模型写 + 投递、09:10 只在没回执时补一枪 |

**压住兜底的唯一证据是投递回执，不是文件存在。** 文件只能证明模型写完了，不能证明用户收到了；写完了但发送失败时若静默，用户会整天没有汇报。

兜底窗口是技术性守卫，**不计入**「今日闹钟 N 个窗口」的统计（`_window_tally` 会跳过它）。
- 任务条数**装完就固定**（默认两条；启用早报模型生成后三条），任何流程只 `edit`，不重新 `create`

## 入口脚本

`--script` 只接受 `~/.hermes/scripts/` 下的相对文件名，且**不传参数**，cwd 也在那里。所以用一个三行的包装器，把工作区写死在脚本里：

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rtc
raise SystemExit(rtc.main(["--workspace", r"C:\path\to\workspace", "due"]))
```

工作区不要靠 cwd 推断——Hermes 从 `scripts/` 目录执行，靠 cwd 会找不到 `rules/routines.json`，表现为任务 `failed` 而输出是一行错误路径。

## 自检与心跳

`rtc.py due` 每次进入都会写 `state/heartbeat.json`，但**两个字段的时机不同**：

| 字段 | 何时写 | 含义 |
|---|---|---|
| `attemptAt` | 一进入 `due` 就写 | 进程被调用过（失败也留痕） |
| `lastOkAt` | 判定完整走完才写（含正确判为静默、写入冲突静默） | 最近一次成功判定 |
| `at` | 与 `lastOkAt` 同步 | 旧字段，兼容旧版 `rtc.py` 和外部脚本 |

这样区分了「调过但没判完」与「判完了」。状态文件损坏时 `due` 一进入就写 `attemptAt`、随后失败退出，`lastOkAt` **不会**被推进——心跳不再假装新鲜。旧格式（只有 `at`）会被当作一次成功判定读取，向前兼容。

外部看门狗（与 RTC 不同宿主、不同调度器、不同投递渠道的每日检查）应读 `lastOkAt`：超过 24 小时即告警。只核对 `attemptAt` 或 `at` 会把「反复被调用却一直失败」误判为健康。

`rtc.py plan` 在日切后读心跳，只在发现异常时向 **stdout** 输出：

| 信号 | 含义 |
|---|---|
| `lastOkAt` 超过 60 分钟 | 最近一次成功判定太久之前，这段时间的窗口可能没响 |
| 昨天某窗口 `missed` 且 `fires == 0` | 该窗口整段没有一次触发，调度器当时不在 |
| 没有成功判定心跳（非首次运行，且 `attemptAt` 也已陈旧） | `rtc-due` 可能因状态损坏反复失败，从未完整跑完一次判定 |

没有异常时一个字符都不打印，因此 `rtc-plan` 可以设成 `--deliver <真实渠道>`：平时静默，出问题才发一条。这正是「空输出即静默」的正确用法——用异常文本当告警，不需要额外的告警通道。

`rtc.py status` 会分别打印「上次成功判定」与「其后是否又调过」两个时间，用于人工排查「调了但没判完」。

## 停摆恢复（issue #94）

宿主长时间不被调用时，`attemptAt` 停止刷新。重启后第一个 `due` 发现 `attemptAt` 陈旧超过两个 tick（10 分钟），判定为「刚从停摆恢复」，确定性地做三件事：

1. 把旧 `planDate` 与今天之间错过的窗口归档为 `missed`（原因 `host_outage`），写进 `state/alarms.json` 的 history 与 `state/history.db` 的 `daily` 表；暂停期与显式禁用日不算漏响。
2. 补建当天计划（沿用既有 `plan` 路径）。
3. stdout 输出**一条**恢复提示：停摆时长、上次运行时刻、错过几个窗口。

同一停摆以停摆前最后一次 `attemptAt` 为身份，事件表 `events` 记一条 `kind=recovery`；重复核对不会二次归档、不会重复投递。健康心跳上不会进入这条路径。

宿主重启钩子可以显式触发一次核对（不必等下一个 5 分钟 tick）：

```bash
python rtc.py --workspace <dir> due --bootstrap
```

`--bootstrap` 只是不等下一个 cron tick、现在就跑核对；心跳新鲜说明宿主一直在跑，不会伪造停摆。

## 已验证的事实（2026-11，Hermes v0.23.0 / Windows）

- `--no-agent` 任务在没有任何模型凭据的情况下也能跑；stdout 原样成为投递内容，空 stdout 不投递
- `hermes cron run <id>` 可手动触发一次；`hermes cron runs <id>` 有持久记录（`completed` / `failed`）
- 网关未运行时 cron 不会触发；`hermes cron status` 会明确告警「没有 scheduler 服务这个 profile」
- 脚本用随包 Python 执行（3.14），标准库足够，**不需要 Node**
- 包装器里必须 `sys.stdout.reconfigure(encoding="utf-8")`（已在 `rtc.py` 内处理），否则中文在 Windows 控制台编码下可能抛错

## 投递目标的一个坑（微信 / iLink）

若 `--deliver` 指向个人微信（iLink bot），**主动推送要求先有入站会话**。刚配对的 bot 没有 `context_token`，发送会返回：

```
iLink sendmessage session not ready: ret=-2 errmsg=prepare failed
```

表现是「任务 `completed`，但用户什么都没收到」。对策见 `../docs/audit/2026-11-09-response.md` 第 7 条：给主链路加兜底通道，或把投递失败做成可见信号。

## 验收顺序

1. 两条任务都先 `--deliver local`，跑两天，只看文件证据
2. 核对：唤醒次数、实际输出次数、`alarms.json` 的 `fireCount` 三者一致
3. 再切真实渠道，**人为确认至少一条真到达**
4. 最后才打开第二个窗口
