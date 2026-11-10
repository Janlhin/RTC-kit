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

## 两条触发器

```
hermes cron create "0 6 * * *"  --name rtc-plan --no-agent --script rtc_plan.py \
      --deliver local --failure-deliver <target> --catch-up on  --timezone <TZ>

hermes cron create "*/5 * * * *" --name rtc-due  --no-agent --script rtc_due.py \
      --deliver <target> --failure-deliver <target> --catch-up off --timezone <TZ>
```

- `rtc-plan` 成功时**不输出**（静默），只有失败才经由 `--failure-deliver` 通知
- `rtc-due` 的 `--catch-up off` 是刻意的：机器关机数小时后开机，不需要补一串过期提醒
- 任务条数恒定两条，任何流程只 `edit`，不重新 `create`

## 入口脚本

`--script` 只接受 `~/.hermes/scripts/` 下的相对文件名，且不传参数。所以用一个三行的包装器：

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rtc
raise SystemExit(rtc.main(["due"]))
```

工作区通过环境变量 `RTC_WORKSPACE` 指定，包装器里可以直写，也可以用 Hermes 的 profile 环境。

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

表现是「任务 `completed`，但用户什么都没收到」。对策见 `AUDIT-2026-11-09-RESPONSE.md` 第 7 条：给主链路加兜底通道，或把投递失败做成可见信号。

## 验收顺序

1. 两条任务都先 `--deliver local`，跑两天，只看文件证据
2. 核对：唤醒次数、实际输出次数、`alarms.json` 的 `fireCount` 三者一致
3. 再切真实渠道，**人为确认至少一条真到达**
4. 最后才打开第二个窗口
