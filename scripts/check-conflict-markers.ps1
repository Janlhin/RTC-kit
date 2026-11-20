#requires -Version 7.4
<#
.SYNOPSIS
    冲突标记门禁：Git 跟踪的文本里不得残留 <<<<<<< / >>>>>>> / 七等号分隔行。
.NOTES
    合并时漏删的标记，ruff 不管、测试不管、链接检查也不管——只有渲染出来才发现。
    所以单独开一条通道：直接问 Git 要「已跟踪文件里谁还有这几个开头」。
    只扫跟踪文件，未跟踪的草稿不拦（那是编辑中的中间态）。
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    # 三个形态分开写，避免把 Markdown 的 setext 下划线误判成冲突标记
    $patterns = @('^<<<<<<< ', '^>>>>>>> ', '^=======$')
    $hits = @()
    foreach ($p in $patterns) {
        $found = & git grep -n -E -- $p
        if ($LASTEXITCODE -eq 0 -and $found) {
            $hits += $found
        }
    }

    if ($hits.Count -gt 0) {
        [Console]::Error.WriteLine('发现合并冲突标记（合并时没删干净）：')
        $hits | ForEach-Object { [Console]::Error.WriteLine("  $_") }
        exit 1
    }

    exit 0
}
finally {
    Pop-Location
}
