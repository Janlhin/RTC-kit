$ErrorActionPreference = 'Continue'
$repo = 'C:\Users\Janlhin\AppData\Local\DoubaoWork\User Data\Default\.doubaowork\agent_mode\workspace\.sessions\38456938345562882\agents\s_000K0UXppAb\rtc-99\repo'
$py = Join-Path $repo '.venv\Scripts\python.exe'
$rtc = Join-Path $repo 'code\rtc.py'
$install = Join-Path $repo 'install.py'
$root = Join-Path $env:TEMP 'rtc-restore-drill-20261127'
$ws = Join-Path $root 'ws'
$target = Join-Path $root 'target'
$restore = Join-Path $root 'restore'
$hscripts = Join-Path $root 'harness-scripts'
$hskills = Join-Path $root 'harness-skills'
$log = Join-Path $root 'logs'

if (Test-Path $root) { Remove-Item -Recurse -Force $root }
New-Item -ItemType Directory -Force $ws, $target, $restore, $hscripts, $hskills, $log | Out-Null

function Run-Step($name, [string[]]$argList) {
  $out = Join-Path $log "$name.stdout.txt"
  $err = Join-Path $log "$name.stderr.txt"
  $sw = [Diagnostics.Stopwatch]::StartNew()
  & $py @argList 1> $out 2> $err
  $code = $LASTEXITCODE
  $sw.Stop()
  "name=$name`nexit=$code`nelapsedMs=$($sw.ElapsedMilliseconds)`nargv=$($argList -join ' ')" |
    Out-File (Join-Path $log "$name.meta.txt") -Encoding utf8
  Write-Host "[$name] exit=$code ms=$($sw.ElapsedMilliseconds)"
}

# 1. 安装器初始化工作区（脚本/技能重定向到临时目录）
Run-Step '01-install' @($install, '--workspace', $ws, '--harness-scripts-dir', $hscripts, '--harness-skills-dir', $hskills)

# 2. 把备份目标改成临时目录，避免 plan 自动备份写真机盘
$backupCfg = Join-Path $ws 'rules\backup.json'
$cfg = Get-Content $backupCfg -Raw | ConvertFrom-Json
$cfg.targets = @($target)
$cfg | ConvertTo-Json -Depth 6 | Set-Content $backupCfg -Encoding utf8
Write-Host 'backup.json targets ->' $target

# 3. 固定时刻生成当天账本
Run-Step '02-plan' @($rtc, '--workspace', $ws, 'plan', '--now', '2026-11-27T06:00:00+08:00')

# 4. 基线：status / status --json
Run-Step '03-status-baseline' @($rtc, '--workspace', $ws, 'status')
Run-Step '04-status-json-baseline' @($rtc, '--workspace', $ws, 'status', '--json')

# 5. 基线指纹（state/alarms.json 全文 SHA256）
$alarms = Join-Path $ws 'state\alarms.json'
$h1 = Get-FileHash -LiteralPath $alarms -Algorithm SHA256
"file=$alarms`nalgorithm=SHA256`nhash=$($h1.Hash)" | Out-File (Join-Path $log '05-hash-alarms-baseline.txt') -Encoding utf8

# 6. 手动备份到临时目标
Run-Step '05-backup' @($rtc, '--workspace', $ws, 'backup', '--to', $target)

# 7. zip 指纹
$zip = Join-Path $target 'RTC-20261127.zip'
if (Test-Path $zip) {
  $hz = Get-FileHash -LiteralPath $zip -Algorithm SHA256
  "file=$zip`nalgorithm=SHA256`nlength=$((Get-Item $zip).Length)`nhash=$($hz.Hash)" |
    Out-File (Join-Path $log '06-hash-zip.txt') -Encoding utf8
}

# 8. 打开 zip 核对条目与关键数字
$probe = @'
import json, zipfile, sys
zip_path = sys.argv[1]
with zipfile.ZipFile(zip_path) as z:
    names = z.namelist()
    print("ENTRIES=" + ",".join(names))
    a = json.loads(z.read("state/alarms.json").decode("utf-8"))
    r = json.loads(z.read("rules/routines.json").decode("utf-8"))
    print("planDate=" + str(a.get("planDate")))
    print("rev=" + str(a.get("rev")))
    print("windows=" + str(len(a.get("windows") or [])))
    print("alarmIds=" + ",".join(w.get("id","") for w in (a.get("windows") or [])))
    print("routineIds=" + ",".join(w.get("id","") for w in (r.get("alarmWindows") or [])))
'@
$probePath = Join-Path $root 'probe_zip.py'
$probe | Set-Content $probePath -Encoding utf8
Run-Step '06-zip-inspect' @($probePath, $zip)

# 9. 解压到只读目录试跑
if (Test-Path $restore) { Remove-Item -Recurse -Force $restore }
Expand-Archive -LiteralPath $zip -DestinationPath $restore -Force
Run-Step '07-status-restore-readonly' @($rtc, '--workspace', $restore, 'status')

# 10. 真删工作区账本，再 status（应当报尚无计划）
Remove-Item -LiteralPath $alarms -Force
Run-Step '08-status-after-delete' @($rtc, '--workspace', $ws, 'status')

# 11. 按 RESTORE.md 第 4 步盖回：state 整个、routines.json、backup.json
Copy-Item -Recurse -Force (Join-Path $restore 'state') (Join-Path $ws '')
Copy-Item -Force (Join-Path $restore 'rules\routines.json') (Join-Path $ws 'rules\')
Copy-Item -Force (Join-Path $restore 'rules\backup.json') (Join-Path $ws 'rules\')

# 12. 恢复后对账
Run-Step '09-status-restored' @($rtc, '--workspace', $ws, 'status')
Run-Step '10-status-json-restored' @($rtc, '--workspace', $ws, 'status', '--json')
Run-Step '11-history-restored' @($rtc, '--workspace', $ws, 'history', '--month', '2026-11')
Run-Step '12-schedule-restored' @($rtc, '--workspace', $ws, 'schedule', '--month', '2026-11')

# 13. 恢复后指纹并与基线比对
$h2 = Get-FileHash -LiteralPath $alarms -Algorithm SHA256
"file=$alarms`nalgorithm=SHA256`nhash=$($h2.Hash)" | Out-File (Join-Path $log '13-hash-alarms-restored.txt') -Encoding utf8
"baseline=$($h1.Hash)`nrestored=$($h2.Hash)`nequal=$($h1.Hash -eq $h2.Hash)" |
  Out-File (Join-Path $log '14-hash-compare.txt') -Encoding utf8

# 14. 目录清单
Get-ChildItem -Recurse $ws | ForEach-Object { $_.FullName.Substring($root.Length+1) } |
  Out-File (Join-Path $log '15-tree-ws.txt') -Encoding utf8
Get-ChildItem -Recurse $target | ForEach-Object { $_.FullName.Substring($root.Length+1) } |
  Out-File (Join-Path $log '16-tree-target.txt') -Encoding utf8

Write-Host '=== hash compare ==='
Get-Content (Join-Path $log '14-hash-compare.txt')
