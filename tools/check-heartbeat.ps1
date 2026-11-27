#requires -Version 7.4
<#
.SYNOPSIS
    外部只读心跳看门狗：检查 RTC 工作区最后一次成功判定是否超过阈值。
.NOTES
    与 RTC 主链路解耦：只读 <Workspace>/state/heartbeat.json 的 lastOkAt（兼容旧字段 at），
    不写任何文件、不调用 rtc.py、不读凭据、不联网。载体（系统计划任务 / 另一台机器 /
    手机提醒 / 第三方监控）由使用者自行部署，RTC 不在内部起 cron。
    退出码只有两个：0 = 健康；1 = 超时 / 文件缺失 / 不可解析 / 无成功记录。
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Workspace,
    [double]$MaxAgeHours = 24
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function Get-Field {
    param($Object, [string]$Name)
    $prop = $Object.PSObject.Properties[$Name]
    if ($null -eq $prop) { return $null }
    return $prop.Value
}

function Write-Diagnostic {
    param([string]$Message)
    [Console]::Error.WriteLine($Message)
}

if ($MaxAgeHours -lt 0) {
    Write-Diagnostic "参数错误：-MaxAgeHours 不能为负数（收到 $MaxAgeHours）。"
    exit 1
}

$hbPath = Join-Path $Workspace 'state\heartbeat.json'

if (-not (Test-Path -LiteralPath $hbPath -PathType Leaf)) {
    Write-Diagnostic "心跳异常：找不到心跳文件 $hbPath；可能是工作区路径不对，或网关整体停摆、due 从未成功跑过。建议：核对工作区路径，再按排查文档第 1 节检查宿主调度器。"
    exit 1
}

$data = $null
try {
    $raw = [IO.File]::ReadAllText($hbPath, [Text.Encoding]::UTF8)
    $data = $raw | ConvertFrom-Json -ErrorAction Stop
}
catch {
    Write-Diagnostic '心跳异常：heartbeat.json 不是合法 JSON；文件已损坏。建议：保留原件，按排查文档第 3 节处置，不要手改 state/。'
    exit 1
}

if ($null -eq $data -or $data -isnot [pscustomobject]) {
    Write-Diagnostic '心跳异常：heartbeat.json 的形状不是对象；文件已损坏。建议：保留原件，按排查文档第 3 节处置，不要手改 state/。'
    exit 1
}

$iso = Get-Field -Object $data -Name 'lastOkAt'
if ([string]::IsNullOrWhiteSpace([string]$iso)) {
    $iso = Get-Field -Object $data -Name 'at'
}
if ([string]::IsNullOrWhiteSpace([string]$iso)) {
    Write-Diagnostic '心跳异常：heartbeat.json 没有 lastOkAt（兼容字段 at 也为空）；本工作区从未留下成功判定记录。建议：先跑一次 due 链路，排查安装与调度。'
    exit 1
}

$lastOk = $null
try {
    $lastOk = [DateTimeOffset]::Parse([string]$iso)
}
catch {
    Write-Diagnostic "心跳异常：lastOkAt（$iso）不是合法时间戳；文件已损坏。建议：保留原件，按排查文档第 3 节处置。"
    exit 1
}

$age = [DateTimeOffset]::UtcNow - $lastOk
$ageHours = $age.TotalHours

if ($ageHours -gt $MaxAgeHours) {
    $lastOkText = $lastOk.LocalDateTime.ToString('yyyy-MM-dd HH:mm:ss zzz')
    Write-Diagnostic ("心跳超时：最后一次成功判定 {0}，距今 {1:N1} 小时（阈值 {2} 小时）。建议：检查网关是否在跑、rtc-due 是否被停，对照排查文档第 1 节。" -f $lastOkText, $ageHours, $MaxAgeHours)
    exit 1
}

$ageText = if ($ageHours -ge 1) { '{0:N1} 小时' -f $ageHours } else { '{0:N0} 分钟' -f $age.TotalMinutes }
$lastOkHealthy = $lastOk.LocalDateTime.ToString('yyyy-MM-dd HH:mm:ss zzz')
Write-Host ("心跳正常：最后一次成功判定 {0}，距今 {1}（阈值 {2} 小时）。" -f $lastOkHealthy, $ageText, $MaxAgeHours)
exit 0
