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
    # Git grep retains CR before LF; accept that byte without permitting other whitespace.
    $patterns = @('^<<<<<<< ', '^>>>>>>> ', "^=======`r?$")
    $hits = @()
    foreach ($p in $patterns) {
        $found = & git grep -n -E -- $p
        if ($LASTEXITCODE -eq 0) {
            if ($found) { $hits += $found }
        }
        elseif ($LASTEXITCODE -gt 1) {
            # git grep：1 = 没有匹配（正常），≥2 = 读取失败（仓库坏了 / 权限 / 锁）
            # 读取失败不能当成「没有标记」放行，否则门禁在最需要它的时候失声。
            [Console]::Error.WriteLine("git grep 读取失败（exit $LASTEXITCODE）：无法确认是否残留冲突标记")
            exit 1
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
