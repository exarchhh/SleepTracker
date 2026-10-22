@echo off
REM SleepTracker: автозапуск локального сервера (Windows)
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
    python launcher.py
) else (
    py launcher.py
)
pause
