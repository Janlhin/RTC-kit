#requires -Version 7.4
<#
.SYNOPSIS
    commit-msg 钩子的转发薄壳：校验提交信息的标题格式。
.NOTES
    Conventional Commits：type(scope)!: 主题。type 限下表内的值，
    scope 可省略，主题不得为空、不得以句号结尾。
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$msgFile = $args[0]
if (-not $msgFile -or -not (Test-Path -LiteralPath $msgFile)) {
    [Console]::Error.WriteLine('commit-msg: 没有拿到提交信息文件')
    exit 1
}

$first = (Get-Content -LiteralPath $msgFile -Encoding utf8 | Select-Object -First 1)

# 合并提交与 revert 提交的标题由 git 生成，不套 Conventional Commits
$gitDir = & git rev-parse --git-dir 2>$null
if ($gitDir -and (Test-Path -LiteralPath (Join-Path $gitDir 'MERGE_HEAD'))) { exit 0 }
if ($first -match '^(Merge |Revert ")') { exit 0 }

$pattern = '^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\((?:[a-z0-9./-]+)\))?!?: [^ ].{0,}$'

if ($first -notmatch $pattern) {
    [Console]::Error.WriteLine('提交信息标题不合规。示例：docs(contributing): 补充提交信息约定')
    [Console]::Error.WriteLine("实际：$first")
    exit 1
}

if ($first.TrimEnd() -match '。$') {
    [Console]::Error.WriteLine('提交信息标题不要以中文句号结尾。')
    exit 1
}

exit 0
