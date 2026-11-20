# 啟動轉發站（正式模式：HTTPS + 本機後台）。由「工作排程器」在開機時執行，也可以手動執行。
# 輸出寫進 logs\ 資料夾：relay-日期.out.log（一般訊息）、relay-日期.err.log（Python 的紀錄和錯誤都在這）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 有虛擬環境就用虛擬環境的 Python，沒有就用系統的
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { (Get-Command python).Source }

New-Item -ItemType Directory -Force (Join-Path $root "logs") | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$env:PYTHONIOENCODING = "utf-8"  # 紀錄裡的中文才不會變亂碼

$proc = Start-Process -FilePath $python -ArgumentList "-m", "app.serve" -WorkingDirectory $root -NoNewWindow -Wait `
    -PassThru -RedirectStandardOutput (Join-Path $root "logs\relay-$stamp.out.log") `
    -RedirectStandardError (Join-Path $root "logs\relay-$stamp.err.log")
# 不是正常結束 → 回傳錯誤碼，工作排程器看到失敗會在 1 分鐘後自動重啟
exit $proc.ExitCode
