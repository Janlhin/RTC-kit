#requires -Version 7.4
<#
.SYNOPSIS
    安装 git 钩子：把 core.hooksPath 指向 .husky，并校验转发薄壳的行尾。
.NOTES
    .husky 下的钩子是 sh 薄壳，必须 LF 行尾——CRLF 会让 sh 报
    "/usr/bin/env: 'sh\r': No such file or directory"。
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    & git config core.hooksPath .husky
    if ($LASTEXITCODE -ne 0) {
        [Console]::Error.WriteLine('设置 core.hooksPath 失败')
        exit 1
    }

    $crlf = @()
    foreach ($name in @('pre-commit', 'commit-msg')) {
        $path = Join-Path $root (Join-Path '.husky' $name)
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $bytes = [IO.File]::ReadAllBytes($path)
        if ($bytes -contains 13) { $crlf += $name }
    }
    if ($crlf.Count -gt 0) {
        [Console]::Error.WriteLine('钩子文件含 CR：' + ($crlf -join '、') + '；需转成 LF')
        exit 1
    }

    Write-Host '钩子已启用（core.hooksPath = .husky）'
    exit 0
}
finally {
    Pop-Location
}
