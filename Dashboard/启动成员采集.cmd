@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 member_client.py
) else (
    python member_client.py
)
pause
