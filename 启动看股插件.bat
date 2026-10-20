@echo off
rem StockBar - 源码启动脚本
setlocal
cd /d "%~dp0"

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

echo [ERROR] pythonw.exe not found. Install Python 3.10+ and add it to PATH.
pause
