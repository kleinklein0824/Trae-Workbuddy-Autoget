@echo off
chcp 65001 >nul
title Daily Checkin - TraeWork + TraeCN + WorkBuddy
cd /d "%~dp0"
set "PY=C:\Program Files\Python313\python.exe"
if not exist "%PY%" set "PY=python"
echo ============================================
echo   Daily Checkin: TraeWork + TraeCN + WorkBuddy
echo   (output of python below is in Chinese)
echo --------------------------------------------
echo   Offline? The script keeps sniffing the
echo   network (every 30s, up to 12h) and starts
echo   automatically once connected. Ctrl+C to
echo   cancel the wait.
echo ============================================
"%PY%" "%~dp0checkin.py" run
echo.
echo ------ Done. Details in logs\. Press any key to close ------
pause >nul
