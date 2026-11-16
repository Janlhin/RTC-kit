#requires -Version 7.4
<#
.SYNOPSIS
    脚本基线门禁：仓库内不得出现 .cmd / .bat，也不得用 cmd 专有调用。
.NOTES
    只扫「项目自写的东西」：CI、hooks、脚本、文档。第三方工具的启动层
    （例如 npm 在 Windows 上经 cmd.exe 起脚本）属于豁免，需在 DECISIONS 登记。
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $bad = @(git ls-files | Where-Object { $_ -match '\.(cmd|bat)$' })
    if ($bad.Count -gt 0) {
        [Console]::Error.WriteLine('禁止 .cmd / .bat 文件：')
        $bad | ForEach-Object { [Console]::Error.WriteLine("  $_") }
        exit 1
    }

    $pattern = 'cmd(\.exe)?\s*/c|powershell(\.exe)?\s+-|shell:\s*(cmd|powershell)'
    $paths = @('.github', 'scripts', '.husky')
    $existing = $paths | Where-Object { Test-Path -LiteralPath $_ }
    if ($existing.Count -gt 0) {
        $hits = @(git grep -n -E -- $pattern -- $existing 2>$null)
        if ($LASTEXITCODE -eq 0 -and $hits.Count -gt 0) {
            [Console]::Error.WriteLine('检测到禁止的脚本基线用法：')
            $hits | ForEach-Object { [Console]::Error.WriteLine("  $_") }
            exit 1
        }
    }

    exit 0
}
finally {
    Pop-Location
}
