# 一次設定好：開機自動啟動、每天備份、防火牆開放 443。
# 要用「系統管理員」身分開 PowerShell 執行：右鍵 PowerShell → 以系統管理員身分執行，然後
#   powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1
# 重複執行沒關係（會覆蓋成最新設定）。要移除：工作排程器裡刪掉「AI Relay」「AI Relay Backup」兩個工作。
#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { (Get-Command python).Source }

# 用 SYSTEM 身分跑：開機就啟動，不用有人登入 Windows
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

# 1. 轉發站：開機啟動；異常結束時每 1 分鐘重試；不設執行時間上限
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$root\scripts\start-relay.ps1`"" -WorkingDirectory $root
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "AI Relay" -Action $action -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -Settings $settings -Principal $principal -Force | Out-Null

# 2. 每天凌晨 4 點備份資料庫到 backups\，留最新 14 份
$backup = New-ScheduledTaskAction -Execute $python -Argument "-m app.cli backup --dir backups --keep 14" `
    -WorkingDirectory $root
Register-ScheduledTask -TaskName "AI Relay Backup" -Action $backup -Trigger (New-ScheduledTaskTrigger -Daily -At 4am) `
    -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable) -Principal $principal -Force | Out-Null

# 3. Windows 防火牆開放 HTTPS（443）。本機後台的 8000 不要開
if (-not (Get-NetFirewallRule -DisplayName "AI Relay HTTPS" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "AI Relay HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow | Out-Null
}

Write-Host "完成。現在立刻啟動：Start-ScheduledTask -TaskName 'AI Relay'（或重新開機）"
Write-Host "紀錄檔在 $root\logs，備份在 $root\backups"
