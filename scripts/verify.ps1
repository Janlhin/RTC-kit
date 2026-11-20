#requires -Version 7.4
<#
.SYNOPSIS
    唯一验收入口：按「快 → 慢」跑完全部门禁。
.PARAMETER Fast
    跳过测试与覆盖率，只做静态检查（供 pre-commit 使用）。
.PARAMETER SkipStatic
    跳过静态检查，只跑测试、覆盖率与安装器冒烟（供 CI 的 test 层使用）。
#>
param(
    [switch]$Fast,
    [switch]$SkipStatic
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

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
$env:PYTHONIOENCODING = 'utf-8'
$fail = 0

function Step {
    param([string]$Name, [scriptblock]$Body)
    Write-Host "== $Name" -ForegroundColor Cyan
    & $Body
    if ($LASTEXITCODE -ne 0) {
        Write-Host "   FAILED (exit $LASTEXITCODE)" -ForegroundColor Red
        $script:fail = 1
    }
    else {
        Write-Host "   ok" -ForegroundColor DarkGray
    }
}

Push-Location $root
try {
    if (-not $SkipStatic) {
        Step '脚本基线（禁止 cmd/bat，CI 只用 pwsh）' {
            & pwsh -NoLogo -NoProfile -File (Join-Path $PSScriptRoot 'check-script-baseline.ps1')
        }

        Step '根目录白名单' {
            & pwsh -NoLogo -NoProfile -File (Join-Path $PSScriptRoot 'check-root-whitelist.ps1')
        }

        Step '冲突标记残留' {
            & pwsh -NoLogo -NoProfile -File (Join-Path $PSScriptRoot 'check-conflict-markers.ps1')
        }

        Step '文档链接可达' {
            & pwsh -NoLogo -NoProfile -File (Join-Path $PSScriptRoot 'check-doc-links.ps1')
        }

        Step '版本号 SemVer' {
            & pwsh -NoLogo -NoProfile -File (Join-Path $PSScriptRoot 'check-semver.ps1')
        }

        Step '文档命令（子命令/参数存在 + 链路冒烟）' {
            & pwsh -NoLogo -NoProfile -File (Join-Path $PSScriptRoot 'verify-docs.ps1')
        }

        Step '格式（ruff format --check）' {
            & $py -m ruff format --check code install.py
        }

        Step 'Lint（ruff check）' {
            & $py -m ruff check code install.py
        }

        Step '类型（mypy）' {
            & $py -m mypy
        }
    }

    if (-not $Fast) {
        Step '测试 + 覆盖率' {
            $env:COVERAGE_PROCESS_START = Join-Path $PSScriptRoot 'coverage.rc'
            $env:PYTHONPATH = $PSScriptRoot
            & $py -m coverage erase
            & $py -m coverage run -m unittest discover -s code/tests -p 'test_*.py'
            if ($LASTEXITCODE -ne 0) { return }
            & $py -m coverage combine
            & $py -m coverage report
            if ($LASTEXITCODE -ne 0) { return }
            & pwsh -NoLogo -NoProfile -File (Join-Path $PSScriptRoot 'check-coverage.ps1') -Python $py
        }

        Step '安装器冒烟（dry-run）' {
            & $py install.py --workspace ./_smoke-ws --dry-run
        }
    }
}
finally {
    Pop-Location
}

if ($fail -ne 0) {
    Write-Host '校验未通过：修，不要绕过。' -ForegroundColor Red
    exit 1
}
Write-Host '全部通过。' -ForegroundColor Green
exit 0
