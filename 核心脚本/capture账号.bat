@echo off
chcp 65001 >nul
title WorkBuddy Account Capture (TraeCN auto-detect)
cd /d "%~dp0"
set "PY=C:\Program Files\Python313\python.exe"
if not exist "%PY%" set "PY=python"
echo ============================================
echo   WorkBuddy multi-account capture
echo ============================================
echo Steps:
echo   1. Login account A in WorkBuddy desktop app
echo   2. Run this script  (double-click)
echo   3. Switch to account B in the app, run again
echo   4. Repeat for more accounts
echo.
echo Note: TraeWork uses browser profiles and TraeCN
echo       auto-reads tokens from IDE logs, so neither
echo       needs capture.
"%PY%" "%~dp0checkin.py" capture
echo.
echo ------ Done. Press any key to close ------
pause >nul
