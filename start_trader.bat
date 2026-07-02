@echo off
title BTC_5MIN Trading Bot & Dashboard
echo ==================================================
echo [BTC_5MIN] Starting Trading Bot and Dashboard...
echo ==================================================
cd /d "%~dp0"
python main.py
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] The bot exited with an error (code: %errorlevel%).
) else (
    echo.
    echo [INFO] The bot stopped cleanly.
)
echo.
pause
