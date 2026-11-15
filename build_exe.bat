@echo off
REM Сборка SleepTracker в .exe через PyInstaller
cd /d "%~dp0"

pip install pyinstaller || goto :err
REM --onedir (актуальный layout PyInstaller 6): exe и папка _internal лежат
REM в dist\SleepTracker\, база sleep.db создаётся РЯДОМ С EXE (не внутри
REM временной папки), поэтому история переживает перезапуск и обновление
REM программы.
pyinstaller --onedir --name SleepTracker ^
    --add-data "templates;templates" ^
    --add-data "static;static" ^
    --hidden-import core ^
    launcher.py || goto :err

echo.
echo Готово: dist\SleepTracker\SleepTracker.exe
echo   База данных sleep.db будет создана рядом с exe.
goto :eof

:err
echo Ошибка сборки.
exit /b 1
