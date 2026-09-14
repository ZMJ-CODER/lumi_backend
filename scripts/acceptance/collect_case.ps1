# 验收样例抓取：每个用例一条命令，自动落盘指标 / 时间标记 / SSE 序列 / Job 快照。
#
# 用法（先设好变量，避免手敲占位符）:
#   $case = "A_direct"
#   .\scripts\acceptance\collect_case.ps1 -Case $case -Token "<token>" -ConversationId "<conv>" `
#       -Content "把这句话改得更正式一些。"
#
# 工作区用例加 -WorkspaceId "w1"；Job 用例拿到 job_id 后加 -JobId "<job_id>" 再跑一次做刷新后快照。
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Case,
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$Token = "",
    [string]$ConversationId = "",
    [string]$WorkspaceId = "",
    [string]$Content = "",
    [string]$JobId = "",
    [switch]$SkipSse
)

$ErrorActionPreference = "Stop"
$outDir = "artifacts\acceptance"
$logDir = "app\logs"
New-Item -ItemType Directory -Force $outDir | Out-Null
New-Item -ItemType Directory -Force $logDir | Out-Null
$markers = Join-Path $logDir "markers.txt"

Write-Host "[$Case] metrics(before) -> $outDir\$Case.metrics.before.txt"
curl.exe -s "$BaseUrl/metrics" -o "$outDir\$Case.metrics.before.txt"

Add-Content $markers "$(Get-Date -Format o)  $Case start"

if (-not $SkipSse -and $Content) {
    & .venv\Scripts\python.exe scripts\acceptance\sse_capture.py `
        $BaseUrl $Token $ConversationId $Content $WorkspaceId $Case
}

if ($JobId) {
    Write-Host "[$Case] job snapshot -> $outDir\$Case.job.json"
    curl.exe -s -H "Authorization: Bearer $Token" "$BaseUrl/api/v1/agents/jobs/$JobId" -o "$outDir\$Case.job.json"
}

Add-Content $markers "$(Get-Date -Format o)  $Case end"

Write-Host "[$Case] metrics(after) -> $outDir\$Case.metrics.after.txt"
curl.exe -s "$BaseUrl/metrics" -o "$outDir\$Case.metrics.after.txt"

$patterns = "lumi_execution_policy_route_total|lumi_policy_route_latency|lumi_answer_first_delta|lumi_answer_stream_duration|lumi_workspace_read_duration|lumi_planner_invoked|lumi_agent_invoked"
Select-String -Path "$outDir\$Case.metrics.after.txt" -Pattern $patterns |
    ForEach-Object { $_.Line } |
    Set-Content "$outDir\$Case.metrics.txt"

Write-Host "[$Case] 完成。产物："
Get-ChildItem "$outDir\$Case.*" | Select-Object Name, Length

if (Select-String -Path "$outDir\$Case.metrics.before.txt" -Pattern "metrics disabled" -Quiet) {
    Write-Warning "METRICS_ENABLED 未开启：/metrics 只有 '# metrics disabled'，指标证据不可用。"
}
Write-Host "下一步：把 $outDir\$Case.* 以及该时段 app\logs\lumi_*.log 的 policy_v2_acceptance/WARN/ERROR 行发我。"
