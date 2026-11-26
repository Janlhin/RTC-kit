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

## 6.1 删除后恢复演练（2026-11-27，真实执行，对应 T-011）

第 6 节是只读试跑；本节补上闭环：真的删掉 `state/alarms.json`，再按第 1–5 步盖回。
演练全程在隔离临时工作区，脚本 / 技能 / 备份目标都指向临时目录，真实工作区零触碰。

记号：`<WS>` 临时工作区，`<TARGET>` 临时备份目录，`<RESTORE>` 临时解压目录，
`<CODE_DIR>` 仓库 `code/`。

**步骤与命令**（退出码均为本次实测）：

1. 安装器初始化工作区（脚本 / 技能重定向到临时目录）：

   ```powershell
   python install.py --workspace <WS> --harness-scripts-dir <TMP>/scripts --harness-skills-dir <TMP>/skills
   ```

   退出码 0。

2. 固定时刻生成当天账本：

   ```powershell
   python <CODE_DIR>/rtc.py --workspace <WS> plan --now 2026-11-27T06:00:00+08:00
   ```

   退出码 0；输出 `plan: 2026-11-27 workday 4 窗口 / 25 次，rev=1`。

3. 打备份（`plan` 每日自动打一份；手动复核用）：

   ```powershell
   python <CODE_DIR>/rtc.py --workspace <WS> backup --to <TARGET>
   ```

   退出码 0；zip 为 `RTC-20261127.zip`，标记 `state/.backup-last.json`：
   `written=1, git=null, errors=[]`。

4. 备份时刻基线：`planDate=2026-11-27`，`rev=1`，4 条窗口
   （`wake` / `meds-am` / `meds-noon` / `meds-pm`）。`state/alarms.json` 的
   SHA256 本次实测为 `FD10587C…`（各人各时刻不同；复跑时以「恢复后指纹 == 备份时刻
   指纹」为准，不要照抄这个哈希值）。

5. 按第 1 步开 zip 核对：`planDate=2026-11-27`、`rev=1`、窗口数 4，时刻表 id 与
   账本窗口对得上。

6. 按第 2 步解压到 `<RESTORE>`，按第 3 步只读试跑：

   ```powershell
   python <CODE_DIR>/rtc.py --workspace <RESTORE> status
   ```

   退出码 0；`rev=1`，4 条窗口与基线一致。

7. 真删 `state/alarms.json`；再跑：

   ```powershell
   python <CODE_DIR>/rtc.py --workspace <WS> status
   ```

   退出码 0；输出 `尚无计划。先跑：python rtc.py plan`（账本已丢，与预期一致）。

8. 按第 4 步盖回：`<RESTORE>/state` 整个盖进 `<WS>/`，再盖回 `rules/routines.json`、
   `rules/backup.json`。

9. 恢复后对账：

   ```powershell
   python <CODE_DIR>/rtc.py --workspace <WS> status
   python <CODE_DIR>/rtc.py --workspace <WS> status --json
   python <CODE_DIR>/rtc.py --workspace <WS> history --month 2026-11
   python <CODE_DIR>/rtc.py --workspace <WS> schedule --month 2026-11
   ```

   四条退出码均为 0。

**比对字段与结果**（备份时刻 → 恢复后）：

| 字段 | 备份时刻 | 恢复后 | 一致 |
|---|---|---|---|
| `planDate` | 2026-11-27 | 2026-11-27 | ✅ |
| `rev` | 1 | 1 | ✅ |
| 窗口数 | 4 | 4 | ✅ |
| 窗口 `id` / `label` / `state` / `fireCount` / `expectFires` | 4 条逐项 | 4 条逐项 | ✅ |
| `state/alarms.json` SHA256 | 备份时刻指纹 | 同一指纹（字节一致） | ✅ |

结论：按本文档第 1–5 步，删除 `state/alarms.json` 后可从当日 zip 完整恢复；`status`
与备份时刻一致。「一致」的机器判据是上表五项，不是肉眼看起来差不多。

> 说明：`history --month 2026-11` 在这份全新演练工作区里没有已归档执行结果，属预期；
> 每日快照在 `state/schedule/2026-11-27.json`，`schedule --month 2026-11` 打出当天
> 4 窗口 / 25 次。

---

## 7. 恢复不了怎么办

| 情况 | 处理 |
|---|---|
| 所有 zip 都坏了 | `state/schedule/*.json` 是每日快照，能拿回「每天该有什么」，但拿不回 `rev` 与投递记录 |
| 快照也没了 | 数据真丢了。重建方式：按 `rules/routines.md` 重写时刻表 → `plan --force`，从今天重新开始记账 |
| 只有 `rules/routines.json` 坏了 | 不用整份恢复：从任一 zip 里只取这一个文件盖回 `rules/`，再 `plan --force` |
| 想预防下一次 | 打开 git 归档（`rules/backup.json` 里的 `git` 段），每天会自动 commit 到私有仓，多一层异地副本 |
