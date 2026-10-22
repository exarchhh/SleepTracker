@echo off
REM Сборка SleepTracker в .exe через PyInstaller
cd /d "%~dp0"

pip install pyinstaller || goto :err
pyinstaller --onefile --name sleeptracker ^
    --hidden-import matplotlib ^
    --collect-submodules matplotlib ^
    launcher.py || goto :err

echo.
echo Готово: dist\sleeptracker.exe
goto :eof

:err
echo Ошибка сборки.
exit /b 1
