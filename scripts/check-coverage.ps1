#requires -Version 7.4
<#
.SYNOPSIS
    覆盖率双阈值：语句不足 80% 或分支不足 70% 即失败。
.PARAMETER Python
    用来跑 coverage 的解释器；默认 PATH 上的 python。
.NOTES
    语句阈值只在 pyproject.toml 里写一次（`fail_under`），本脚本读它，不再重写一个数字。
    分支阈值 coverage.py 没有对应配置项，只能由本脚本判——所以那一个 70 是这里的常量。
    两条路径都拦：`coverage report` 会因 fail_under 变红，本脚本会因分支不足变红。
#>
param(
    [string]$Python = 'python'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$root = Split-Path -Parent $PSScriptRoot
$pyproject = Join-Path $root 'pyproject.toml'
$branchMin = 70.0

$line = Select-String -LiteralPath $pyproject -Pattern '^fail_under\s*=\s*(\d+(?:\.\d+)?)' | Select-Object -First 1
if (-not $line) {
    [Console]::Error.WriteLine('pyproject.toml 里没有 fail_under，语句阈值缺失。')
    exit 1
}
$statMin = [double]$line.Matches[0].Groups[1].Value

Push-Location $root
$json = Join-Path ([IO.Path]::GetTempPath()) ("rtc-coverage-{0}.json" -f [guid]::NewGuid().ToString('N'))
try {
    # --fail-under=0：先让 coverage 无条件产出 JSON，阈值由本脚本统一判，
    # 否则「语句未达标」会被 coverage 自己拦下，轮到我们报的时候已经看不到数字了。
    & $Python -m coverage json --quiet -o $json --fail-under=0
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $json)) {
        [Console]::Error.WriteLine('coverage json 未产出报告：先跑 coverage run。')
        exit 1
    }

    # coverage 的 JSON 里有以空字符串为键的条目，ConvertFrom-Json 默认拒绝解析
    $totals = (Get-Content -LiteralPath $json -Raw | ConvertFrom-Json -AsHashtable).totals
    $stat = [double]$totals.percent_covered
    $branchTotal = [double]$totals.num_branches
    $branch = if ($branchTotal -gt 0) {
        [double]$totals.covered_branches / $branchTotal * 100
    }
    else {
        100.0
    }

    Write-Host ("  语句 {0:N2}%（阈值 {1}%，共 {2} 行）" -f $stat, $statMin, $totals.num_statements)
    Write-Host ("  分支 {0:N2}%（阈值 {1}%，共 {2} 个）" -f $branch, $branchMin, [int]$branchTotal)

    $bad = @()
    if ($stat -lt $statMin) { $bad += '语句覆盖率低于阈值' }
    if ($branch -lt $branchMin) { $bad += '分支覆盖率低于阈值' }
    if ($bad.Count -gt 0) {
        [Console]::Error.WriteLine(($bad -join '；') + '：补用例，不要降阈值。')
        exit 1
    }

    exit 0
}
finally {
    if (Test-Path -LiteralPath $json) { Remove-Item -LiteralPath $json -Force }
    Pop-Location
}
