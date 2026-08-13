<#
.SYNOPSIS
  一键启动/检查/停止 Lumi 的 Temporal 调试环境（开发服务器 + Worker）。

.DESCRIPTION
  默认行为：检查 Temporal 开发服务器（gRPC 7233）与 Worker 是否在运行，
  未运行则后台拉起（隐藏窗口、日志写入 tools/temporal/*.log），已运行则跳过。

.PARAMETER Status
  只打印当前运行状态，不启动任何进程。

.PARAMETER Stop
  停止 Temporal 服务器与 Worker（不删数据）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\start_temporal.ps1
  powershell -ExecutionPolicy Bypass -File scripts\start_temporal.ps1 -Status
  powershell -ExecutionPolicy Bypass -File scripts\start_temporal.ps1 -Stop
#>

param(
    [switch]$Status,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path $PSScriptRoot -Parent
$temporalDir = Join-Path $projectRoot "tools\temporal"
$temporalExe = Join-Path $temporalDir "temporal.exe"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

$serverOut = Join-Path $temporalDir "temporal-dev.out.log"
$serverErr = Join-Path $temporalDir "temporal-dev.err.log"
$workerOut = Join-Path $temporalDir "temporal-worker.out.log"
$workerErr = Join-Path $temporalDir "temporal-worker.err.log"

$serverAddress = "127.0.0.1:7233"

function Test-TemporalServer {
    # TCP 端口探测：不依赖原生命令 stderr，兼容 Windows PowerShell 5.1
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect("127.0.0.1", 7233, $null, $null)
        $connected = $async.AsyncWaitHandle.WaitOne(1500)
        return ($connected -and $client.Connected)
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Get-TemporalWorkerProcess {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*app.agents.orchestration.temporal.worker*" }
}

function Start-HiddenProcess {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory,
        [string]$OutLog,
        [string]$ErrLog
    )
    try {
        $p = Start-Process -FilePath $FilePath -ArgumentList $Arguments `
            -WorkingDirectory $WorkingDirectory -WindowStyle Hidden `
            -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog -PassThru
        return $p
    }
    catch {
        # 个别环境 Start-Process 因 Path/PATH 环境变量键冲突报错，退回 .NET 直接拉起
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $FilePath
        $psi.Arguments = ($Arguments -join " ")
        $psi.WorkingDirectory = $WorkingDirectory
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $proc = [System.Diagnostics.Process]::Start($psi)
        $outFs = [System.IO.File]::Create($OutLog)
        $errFs = [System.IO.File]::Create($ErrLog)
        $null = $proc.StandardOutput.BaseStream.CopyToAsync($outFs)
        $null = $proc.StandardError.BaseStream.CopyToAsync($errFs)
        return $proc
    }
}

function Wait-TemporalServer {
    for ($i = 0; $i -lt 30; $i++) {
        if (Test-TemporalServer) {
            Start-Sleep -Seconds 2  # 端口已通，再给服务器 2 秒完成初始化
            return $true
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Write-Status {
    $server = Test-TemporalServer
    $worker = Get-TemporalWorkerProcess
    if ($server) {
        Write-Host "Temporal 服务器: 运行中 (gRPC $serverAddress, UI http://localhost:8233)"
    }
    else {
        Write-Host "Temporal 服务器: 未运行"
    }
    if ($worker) {
        Write-Host ("Temporal Worker: 运行中 PID=" + (($worker.ProcessId) -join ","))
    }
    else {
        Write-Host "Temporal Worker: 未运行"
    }
}

if ($Status) {
    Write-Status
    exit 0
}

if ($Stop) {
    $worker = Get-TemporalWorkerProcess
    foreach ($w in $worker) {
        Stop-Process -Id $w.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "已停止 Worker PID=$($w.ProcessId)"
    }
    Get-Process -Name temporal -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Host "已停止 Temporal 服务器"
    exit 0
}

# ── 默认：确保都在运行 ───────────────────────────────────

if (-not (Test-Path -LiteralPath $temporalExe)) {
    Write-Error "未找到 $temporalExe，请先下载 Temporal CLI（tools/temporal/temporal.exe）"
    exit 1
}
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Error "未找到 $venvPython，请先创建 .venv 并安装依赖（uv add temporalio）"
    exit 1
}

if (-not (Test-TemporalServer)) {
    Write-Host "启动 Temporal 开发服务器 ..."
    $null = Start-HiddenProcess -FilePath $temporalExe `
        -Arguments @("server", "start-dev", "--namespace", "default", "--db-filename", "temporal.db") `
        -WorkingDirectory $temporalDir -OutLog $serverOut -ErrLog $serverErr
    if (-not (Wait-TemporalServer)) {
        Write-Error "Temporal 服务器启动超时，请查看日志: $serverErr"
        exit 1
    }
    Write-Host "Temporal 服务器已就绪: UI http://localhost:8233"
}
else {
    Write-Host "Temporal 服务器已在运行，跳过启动"
}

if (-not (Get-TemporalWorkerProcess)) {
    Write-Host "启动 Temporal Worker ..."
    $null = Start-HiddenProcess -FilePath $venvPython `
        -Arguments @("-m", "app.agents.orchestration.temporal.worker") `
        -WorkingDirectory $projectRoot -OutLog $workerOut -ErrLog $workerErr
    Start-Sleep -Seconds 5
    if (-not (Get-TemporalWorkerProcess)) {
        Write-Error "Worker 启动失败，请查看日志: $workerErr"
        exit 1
    }
    Write-Host "Temporal Worker 已启动"
}
else {
    Write-Host "Temporal Worker 已在运行，跳过启动"
}

Write-Status
Write-Host ""
Write-Host "完成。办公模式多智能体任务将走 Temporal 编排；日志目录: $temporalDir"
