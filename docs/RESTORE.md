# 恢复流程（RESTORE）

**什么时候用**：`state/alarms.json` 被删或写坏、换电脑、硬盘坏了一块、误把 `rules/routines.json` 改乱之后。

**一句话原则**：先验证「这份备份是我的」，再验证「这份备份没坏」，最后才盖回去。顺序颠倒等于把别人的作息盖到自己头上。

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

> 本节是把 zip 当**只读副本**试跑；真的删掉 `state/alarms.json` 再盖回的闭环演练见 6.1。

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

## 6.1 删除后恢复演练（2026-11-27 23:12 +08:00，真实执行，对应 T-011）

第 6 节是只读试跑；本节补上闭环：真的删掉 `state/alarms.json`，再按第 1–5 步盖回。
演练全程在隔离临时工作区，安装器脚本 / 技能 / 备份目标都指向临时目录，真实作息工作区零触碰。

**运行环境**：Windows（DESKTOP-5M9V04K），仓库 `.venv` Python 3.13.15；演练根目录
`%TEMP%\rtc-restore-drill-20261127`，跑完可整目录删除。原始 stdout / stderr 逐字留存于附录
[`docs/restore-evidence/2026-11-27/`](restore-evidence/2026-11-27/MANIFEST.txt)（每条命令一个
`*.stdout.txt` / `*.stderr.txt`，`.meta.txt` 记退出码与耗时）；本节只贴关键原文。

**记号**：`<WS>` 临时工作区，`<TARGET>` 临时备份目录，`<RESTORE>` 临时解压目录，
`<CODE_DIR>` 仓库 `code/`，`<ROOT>` 演练根目录。

### 6.1.1 可复现命令序列

```powershell
# 0. 临时目录（全部在 %TEMP% 下，不碰真实工作区）
$ROOT = Join-Path $env:TEMP 'rtc-restore-drill-20261127'
$WS = Join-Path $ROOT 'ws'
$TARGET = Join-Path $ROOT 'target'
$RESTORE = Join-Path $ROOT 'restore'
$HSCRIPTS = Join-Path $ROOT 'harness-scripts'
$HSKILLS = Join-Path $ROOT 'harness-skills'
New-Item -ItemType Directory -Force $WS,$TARGET,$RESTORE,$HSCRIPTS,$HSKILLS | Out-Null

# 1. 安装器初始化工作区（脚本 / 技能重定向到临时目录）
python install.py --workspace $WS --harness-scripts-dir $HSCRIPTS --harness-skills-dir $HSKILLS

# 2. 备份目标先改成 <TARGET>：安装器默认会扫 D:/E: 等非源盘写进 rules/backup.json 的
#    targets，不改的话第 3 步 plan 的自动备份会写到真机盘
$cfg = Get-Content (Join-Path $WS 'rules/backup.json') -Raw | ConvertFrom-Json
$cfg.targets = @($TARGET)
$cfg | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $WS 'rules/backup.json') -Encoding utf8

# 3. 固定时刻生成当天账本
python code/rtc.py --workspace $WS plan --now 2026-11-27T06:00:00+08:00

# 4. 基线：status / status --json，并算账本指纹
python code/rtc.py --workspace $WS status
python code/rtc.py --workspace $WS status --json
Get-FileHash -LiteralPath (Join-Path $WS 'state/alarms.json') -Algorithm SHA256

# 5. 备份到 <TARGET>，再算备份包指纹
python code/rtc.py --workspace $WS backup --to $TARGET
Get-FileHash -LiteralPath (Join-Path $TARGET 'RTC-20261127.zip') -Algorithm SHA256

# 6. 打开 zip 核对条目与关键数字（核对脚本 probe_zip.py 见附录目录）
python probe_zip.py (Join-Path $TARGET 'RTC-20261127.zip')

# 7. 解压到 <RESTORE> 做只读试跑
Expand-Archive -LiteralPath (Join-Path $TARGET 'RTC-20261127.zip') -DestinationPath $RESTORE -Force
python code/rtc.py --workspace $RESTORE status

# 8. 真删 <WS>/state/alarms.json，再 status（应当报尚无计划）
Remove-Item -LiteralPath (Join-Path $WS 'state/alarms.json') -Force
python code/rtc.py --workspace $WS status

# 9. 按第 4 步盖回：state 整个、routines.json、backup.json
Copy-Item -Recurse -Force (Join-Path $RESTORE 'state') (Join-Path $WS '')
Copy-Item -Force (Join-Path $RESTORE 'rules/routines.json') (Join-Path $WS 'rules/')
Copy-Item -Force (Join-Path $RESTORE 'rules/backup.json') (Join-Path $WS 'rules/')

# 10. 恢复后对账，并再算一次账本指纹
python code/rtc.py --workspace $WS status
python code/rtc.py --workspace $WS status --json
python code/rtc.py --workspace $WS history --month 2026-11
python code/rtc.py --workspace $WS schedule --month 2026-11
Get-FileHash -LiteralPath (Join-Path $WS 'state/alarms.json') -Algorithm SHA256
```

### 6.1.2 指纹（完整 64 位 SHA256，无截断、无省略号）

| 对象 | 路径 / 条目 | 大小 | SHA256 |
|---|---|---|---|
| 备份时刻账本 | `<WS>/state/alarms.json` | 2866 B | `FD10587C45115478A68065434A92708987478D76DA5FC1DF79A421B84D3FE281` |
| zip 内账本条目 | `RTC-20261127.zip!/state/alarms.json` | 2866 B | `fd10587c45115478a68065434a92708987478d76da5fc1df79a421b84d3fe281`（与上行同一字节串，差异仅为十六进制大小写） |
| 恢复后账本 | `<WS>/state/alarms.json` | 2866 B | `FD10587C45115478A68065434A92708987478D76DA5FC1DF79A421B84D3FE281` |
| 备份包 | `<TARGET>/RTC-20261127.zip` | 9085 B | `A77F16332E5F73CD7BA00B075D2BE484DF42E1E743AD55955D04AE4946FA5BF7` |

指纹命令原文（PowerShell，算的是哪个文件写在命令里）：

```powershell
Get-FileHash -LiteralPath (Join-Path $WS 'state/alarms.json') -Algorithm SHA256
Get-FileHash -LiteralPath (Join-Path $TARGET 'RTC-20261127.zip') -Algorithm SHA256
```

zip 内条目指纹由 Python 直读 zip 字节串计算（附录 `probe_zip.py`，`hashlib.sha256`）。

### 6.1.3 关键原始输出（逐字，未润色）

**plan（stderr，stdout 为空）**：

```
plan: 2026-11-27 workday 4 窗口 / 25 次，rev=1
```

**基线 status（stdout）**：

```
2026-11-27  workday  rev=1
  调度器心跳：无成功判定
  备份  11-27 06:00  本地 1 处 · git 未跑（这次只做本地） · 最近无错误
  起床       08:10-08:40  pending  0/7
  早间事项     09:00-09:25  pending  0/6
  午间事项     12:00-12:25  pending  0/6
  晚间事项     19:00-19:25  pending  0/6
```

**backup（stdout）**：

```
backup: C:\Users\Janlhin\AppData\Local\Temp\rtc-restore-drill-20261127\target\RTC-20261127.zip  8.9 KB（今天已存在，未覆盖）
```

> 说明：`plan` 成功后已自动打过一份同名 zip，手动 `backup --to` 这次看到的是
> 「今天已存在，未覆盖」；命令退出码仍为 0，并照常写 `state/.backup-last.json` 记录。

**zip 核对（probe_zip.py stdout）**：

```
ENTRIES=rules/routines.json,rules/backup.json,state/alarms.json,state/routine-seal.json,state/metrics.sql,state/metrics.db,AGENTS.md,tasks.md,state/schedule/2026-11-27.json,state/backups/routines.program-last.json
planDate=2026-11-27
rev=1
windows=4
alarmIds=wake,meds-am,meds-noon,meds-pm
routineIds=wake,meds-am,meds-noon,meds-pm
```

**只读试跑 status（`<RESTORE>`，stdout）**：

```
2026-11-27  workday  rev=1
  调度器心跳：无成功判定
  备份  尚未备份（python rtc.py archive backup 可手动跑一次）
  起床       08:10-08:40  pending  0/7
  早间事项     09:00-09:25  pending  0/6
  午间事项     12:00-12:25  pending  0/6
  晚间事项     19:00-19:25  pending  0/6
```

**删除账本后 status（stdout）**：

```
尚无计划。先跑：python rtc.py plan
```

**恢复后 status（stdout）**：

```
2026-11-27  workday  rev=1
  调度器心跳：无成功判定
  备份  11-27 23:12  本地 1 处 · git 未跑（这次只做本地） · 最近无错误
  起床       08:10-08:40  pending  0/7
  早间事项     09:00-09:25  pending  0/6
  午间事项     12:00-12:25  pending  0/6
  晚间事项     19:00-19:25  pending  0/6
```

**恢复后对账命令输出**：

```
history: 2026-11 没有已归档的执行结果
2026-11-27（周五）workday  4 窗口 / 25 次  播报 2  周专项 0
```

**指纹比对（附录 `14-hash-compare.txt` 原文）**：

```
baseline=FD10587C45115478A68065434A92708987478D76DA5FC1DF79A421B84D3FE281
restored=FD10587C45115478A68065434A92708987478D76DA5FC1DF79A421B84D3FE281
equal=True
```

### 6.1.4 步骤退出码与耗时

| 步骤 | 命令 | 退出码 | 耗时 (ms) |
|---|---|---|---|
| 1 | `install.py --workspace <WS> …` | 0 | 263 |
| 2 | 改 `backup.json` targets（纯文件改写） | — | — |
| 3 | `rtc.py … plan --now 2026-11-27T06:00:00+08:00` | 0 | 390 |
| 4 | `rtc.py … status` / `status --json` | 0 / 0 | 157 / 150 |
| 5 | `rtc.py … backup --to <TARGET>` | 0 | 137 |
| 6 | `probe_zip.py <zip>` | 0 | 96 |
| 7 | `rtc.py --workspace <RESTORE> status` | 0 | 141 |
| 8 | 删账本后 `rtc.py … status` | 0 | 124 |
| 9 | 盖回 `state` / `routines.json` / `backup.json`（纯文件复制） | — | — |
| 10 | `status` / `status --json` / `history --month` / `schedule --month` | 0 / 0 / 0 / 0 | 143 / 147 / 139 / 140 |

### 6.1.5 比对字段与结论

| 字段 | 备份时刻 | 恢复后 | 一致 |
|---|---|---|---|
| `planDate` | 2026-11-27 | 2026-11-27 | ✅ |
| `rev` | 1 | 1 | ✅ |
| 窗口数 | 4 | 4 | ✅ |
| 窗口 `id` / `label` / `state` / `fireCount` / `expectFires` | 4 条逐项 | 4 条逐项 | ✅ |
| `state/alarms.json` SHA256 | `FD10587C45115478A68065434A92708987478D76DA5FC1DF79A421B84D3FE281` | 同一指纹（字节一致，`equal=True`） | ✅ |

结论：按本文档第 1–5 步，删除 `state/alarms.json` 后可从当日 zip 完整恢复；`status`
与备份时刻一致。「一致」的机器判据是上表五项 + 账本字节指纹相等，不是肉眼看起来差不多。

> 说明：`history --month 2026-11` 在这份全新演练工作区里没有已归档执行结果，属预期；
> 每日快照在 `state/schedule/2026-11-27.json`，`schedule --month 2026-11` 打出当天
> 4 窗口 / 25 次。`<RESTORE>` 只读副本里 `备份` 一行显示「尚未备份」，是因为
> `state/.backup-last.json` 不在归档清单内、没有进 zip，不影响账本恢复判定。

---

## 7. 恢复不了怎么办

| 情况 | 处理 |
|---|---|
| 所有 zip 都坏了 | `state/schedule/*.json` 是每日快照，能拿回「每天该有什么」，但拿不回 `rev` 与投递记录 |
| 快照也没了 | 数据真丢了。重建方式：按 `rules/routines.md` 重写时刻表 → `plan --force`，从今天重新开始记账 |
| 只有 `rules/routines.json` 坏了 | 不用整份恢复：从任一 zip 里只取这一个文件盖回 `rules/`，再 `plan --force` |
| 想预防下一次 | 打开 git 归档（`rules/backup.json` 里的 `git` 段），每天会自动 commit 到私有仓，多一层异地副本 |
