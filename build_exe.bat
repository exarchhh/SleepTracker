@echo off
REM Сборка SleepTracker в .exe через PyInstaller
cd /d "%~dp0"

pip install pyinstaller || goto :err
REM --onedir: exe и папка sleeptracker_files лежат в dist\, база sleep.db
REM создаётся РЯДОМ С EXE (не внутри временной папки), поэтому история
REM переживает перезапуск и обновление программы.
pyinstaller --onedir --name sleeptracker ^
    --hidden-import matplotlib ^
    --collect-submodules matplotlib ^
    launcher.py || goto :err

echo.
echo Готово: dist\sleeptracker\sleeptracker.exe
echo   База данных sleep.db будет создана рядом с exe.
goto :eof

:err
echo Ошибка сборки.
exit /b 1
