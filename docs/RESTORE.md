# 恢复流程（RESTORE）

**什么时候用**：`state/alarms.json` 被删或写坏、换电脑、硬盘坏了一块、误把 `rules/routines.json` 改乱之后。

**一句话原则**：先验证「这份备份是我的」，再验证「这份备份没坏」，最后才盖回去。顺序颠倒等于把别人的作息盖到自己头上。

> **⚠ 演练 / 验收前的强制前置动作**
>
> 做任何迁移、恢复、回滚演练之前，**必须先把 `rules/backup.json` 里的 `targets` 改到一个临时目录**（例如仓库外的 `_acceptance/.../bk`），确认不是真实备份盘后再跑。
>
> 红线：**不碰 `C:\Users\Janlhin\RTC` 真实工作区，不写 D:/E: 备份盘**。备份配置示例（临时目录，用完即删）：
>
> ```json
> {
>   "targets": ["C:/Users/Janlhin/DoubaoWork/chats/2026-11-27/new-chat/rtc-work/_acceptance-p7/bk"],
>   "keep": 30
> }
> ```

---

## 0. 备份长什么样

每天 `rtc-plan` 成功后会顺手打一份 zip，落在 `rules/backup.json` 里写的 `targets` 上（**不写源盘**，同盘不算备份）：

```
{{TARGET_DIR_1}}/RTC-YYYYMMDD.zip
{{TARGET_DIR_2}}/RTC-YYYYMMDD.zip
```

zip 里是这些文件（缺哪个说明那天那个文件还不存在，不是坏了）：

| 文件 | 作用 |
|---|---|
| `rules/routines.json` | 时刻表本体 |
| `rules/backup.json` | 备份配置（目标盘、保留份数、git 归档） |
| `state/alarms.json` | 当天的窗口与执行状态（`rev` 在这里） |
| `state/routine-seal.json` | 时刻表印章（防手改） |
| `state/metrics.sql` / `state/metrics.db` | 执行流水 |
| `state/schedule/YYYY-MM-DD.json` | 每日永久快照（一天一个） |
| `AGENTS.md` / `tasks.md` | 模型读的行程与任务 |

每个目标盘保留最近 `keep` 份（默认 30），更早的靠 `state/schedule/` 里的快照本身留住。

---

## 1. 第 0 步：这份 zip 是不是「我的」

**必须做**。备份能写出来，不等于备份可信——测试沙箱曾经也往同一批目录写过（见 issue #48，已修，但旧文件还在）。

```powershell
# 看文件名对应的日期，和最后修改时间对不对得上
Get-ChildItem -LiteralPath "{{TARGET_DIR_1}}" -Filter "RTC-*.zip" | Select-Object Name, Length, LastWriteTime
```

再打开 zip 看里面两个关键数字：

```powershell
$zip = "{{TARGET_DIR_1}}/RTC-20261116.zip"
Add-Type -AssemblyName System.IO.Compression.FileSystem
$z = [IO.Compression.ZipFile]::OpenRead($zip)
$e = $z.Entries | Where-Object { $_.FullName -eq "state/alarms.json" }
$sr = New-Object IO.StreamReader($e.Open()); $alarms = $sr.ReadToEnd() | ConvertFrom-Json; $sr.Close()
$e2 = $z.Entries | Where-Object { $_.FullName -eq "rules/routines.json" }
$sr2 = New-Object IO.StreamReader($e2.Open()); $routine = $sr2.ReadToEnd() | ConvertFrom-Json; $sr2.Close()
$z.Dispose()
"planDate = $($alarms.planDate)   rev = $($alarms.rev)   窗口数 = $($alarms.windows.Count)"
"时刻表窗口 = " + (($routine.alarmWindows | ForEach-Object { $_.id }) -join ", ")
```

**对得上**的样子：

- `planDate` 等于文件名里的日期（`RTC-20261116.zip` → `2026-11-16`）
- `alarms.windows` 里的 `label` 是你认得的那些提醒
- `routines.alarmWindows` 的 `id` 与 `alarms.windows` 对得上（`wake` ↔ 起床，`once-*` ↔ 一次性条目）

对不上（比如 `planDate` 是 5 月、窗口是 `tea / travel / pack` 这种你没设过的）→ **这份不是你的，换下一份**。

---

## 2. 验证 zip 本身没坏

zip 写入时会回读做 CRC 校验，所以解压报错就是真坏了：

```powershell
Expand-Archive -LiteralPath "{{TARGET_DIR_1}}/RTC-20261116.zip" -DestinationPath "{{TEMP_DIR}}/rtc-restore" -Force
```

`Expand-Archive` 不报错 = 全部条目 CRC 通过。想留个凭据就把哈希记下来：

```powershell
Get-FileHash -LiteralPath "{{TARGET_DIR_1}}/RTC-20261116.zip" -Algorithm SHA256
```

（zip 里没有 manifest，所以哈希只能事后对账，不能事前校验——这条限制写在这里，别假装有。）

---

## 3. 先当只读副本跑一遍（不覆盖任何东西）

这一步是关键：把解出来的东西**当成一个独立工作区**，看它是不是自洽。

```powershell
python {{CODE_DIR}}/rtc.py --workspace {{TEMP_DIR}}/rtc-restore status
python {{CODE_DIR}}/rtc.py --workspace {{TEMP_DIR}}/rtc-restore history --month 2026-11
python {{CODE_DIR}}/rtc.py --workspace {{TEMP_DIR}}/rtc-restore schedule --month 2026-11
```

**该看到什么**：

- `status` 打出 `planDate` = zip 日期，`rev` 与第 0 步读到的一致，窗口数一致
- `history --month` 打出那几天的「N 项，完成 X，未成 Y」
- `schedule --month` 每天一行，没有空洞

跑不起来 / 数字对不上 → 停，别覆盖。**宁可退回再上一份 zip。**

---

## 4. 覆盖恢复

顺序很重要：先让宿主别在恢复途中重新排版。

```powershell
# ① 恢复前先留一份现状（万一恢复错了还能回去）
Copy-Item -LiteralPath "{{WORKSPACE_DIR}}/state/alarms.json" -Destination "{{TEMP_DIR}}/before-restore-alarms.json"
Copy-Item -LiteralPath "{{WORKSPACE_DIR}}/rules/routines.json" -Destination "{{TEMP_DIR}}/before-restore-routines.json"

# ② 盖回去（只盖这两类，别整目录覆盖）
Copy-Item -Recurse -Force "{{TEMP_DIR}}/rtc-restore/state" "{{WORKSPACE_DIR}}/"
Copy-Item -Force "{{TEMP_DIR}}/rtc-restore/rules/routines.json" "{{WORKSPACE_DIR}}/rules/"
Copy-Item -Force "{{TEMP_DIR}}/rtc-restore/rules/backup.json" "{{WORKSPACE_DIR}}/rules/"

# ③ 立刻确认
python {{CODE_DIR}}/rtc.py --workspace {{WORKSPACE_DIR}} status
```

**不要**在早上 6 点前后（`rtc-plan` 要跑）做这件事：`plan` 会把恢复出来的状态按「今天」重排，
`rev` 会往前走，你就分不清哪部分是恢复的、哪部分是重排的。要么在 06:00 之前做完，要么用
`plan --force` 重排一次并在 `status` 里确认窗口列表符合预期。

---

## 5. 恢复后对账

三件事对上才算恢复成功：

1. `status` 的 `rev` 与备份里的一致（或只多了 `plan` 推的那一次）
2. `backup` 那一行显示「尚未备份」也不必慌——当天还没到备份时刻
3. `history --month <当月>` 能查到恢复前的日子（快照在 `state/schedule/` 里，没被覆盖）

---

## 6. 演练记录（2026-11-17，真实执行）

用真实备份 `{{TARGET_DIR_1}}/RTC-20261116.zip` 解到一个临时目录当只读工作区：

```
2026-11-16  workday  rev=70
  起床       07:00-07:25  skipped  0/6
  例行事项 A   08:30-08:55  skipped  0/6
  例行事项 B   12:00-12:25  missed   5/6
  例行事项 C   18:30-18:55  pending  0/6
  【安装自检】链路测试 15:25-15:29  delivered 1/1
  ...
  问CJY是否打OW 20:00-20:04  pending  0/1
```

`history --month 2026-11` 打出 `2026-11-14（周六）5 项，完成 2，未成 3`——快照与状态都在。
结论：**按第 1–3 步验证过的 zip 可以直接用于恢复**，不需要额外工具。

同一轮演练还发现：`D:/E` 上另外两份 zip（`RTC-20260510.zip`、`RTC-20261117.zip`）的内容与实机工作区对不上，
是测试沙箱写进来的假备份。它们**不可用于恢复**。污染源已修（issue #48 / PR #49）。

---

## 7. 恢复不了怎么办

| 情况 | 处理 |
|---|---|
| 所有 zip 都坏了 | `state/schedule/*.json` 是每日快照，能拿回「每天该有什么」，但拿不回 `rev` 与投递记录 |
| 快照也没了 | 数据真丢了。重建方式：按 `rules/routines.md` 重写时刻表 → `plan --force`，从今天重新开始记账 |
| 只有 `rules/routines.json` 坏了 | 不用整份恢复：从任一 zip 里只取这一个文件盖回 `rules/`，再 `plan --force` |
| 想预防下一次 | 打开 git 归档（`rules/backup.json` 里的 `git` 段），每天会自动 commit 到私有仓，多一层异地副本 |

---

## 8. L2 `.ics` 迁移的备份 / 恢复 / 回滚

旧 `rules/routines.json` + `state/alarms.json` 迁到 `.ics` 真相源时，迁移工具自带一套「写前备份 → 幂等写入 → 按清单回滚」流程，与第 1–4 节的 zip 备份是两条独立链路。

### 8.1 迁移前备份

`--to-ics` 默认是 dry-run（只打印计划，不写盘）。真跑加 `--apply`，并用 `--backup-dir` 指到**临时目录**：

```powershell
python code/rtc_migrate.py --workspace <临时工作区> --to-ics --apply --backup-dir <临时备份目录>
```

- 备份目录下按时间戳建子目录，里面是 `routines.json.snapshot`、`alarms.json.snapshot` 与一份 `manifest.json`。
- `manifest.json` 记录每个源文件迁移前的 SHA256、`calendar/` 当时是否存在、以及 `restored` 标记；回滚就按这份清单对账。
- 迁移**不删旧文件**；旧 `routines.json` / `alarms.json` 原样留在原处。

### 8.2 迁移产物与可重建性

真跑后工作区里会有：

| 产物 | 作用 |
|---|---|
| `calendar/<YYYY>.ics` | 事件真相源（一年一文件，含跨年交叉段） |
| `state/calendar-index.db` | SQLite 只读索引，可由 `.ics` 从零 `rebuild` |
| `state/events.json` | 影子派生视图，由 `.ics` 重建；**不反向回写** `.ics` 或旧文件 |

索引或影子被删都不怕：从 `.ics` 重建索引 + 影子后，查询结果与删前逐条一致（见 `docs/l2-acceptance.md` 验收项 2）。

### 8.3 回滚

对最近一次备份目录执行 `--rollback`：

```powershell
python code/rtc_migrate.py --workspace <临时工作区> --to-ics --rollback <临时备份目录>/<时间戳>-to-ics
```

- 回滚按 `manifest.json` 恢复：存在过的 `calendar/` 目录与派生索引 / 影子被清理，旧 `routines.json` / `alarms.json` 从快照还原。
- 输出 `{"restored": true, "calendar_existed": false, ...}` 表示回滚完成；`restored` 标记写回 manifest。
- 回滚后旧五条命令（`status --json` / `history <date>` / `schedule <date>` / `due --now` / `report`）退出码与迁移前一致。

### 8.4 幂等性

同一工作区连续跑两次 `--to-ics --apply`，第二次命中别名 / uid 已存在则跳过，不换 uid、不产生第二份，产物哈希不变。重复迁移不会把影子视图越写越偏。
