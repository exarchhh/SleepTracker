# -*- coding: utf-8 -*-
"""CLI-утилита импорта истории сна из CSV.

Поддерживает два формата:

1) Простой (с заголовком или без): day,bedtime
    2026-10-20,23:15

2) Excel-экспорт пользователя (разделитель ';' или ','):
    2026-10-15;0:19;79;2520;Чт;42,00;
   Колонки: дата; отбой; дельта; штраф; день недели; часы; комментарий.
   Пустые строки (заготовки вперёд в Excel) и строки с «???» в колонке
   времени игнорируются.

Использование:
    python import_history.py history.csv [--dry-run]
"""

import csv
import re
import sys


def _reconfigure_stdout():
    """Не даёт кракозябрам появиться в консоли.

    Windows-консоли с растровой кодировкой cp866/cp437 (часто в Git Bash)
    не могут напечатать русские буквы через utf-8. Если stdout не может
    кодировать кириллицу — переключаем его на windows-1251, иначе Git Bash /
    PowerShell корректно интерпретируют вывод; если и это не помогает —
    заменяем неразрешимые символы на '?', чтобы импорт не падал.
    """
    for enc in ("utf-8", "cp1251"):
        try:
            sys.stdout.reconfigure(encoding=enc)
            sys.stderr.reconfigure(encoding=enc)
            "Тест".encode(enc)
            return
        except (UnicodeEncodeError, LookupError, ValueError, OSError):
            continue
    try:
        sys.stdout.reconfigure(errors="replace")
    except (ValueError, OSError):
        pass


_reconfigure_stdout()

from app import DB_PATH, evening_anchor, get_db, init_db  # noqa: E402

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")


def _valid(day, bedtime):
    try:
        evening_anchor(day, bedtime)
        return True
    except ValueError:
        return False


def _read_text(path):
    """Читает CSV, подбирая кодировку: UTF-8 (с BOM и без), затем cp1251.

    Excel при сохранении CSV часто пишет BOM, а русские комментарии — в
    windows-1251; раньше это ломало разбор строк и вывод в консоли.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_rows(path):
    """Разбирает CSV и возвращает список корректных пар (day, bedtime)."""
    entries = []
    skipped_empty = 0
    text = _read_text(path)
    lines = text.splitlines()
    if lines:
        delimiter = ";" if ";" in lines[0] else ","
        rows = csv.reader(lines, delimiter=delimiter)
        for row in rows:
            if len(row) < 2:
                continue
            day, bedtime = row[0].strip(), row[1].strip()
            if day.lower() == "day":  # строка заголовка простого формата
                continue
            if not DATE_RE.match(day):
                continue
            # Пустая заготовка или «???» (ночь без записи) — пропускаем.
            if not bedtime or "?" in bedtime:
                skipped_empty += 1
                continue
            if not TIME_RE.match(bedtime):
                print("Пропущена некорректная строка: %s ; %s" % (day, bedtime))
                continue
            # Нормализуем время: "0:19" -> "00:19"
            h, m = bedtime.split(":")
            bedtime_norm = "%02d:%s" % (int(h), m)
            if not _valid(day, bedtime_norm):
                print("Пропущена некорректная строка: %s ; %s" % (day, bedtime))
                continue
            entries.append((day, bedtime_norm))
    return entries, skipped_empty


def import_csv(path, dry_run=False):
    """Импортирует записи из CSV-файла в базу. Возвращает код выхода."""
    entries, skipped_empty = parse_rows(path)

    if not entries:
        print("Нет корректных записей для импорта.")
        return 1

    print("Найдено записей: %d, пропущено пустых заготовок: %d."
          % (len(entries), skipped_empty))
    if dry_run:
        for day, bedtime in entries:
            print("  %s %s" % (day, bedtime))
        print("--dry-run: ничего не записано.")
        return 0

    init_db()  # гарантирует существование таблицы при первом импорте
    conn = get_db()
    try:
        added = 0
        for day, bedtime in entries:
            cur = conn.execute(
                "INSERT OR IGNORE INTO sleep_entries (day, bedtime) VALUES (?, ?)",
                (day, bedtime))
            added += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    print("Импортировано записей: %d (дубликаты пропущены)." % added)
    print("База данных: %s" % DB_PATH)
    return 0


def main(argv):
    args = [a for a in argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in argv[1:]
    if len(args) < 1:
        print("Использование: python import_history.py <файл.csv> [--dry-run]")
        return 2
    return import_csv(args[0], dry_run=dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
