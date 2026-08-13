@echo off
rem 双击一键启动 Lumi 的 Temporal 调试环境（服务器 + Worker）
rem 已运行则跳过，未运行则后台拉起
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_temporal.ps1" %*
if errorlevel 1 (
  echo.
  echo 启动失败，请查看上方错误信息或 tools\temporal 下的日志。
  pause
)
