#requires -Version 7.4
<#
.SYNOPSIS
    文档链接门禁：仓库内 Markdown 的相对链接必须指向真实文件。
.NOTES
    只校验文件是否可达，不校验锚点（锚点需要渲染器，纯文本检查会误报）。
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $files = @(git ls-files | Where-Object { $_ -match '\.md$' })
    $broken = @()
    $linkRe = [regex]'\]\(([^)\s]+)'

    foreach ($rel in $files) {
        $full = Join-Path $root $rel
        $text = Get-Content -LiteralPath $full -Raw -Encoding utf8
        foreach ($m in $linkRe.Matches($text)) {
            $target = $m.Groups[1].Value
            if ($target -match '^(http|https|mailto|#|tel):') { continue }
            $target = ($target -split '#')[0]
            if ([string]::IsNullOrWhiteSpace($target)) { continue }
            $target = [Uri]::UnescapeDataString($target)
            $base = Split-Path -Parent $full
            $resolved = Join-Path $base $target
            if (-not (Test-Path -LiteralPath $resolved)) {
                $broken += "$rel → $target"
            }
        }
    }

    if ($broken.Count -gt 0) {
        [Console]::Error.WriteLine('断链：')
        $broken | ForEach-Object { [Console]::Error.WriteLine("  $_") }
        exit 1
    }

    exit 0
}
finally {
    Pop-Location
}
