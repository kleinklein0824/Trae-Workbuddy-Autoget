@echo off
chcp 65001 >nul
title Trae ^& Buddy 签到助手
cd /d "%~dp0"

echo ============================================
echo   Trae ^& Buddy 签到助手 - 启动中
echo ============================================
echo.

rem ---- 优先使用 py 启动器，其次 python ----
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [错误] 未检测到 Python。
    echo        请先安装 Python 3.10+ 并勾选 "Add Python to PATH"。
    echo        下载地址: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

rem ---- 依赖自检（requests / playwright），缺失则自动安装 ----
%PY% -c "import requests, playwright" >nul 2>nul
if errorlevel 1 (
    echo [提示] 检测到缺少依赖，正在安装...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [警告] 依赖安装失败，签到模块可能无法运行。
        echo        请手动执行: %PY% -m pip install -r requirements.txt
        echo.
        pause
    )
)

echo [启动] 正在打开界面...
echo.
%PY% main.py

if errorlevel 1 (
    echo.
    echo [错误] 程序异常退出。详情见 startup_error.log
    pause
    exit /b 1
)
exit /b 0
