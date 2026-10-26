# -*- coding: utf-8 -*-
"""Точка входа для PyInstaller-сборки SleepTracker.

Маршрутизация:
    sleeptracker.exe              -> запуск локального Flask-сервера
                                     + автоматическое открытие браузера
    sleeptracker.exe --import c.csv -> импорт истории из CSV (CLI)
"""

import socket
import sys
import threading
import time
import webbrowser

PORT = 5000


def _find_free_port(start=PORT, tries=20):
    """Возвращает первый свободный TCP-порт, начиная с `start`."""
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def open_browser_later(url, port):
    """Открывает браузер после того, как сервер поднимется (до 10 секунд)."""
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.2)
    try:
        webbrowser.open(url)
    except Exception:
        pass  # браузер не критичен: сервер всё равно работает


def run_server():
    from app import app, init_db
    init_db()
    port = _find_free_port()
    url = "http://127.0.0.1:%d/" % port
    print("SleepTracker запущен: %s" % url)
    threading.Thread(target=open_browser_later, args=(url, port),
                     daemon=True).start()
    app.run(host="127.0.0.1", port=port, debug=False)


def main():
    args = sys.argv[1:]
    if args and args[0] == "--import":
        rest = [a for a in args[1:] if a != "--dry-run"]
        dry_run = "--dry-run" in args[1:]
        if len(rest) < 1:
            print("Использование: sleeptracker --import <файл.csv> [--dry-run]")
            return 2
        from import_history import import_csv
        return import_csv(rest[0], dry_run=dry_run)
    run_server()
    return 0


if __name__ == "__main__":
    sys.exit(main())
