param(
    [Parameter(Mandatory = $true)]
    [string]$AccessToken,
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$FixtureDir = "tests/fixtures",
    [ValidateRange(1, 5)]
    [int[]]$ManifestNumbers = @(1, 2, 3, 4, 5),
    [ValidateRange(0, 60)]
    [int]$InterRequestDelaySeconds = 2
)

$headers = @{ Authorization = "Bearer $AccessToken" }
$files = $ManifestNumbers | Sort-Object -Unique | ForEach-Object {
    Join-Path $FixtureDir ("four_channel_telemetry_manifest_{0:D2}.txt" -f $_)
}

foreach ($file in $files) {
    if (-not (Test-Path -LiteralPath $file)) {
        throw "找不到清单文件: $file"
    }

    # Join lines explicitly instead of relying on -Raw.  This works in both
    # Windows PowerShell 5.1 and PowerShell 7, where provider/encoding
    # differences can otherwise turn the value into a string array.
    $requestText = [string]::Join(
        [Environment]::NewLine,
        @((Get-Content -LiteralPath $file -Encoding utf8))
    )
    if ([string]::IsNullOrWhiteSpace($requestText)) {
        throw "清单为空: $file"
    }
    $payload = [ordered]@{
        request = [string]$requestText
        scene = "office"
    }
    $body = ConvertTo-Json -InputObject $payload -Compress -Depth 3
    if ($body -notmatch '"request"\s*:\s*"') {
        throw "请求体序列化失败，request 不是字符串: $file"
    }

    $response = $null
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        try {
            $response = Invoke-RestMethod `
                -Method Post `
                -Uri "$BaseUrl/api/v1/agents/jobs" `
                -Headers $headers `
                -ContentType "application/json; charset=utf-8" `
                -Body ([string]$body) `
                -ErrorAction Stop
            break
        }
        catch {
            $errorText = [string]$_.ErrorDetails.Message
            if ([string]::IsNullOrWhiteSpace($errorText)) {
                $errorText = [string]$_.Exception.Message
            }
            if (
                $_.Exception.Response -and
                $_.Exception.Response.StatusCode.value__ -eq 429 -and
                $errorText -match "办公任务提交过于频繁" -and
                $attempt -lt 4
            ) {
                Write-Host "提交受限，等待 35 秒后重试: $file"
                Start-Sleep -Seconds 35
                continue
            }
            if ($errorText -match "当前有任务正在进行中") {
                throw "提交被拒绝：当前用户仍有活动办公任务。请先检查并终止本次演练任务后重试；不会把该错误当作普通限流重试。"
            }
            throw
        }
    }

    $jobId = [string]$response.data.job_id
    if ([string]::IsNullOrWhiteSpace($jobId)) {
        throw "提交成功但响应中没有 job_id: $file"
    }
    Write-Host "已创建 $file -> $jobId；立即取消以避免执行清单"

    try {
        $cancelResponse = Invoke-RestMethod `
            -Method Post `
            -Uri "$BaseUrl/api/v1/agents/jobs/$jobId/cancel" `
            -Headers $headers `
            -ContentType "application/json" `
            -Body '{"keep_completed":true}' `
            -ErrorAction Stop
        $status = [string]$cancelResponse.data.status
        Write-Host "已取消 $jobId（状态: $status）"
    }
    catch {
        Write-Warning "取消 $jobId 失败；请立即手动取消该任务。原始错误: $($_.Exception.Message)"
    }
    if ($InterRequestDelaySeconds -gt 0) {
        Start-Sleep -Seconds $InterRequestDelaySeconds
    }
}

Write-Host "已提交并尝试取消 $($files.Count) 份清单；每份产生 20 条路由事件。"
