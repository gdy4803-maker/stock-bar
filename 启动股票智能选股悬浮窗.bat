@echo off
rem StockPick v2.0 launcher (source version)
rem Uses a pythonw.exe if found; falls back to PATH.
setlocal
cd /d "%~dp0"

rem Stop a running source instance so the new code is loaded.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'stock_bar' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

set "PYW=C:\Users\Admin\AppData\Local\Doubao\User Data\sandbox_runtime\bases\c98c5042338ed152c6f10ecd8591889f\python\pythonw.exe"
if exist "%PYW%" (
    start "" "%PYW%" "%~dp0stock_bar.py"
    exit /b 0
)

where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%~dp0stock_bar.py"
    exit /b 0
)

where pyw >nul 2>nul
if not errorlevel 1 (
    start "" pyw "%~dp0stock_bar.py"
    exit /b 0
)

echo [ERROR] pythonw.exe not found. Install Python 3.10+ or set PYW in this file.
pause

