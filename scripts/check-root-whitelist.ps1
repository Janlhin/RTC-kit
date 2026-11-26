#requires -Version 7.4
<#
.SYNOPSIS
    根目录白名单：只允许登记过的文件与目录。
.NOTES
    install.py 是安装入口，属于豁免项，理由写在 DECISIONS.md D-006。
    新增根目录文件/目录必须先提案，再改这里的白名单。
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$allowedFiles = @(
    'README.md', 'AGENTS.md', 'DECISIONS.md', 'TASKS.md', 'MANIFEST.md',
    'LICENSE', 'pyproject.toml', '.python-version', '.gitignore',
    '.gitattributes', '.editorconfig', 'install.py',
    'CHANGELOG.md', 'SECURITY.md'
)
$allowedDirs = @(
    '.github', '.husky', 'code', 'contract', 'docs', 'platform',
    'rules', 'scripts', 'skills', 'templates', 'tools'
)

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $entries = @(git ls-files | ForEach-Object { ($_ -split '/')[0] } | Sort-Object -Unique)
    $offenders = @()
    foreach ($e in $entries) {
        if (Test-Path -LiteralPath $e -PathType Container) {
            if ($allowedDirs -notcontains $e) { $offenders += "$e/  (未登记的目录)" }
        }
        elseif ($allowedFiles -notcontains $e) {
            $offenders += "$e  (未登记的文件)"
        }
    }

    $untracked = @(git ls-files --others --exclude-standard | ForEach-Object { ($_ -split '/')[0] } |
            Sort-Object -Unique)
    foreach ($u in $untracked) {
        if ($allowedFiles -notcontains $u -and $allowedDirs -notcontains $u) {
            $offenders += "$u  (本地新增，尚未登记)"
        }
    }

    if ($offenders.Count -gt 0) {
        [Console]::Error.WriteLine('根目录出现白名单之外的条目：')
        $offenders | ForEach-Object { [Console]::Error.WriteLine("  $_") }
        [Console]::Error.WriteLine('处理方式：移入子目录，或在 DECISIONS.md 登记豁免后加进本脚本白名单。')
        exit 1
    }

    exit 0
}
finally {
    Pop-Location
}
