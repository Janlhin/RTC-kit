#requires -Version 7.4
<#
.SYNOPSIS
    文档命令门禁：文档里出现的 rtc.py 子命令与参数必须真实存在，链路命令必须在临时工作区跑通。
.NOTES
    分两层，都是机器判据：
    A. 命令面：把全部 .md 里的 `rtc.py --workspace … <子命令> …` 抽出来，逐条比对
       `rtc.py --help` / `rtc.py <子命令> --help` 的真实选项。写错子命令或参数名必红。
    B. 链路：在临时工作区（install.py 生成，harness 目录也在临时区）跑一条最小链路，
       断言退出码全为 0。不碰真实部署。
    会改宿主机状态的命令（hermes gateway / tools disable / cron create）不在门禁范围。
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$root = Split-Path -Parent $PSScriptRoot
$venvWin = Join-Path $root '.venv' | Join-Path -ChildPath 'Scripts\python.exe'
$venvPosix = Join-Path $root '.venv' | Join-Path -ChildPath 'bin/python'
$py = if ($env:PYTHON) {
    $env:PYTHON
}
elseif (Test-Path -LiteralPath $venvWin) {
    $venvWin
}
elseif (Test-Path -LiteralPath $venvPosix) {
    $venvPosix
}
else {
    'python'
}

$rtc = Join-Path $root 'code/rtc.py'
$fail = 0
$checked = 0

Push-Location $root
try {
    # ---------- A. 命令面存在性 ----------
    $subOut = & $py $rtc --help 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        [Console]::Error.WriteLine('rtc.py --help 跑不通，无法校验文档')
        exit 1
    }
    # argparse 的 usage 行里 `{plan,due,...}` 就是全部子命令，比解析缩进更可靠
    $subs = @()
    if ($subOut -match '\{([a-z0-9,-]+)\}') {
        $subs = @($Matches[1] -split ',' | Sort-Object -Unique)
    }
    $globalFlags = @([regex]::Matches($subOut, '(?m)^\s+(--[a-z][a-z0-9-]*)') |
            ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)

    $subFlags = @{}
    foreach ($s in $subs) {
        if ($s -eq 'help') { continue }
        $h = & $py $rtc $s --help 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) { continue }
        $subFlags[$s] = @([regex]::Matches($h, '(?m)^\s+--[a-z][a-z0-9-]*') |
                ForEach-Object { $_.Value.Trim() } | Sort-Object -Unique)
    }

    # 文档里每个「选项后面跟的是值」的选项，抽样时要把值跳过去
    $valueOpts = @('--workspace', '--window', '--text', '--to', '--date', '--now', '--month',
        '--item', '--days', '--keep', '--dir', '--remote', '--limit', '--state')

    $lines = @(git ls-files '*.md' | ForEach-Object {
            $f = $_
            $i = 0
            $inFence = $false
            foreach ($line in (Get-Content -LiteralPath $f -Encoding utf8)) {
                $i++
                if ($line -match '^\s*(```|~~~)') { $inFence = -not $inFence; continue }
                if ($inFence -and $line -match 'rtc\.py') { "$f`:$i`t$line" }
            }
        })

    $problems = @()
    foreach ($entry in $lines) {
        $doc, $body = $entry -split "`t", 2
        $tokens = @($body -split '\s+' | Where-Object { $_ })
        # 去掉解释器与脚本路径本身，剩下的第一个裸词就是子命令
        $rest = @($tokens | Where-Object { $_ -notmatch '^(python|python3|py)$' -and $_ -notmatch 'rtc\.py$' })
        $sub = $null
        $flags = @()
        $i = 0
        while ($i -lt $rest.Count) {
            $t = $rest[$i]
            if ($t -match '^<.*>$' -or $t -match '^\{\{.*\}\}$') { $i++; continue }
            if ($t -match '^--') {
                $flags += $t
                if ($valueOpts -contains $t -and $i + 1 -lt $rest.Count) { $i += 2 } else { $i++ }
                continue
            }
            if ($null -eq $sub) { $sub = $t }
            $i++
        }
        if ($null -eq $sub) { continue }
        # 代码块里的注释、提示符、流程图残留不是命令，跳过
        if ($sub -notmatch '^[a-z][a-z0-9-]+$') { continue }
        if ($body -match '^\s*(#|\$|>|→|\||`)') { continue }
        $checked++
        if ($subs -notcontains $sub) {
            $problems += "$doc 子命令不存在：$sub"
            continue
        }
        $allowed = @($globalFlags) + @($subFlags[$sub] ?? @())
        foreach ($fl in $flags) {
            if ($allowed -notcontains $fl) {
                $problems += "$doc 参数 $fl 不属于 $sub"
            }
        }
    }

    Write-Host "  文档里引用 rtc.py 的条目：$checked 条，子命令 $($subs.Count) 个"

    if ($problems.Count -gt 0) {
        [Console]::Error.WriteLine('文档与命令面不一致：')
        $problems | Sort-Object -Unique | ForEach-Object { [Console]::Error.WriteLine("  $_") }
        $fail = 1
    }

    # ---------- B. 链路冒烟 ----------
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('rtckit-docgate-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    $tmp = [IO.Path]::GetFullPath($tmp)
    if (-not $tmp.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()))) {
        [Console]::Error.WriteLine("临时目录解析异常：$tmp")
        exit 1
    }
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $ws = Join-Path $tmp 'ws'

    $steps = @(
        @{ name = 'install'; cmd = { & $py (Join-Path $root 'install.py') --workspace $ws --harness-scripts-dir (Join-Path $tmp 'scripts') --harness-skills-dir (Join-Path $tmp 'skills') } }
        @{ name = 'plan'; cmd = { & $py $rtc --workspace $ws plan --now '2026-11-17T06:00:00+08:00' } }
        @{ name = 'due'; cmd = { & $py $rtc --workspace $ws due --now '2026-11-17T08:10:00+08:00' } }
        @{ name = 'ack'; cmd = { & $py $rtc --workspace $ws ack --text '起了' --now '2026-11-17T08:11:00+08:00' } }
        @{ name = 'status'; cmd = { & $py $rtc --workspace $ws status } }
        @{ name = 'metrics'; cmd = { & $py $rtc --workspace $ws metrics report } }
        @{ name = 'schedule'; cmd = { & $py $rtc --workspace $ws schedule '2026-11-17' } }
        @{ name = 'history'; cmd = { & $py $rtc --workspace $ws history '2026-11-17' } }
        @{ name = 'backup'; cmd = { & $py $rtc --workspace $ws backup --to (Join-Path $tmp 'backup') --keep 3 } }
        @{ name = 'brief'; cmd = { & $py $rtc --workspace $ws brief } }
    )

    foreach ($s in $steps) {
        & $s.cmd *> $null
        if ($LASTEXITCODE -ne 0) {
            [Console]::Error.WriteLine("  链路 $($s.name) 退出码 $LASTEXITCODE")
            $fail = 1
        }
    }

    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue

    exit $fail
}
finally {
    Pop-Location
}
