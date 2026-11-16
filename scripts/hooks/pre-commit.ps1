#requires -Version 7.4
<#
.SYNOPSIS
    pre-commit 钩子的转发薄壳：只调 scripts/verify.ps1 -Fast，不写任何规则。
.NOTES
    规则只有一份载体（scripts/verify.ps1）。钩子里出现判断逻辑就等于第二个载体，
    两边迟早说两套话。
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
& pwsh -NoLogo -NoProfile -File (Join-Path $root 'scripts' 'verify.ps1') -Fast
exit $LASTEXITCODE
