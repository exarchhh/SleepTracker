# -*- coding: utf-8 -*-
"""Точка входа для PyInstaller-сборки SleepTracker.

Маршрутизация:
    sleeptracker.exe              -> запуск локального Flask-сервера
    sleeptracker.exe --import c.csv -> импорт истории из CSV (CLI)
"""

import sys


def run_server():
    from app import app
    app.run(host="127.0.0.1", port=5000, debug=False)


def main():
    args = sys.argv[1:]
    if args and args[0] == "--import":
        from import_history import import_csv
        if len(args) < 2:
            print("Использование: sleeptracker --import <файл.csv>")
            return 2
        return import_csv(args[1])
    run_server()
    return 0


if __name__ == "__main__":
    sys.exit(main())
