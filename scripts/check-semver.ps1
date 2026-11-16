#requires -Version 7.4
<#
.SYNOPSIS
    版本号门禁：pyproject.toml 的 version 必须是合法 SemVer 2.0.0。
.NOTES
    禁止前导零；1.9.1 低于 1.10.0，比较必须按数值，不能按字符串。
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$root = Split-Path -Parent $PSScriptRoot
$toml = Join-Path $root 'pyproject.toml'

if (-not (Test-Path -LiteralPath $toml)) {
    [Console]::Error.WriteLine('找不到 pyproject.toml')
    exit 1
}

$line = Select-String -LiteralPath $toml -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
if ($null -eq $line) {
    [Console]::Error.WriteLine('pyproject.toml 里没有 version 字段')
    exit 1
}

$version = $line.Matches[0].Groups[1].Value
$semver = '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*)?(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?$'

if ($version -notmatch $semver) {
    [Console]::Error.WriteLine("非法版本号：$version")
    exit 1
}

Write-Host "  version = $version"
exit 0
