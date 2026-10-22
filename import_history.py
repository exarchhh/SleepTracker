# -*- coding: utf-8 -*-
"""CLI-утилита импорта истории сна из CSV.

Формат CSV (с заголовком или без): day,bedtime
    2026-10-20,23:15
    2026-10-21,00:40

Использование:
    python import_history.py history.csv
"""

import csv
import sys

from app import DB_PATH, evening_anchor, get_db


def _valid(day, bedtime):
    try:
        evening_anchor(day, bedtime)
        return True
    except ValueError:
        return False


def import_csv(path):
    """Импортирует записи из CSV-файла в базу. Возвращает код выхода."""
    entries = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        for row in reader:
            if len(row) < 2:
                continue
            day, bedtime = row[0].strip(), row[1].strip()
            if day.lower() == "day":  # строка заголовка
                continue
            if not _valid(day, bedtime):
                print("Пропущена некорректная строка: %s , %s" % (day, bedtime))
                continue
            entries.append((day, bedtime))

    if not entries:
        print("Нет корректных записей для импорта.")
        return 1

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
    return 0


def main(argv):
    if len(argv) < 2:
        print("Использование: python import_history.py <файл.csv>")
        return 2
    return import_csv(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
