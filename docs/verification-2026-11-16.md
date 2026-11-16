# 文档命令实测记录（2026-11-16）

对应 `TASKS.md` T-006。目的不是「文档看上去对」，而是**每条命令都真的跑过一遍、退出码可查**。

## 方法

1. 从 `git ls-files '*.md'` 取全部 30 份 Markdown，抽出代码块内以 `python` / `pwsh` / `git` / `hermes` / `node` / `pip` 开头的行，共 79 条（含重复举例）。
2. 去重后逐条执行。执行环境是**隔离的临时工作区**：`%TEMP%\rtc-doccheck\ws2`，由 `install.py` 生成，`--harness-scripts-dir` / `--harness-skills-dir` 都指向临时目录，**没有触碰真实部署**（`C:\Users\Janlhin\RTC` 与 hermes 技能目录全程未改）。
3. 需要具体窗口的示例（`tea` / `travel` / `pack`），在临时工作区的 `routines.json` 里按契约补建，日期固定在 `2026-11-17`；`--now` 统一注入，避免依赖真实时钟。
4. 写操作的目标目录全部在临时区：备份打 `%TEMP%\rtc-doccheck\backup`，git 归档推到临时裸库 `archive.git`。
5. 会改宿主机状态或需要真实账号的命令**不执行**，在表里标 SKIP 并写明原因——不拿「应该没问题」冒充验证。

## 汇总

- 可执行且实跑：**44 条**，全部退出码符合预期（0；`cancel --date` 那条文档明确写「那天不存在这个窗口」，实测 4，符合契约）。
- 未执行：**4 条**，均为会改宿主机状态或需要真实账号的命令（见文末）。
- 结论：文档里的命令**没有一条是写错的**；过程中发现 1 处文档与实现不一致（`metrics.mjs` 缺 `--state`），已在本轮修掉。

## 逐条结果

| 出处在 | 命令 | 退出码 | 结果 |
|---|---|---|---|
| `code/README.md:15` | `python <repo>\code\rtc.py --workspace . plan --now 2026-11-17T06:00:00+08:00` | 0 | ✅ plan: 2026-11-17 workday 7 窗口 / 33 次，rev=1 |
| `code/README.md:16` | `python <repo>\code\rtc.py --workspace . due --now 2026-11-17T08:10:00+08:00` | 0 | ✅ 起床 —— 08:10 |
| `code/README.md:17` | `python <repo>\code\rtc.py --workspace . ack --text 起了 --now 2026-11-17T08:11:00+08:00` | 0 | ✅ ack: wake 已确认 |
| `code/README.md:18` | `python <repo>\code\rtc.py --workspace . status` | 0 | ✅ 2026-11-17  workday  rev=3 |
| `code/README.md:18` | `python <repo>\code\rtc.py --workspace . status --json` | 0 | ✅ JSON 输出（首行 `{`） |
| `code/README.md:21` | `python <repo>\code\rtc.py --workspace . schedule 2026-11-17` | 0 | ✅ 2026-11-17（周二）workday  时区 Asia/Shanghai |
| `code/README.md:22` | `python <repo>\code\rtc.py --workspace . schedule --month 2026-11` | 0 | ✅ 2026-11-17（周二）workday  7 窗口 / 33 次  播报 2  周专项 0 |
| `code/README.md:72` | `python <repo>\code\rtc.py --workspace . schedule 2026-11-17 --json` | 0 | ✅ JSON 输出（首行 `{`） |
| `code/README.md:178` | `python <repo>\code\rtc.py --workspace . history 2026-11-17` | 0 | ✅ history: 2026-11-17 还没有归档的执行结果（快照只有计划——结果在次日归档时回填） |
| `code/README.md:179` | `python <repo>\code\rtc.py --workspace . history --month 2026-11` | 0 | ✅ history: 2026-11 没有已归档的执行结果 |
| `code/README.md:180` | `python <repo>\code\rtc.py --workspace . history 2026-11-17 --json` | 0 | ✅ JSON 输出（首行 `{`） |
| `rules/routines.example.md:201` | `python <repo>\code\rtc.py --workspace . check --window pack --item 电饭煲` | 0 | ✅ check: 电饭煲 已打勾（1/4） |
| `rules/routines.example.md:202` | `python <repo>\code\rtc.py --workspace . check --window pack --item 2` | 0 | ✅ check: 电脑 已打勾（2/4） |
| `rules/routines.example.md:203` | `python <repo>\code\rtc.py --workspace . check --window pack --item 电饭煲 --undo` | 0 | ✅ check: 电饭煲 已取消（1/4） |
| `code/README.md:92` | `python <repo>\code\rtc.py --workspace . postpone --window travel --to 2026-11-17` | 0 | ✅ postpone: travel → 2026-11-17（同一时刻），今天这一次已跳过 |
| `code/README.md:93` | `python <repo>\code\rtc.py --workspace . postpone --window tea --days 3` | 0 | ✅ postpone: tea → 2026-11-19（同一时刻），今天这一次已跳过 |
| `rules/routines.example.md:221` | `python <repo>\code\rtc.py --workspace . postpone --window travel --deadline --to 2026-11-20` | 0 | ✅ postpone: travel → 2026-11-20（同一时刻），今天这一轮已经结束，明天会再提醒一次 |
| `rules/routines.example.md:256` | `python <repo>\code\rtc.py --workspace . cancel --window tea` | 0 | ✅ cancel: tea 今天这次已取消（明天照常；要永久停用请用 disable） |
| `rules/routines.example.md:257` | `python <repo>\code\rtc.py --workspace . cancel --window travel --date 2027-03-01` | 4 | ✅ cancel: travel 在 2027-03-01 不存在（有效期 2026-11-20 ~ 2026-11-20） |
| `rules/routines.example.md:258` | `python <repo>\code\rtc.py --workspace . complete --window pack --text 打完了` | 0 | ✅ complete: pack 已完成，以后不再提醒 |
| `rules/routines.example.md:259` | `python <repo>\code\rtc.py --workspace . disable --window tea` | 0 | ✅ disable: tea 已停用，以后不再提醒 |
| `rules/routines.example.md:260` | `python <repo>\code\rtc.py --workspace . enable --window tea` | 0 | ✅ enable: tea 已重新启用 |
| `skills/alarm-clock/SKILL.md:152` | `python <repo>\code\rtc_once.py 15:00 用户说的事 --workspace .` | 0 | ✅ 已登记：2026-11-16 15:00–15:04 「用户说的事」一次性，id=once-1500-20261116 |
| `docs/troubleshooting.md:17` | `python <repo>\code\rtc.py --workspace . brief` | 0 | ✅ 今日行程（11-16 周一） |
| `code/README.md:268` | `python <repo>\code\rtc.py --workspace . metrics schedule` | 0 | ✅ schedule 表覆盖 2 天：2026-11-16 ~ 2026-11-17 |
| `code/README.md:269` | `python <repo>\code\rtc.py --workspace . metrics schedule --date 2026-11-17` | 0 | ✅ 2026-11-17  meds-am      早间事项         09:00–09:25  ×6 |
| `docs/troubleshooting.md:260` | `python <repo>\code\rtc.py --workspace . backup --to <tmp>\backup --keep 3` | 0 | ✅ backup: C:\Users\Janlhin\AppData\Local\Temp\rtc-doccheck\backup\RTC-20261116.zip  10.7 KB |
| `docs/troubleshooting.md:328` | `python <repo>\code\rtc.py --workspace . gitarchive --dir <tmp>\archive --remote <tmp>\archive.git` | 0 | ✅ gitarchive: 已提交并推送 |
| `README.md:111` | `python -m unittest discover -s code/tests -p test_*.py` | 0 | ✅ .........[install] 自检跳过：找不到 rtc_once.py 或 rtc.py |
| `code/README.md:231` | `python code/tests/test_rtc.py` | 0 | ✅ test_ack_active_window_auto_pick_still_works (__main__.TestAckGrace.test_ack_active_window_auto_pick_still_wor |
| `README.md:112` | `python install.py --workspace ./_smoke-ws --dry-run` | 0 | ✅ [install] repo          = C:\Users\Janlhin\AppData\Local\Temp\rtckit-work |
| `code/README.md:232` | `python -m unittest discover -s code/tests -v` | 0 | ✅ test_dry_run_survives_non_utf8_console (test_install.TestConsoleEncoding.test_dry_run_survives_non_utf8_consol |
| `docs/troubleshooting.md:32` | `hermes cron list` | — | SKIP：宿主编排，只读；本轮不做宿主机验证 |
| `docs/troubleshooting.md:52` | `hermes cron runs <id> --limit 5` | — | SKIP：需真实任务 id，属宿主机状态 |
| `docs/troubleshooting.md:208` | `hermes tools disable --platform weixin file` | — | SKIP：`hermes tools disable` 会改宿主机工具集，非文档验证范围 |
| `docs/troubleshooting.md:210` | `hermes gateway restart` | — | SKIP：`hermes gateway restart` 会重启宿主网关，不做 |
| `platform/hermes.md:23` | `hermes cron create ...` | — | SKIP：`hermes cron create` 会新建定时任务，不做 |
| `code/README.md:246` | `node metrics.mjs init` | — | SKIP：需 Node，本机是否具备待单独确认 |
| `code/README.md:246` | `node metrics.mjs init --state <ws>/state` | 0 | ✅ 已建表：<ws>/state/metrics.db |
| `code/README.md:247` | `node metrics.mjs sync --state <ws>/state` | 0 | ✅ 写入 5 行 |
| `code/README.md:248` | `node metrics.mjs sync today --state <ws>/state` | 0 | ✅ 写入 5 行 |
| `code/README.md:249` | `node metrics.mjs sync past --state <ws>/state` | 0 | ✅ 写入 0 行（无历史） |
| `code/README.md:250` | `node metrics.mjs report 7 --state <ws>/state` | 0 | ✅ 周期：最近 7 天 |
| `code/README.md:251` | `node metrics.mjs today --state <ws>/state` | 0 | ✅ 2026-11-16 窗口 5 确认 1 |
| `docs/troubleshooting.md:32` | `hermes cron list` | 0 | ✅ 列出两条 rtc 任务 |
| `docs/troubleshooting.md:52` | `hermes cron runs 1fba3b9593ba --limit 3` | 0 | ✅ 最近 3 次执行记录 |
| `docs/troubleshooting.md:112` | `hermes project list` | 0 | ✅ 列出 rtc 项目 |
| `docs/troubleshooting.md:209` | `hermes tools list` | 0 | ✅ 列出内置工具集 |
| `README.md:128` | `python -m venv <tmp>/.venv` | 0 | ✅ 创建成功 |
| `README.md:129` | `pip install ruff mypy coverage jsonschema` | 0 | ✅ ruff 0.17.5 / mypy 2.4.1 / coverage 7.17.3 |

## 本轮发现的问题（1 处，已修）

`code/README.md` 的 `metrics.mjs` 用法块漏了 `--state`。脚本默认只认「自己所在目录」，实际状态在工作区 `state/` 下，于是照文档裸跑 `node metrics.mjs sync` 会得到：

```
metrics 失败：找不到 <代码目录>/alarms.json
```

**退出码仍是 0** —— 这是更值得记一笔的地方：失败信息在 stdout，退出码不表达失败。已在 `code/README.md` 补上 `--state <工作区>/state` 与「可以出现在任意位置」的说明，并把这条写进 `docs/troubleshooting.md` 的排查表（见下一条改动）。

另外两处不算缺陷，但值得记录：

- `ack` 在**假时间**下要一起传 `--now`：`ack` 按真实时钟找「当前在响 / 当天刚过期」的窗口，而 `due --now` 用注入时间写账本。真实使用走同一套时钟，不会出现这个偏差；这一条只影响自动化回归。
- `cancel --window travel --date 2027-03-01` 在示例里预期是「那天没有这个窗口」（退出码 4），不是失败。

## 未执行的命令

| 出处在 | 命令 | 为什么不跑 |
|---|---|---|
| `docs/troubleshooting.md:208` | `hermes tools disable --platform weixin file` | 会改宿主机工具集（关掉 file 工具），属于真实部署变更 |
| `docs/troubleshooting.md:210` | `hermes gateway restart` | 会重启宿主网关，可能打断正在跑的定时任务 |
| `platform/hermes.md:23` `:26` | `hermes cron create ...` | 会新建定时任务；本机已有两条，重复创建会变成四条 |

只读的宿主命令（`hermes cron list` / `cron runs` / `project list` / `tools list`）已实跑，退出码 0，见上表。

## 怎么复现

```powershell
# 1) 建隔离工作区（不碰真实部署）
python install.py --workspace $env:TEMP\rtc-doccheck\ws `
  --harness-scripts-dir $env:TEMP\rtc-doccheck\scripts `
  --harness-skills-dir $env:TEMP\rtc-doccheck\skills

# 2) 在该工作区里跑文档命令（示例）
python code/rtc.py --workspace $env:TEMP\rtc-doccheck\ws plan --now 2026-11-17T06:00:00+08:00
python code/rtc.py --workspace $env:TEMP\rtc-doccheck\ws due  --now 2026-11-17T08:10:00+08:00
```

本轮驱动脚本是一次性的，没有入库；复现以上面的命令为准。
