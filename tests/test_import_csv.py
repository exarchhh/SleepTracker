# -*- coding: utf-8 -*-
"""Регрессионные тесты CLI-импорта истории из CSV."""

import sqlite3

from import_history import import_csv, parse_rows


def _write(tmp_path, text, name="history.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_normal_row_imported(tmp_path):
    """Нормальная строка формата Excel (';' разделитель) импортируется."""
    path = _write(tmp_path, "2026-10-15;23:40;40;471;Чт;7,85;\n")
    entries, skipped = parse_rows(path)
    assert entries == [("2026-10-15", "23:40")]
    assert skipped == 0


def test_empty_row_ignored(tmp_path):
    """Пустая заготовка ';;;' игнорируется (но считается пропущенной)."""
    path = _write(tmp_path, "2026-10-20;;;;Вт;0,00;\n")
    entries, skipped = parse_rows(path)
    assert entries == []
    assert skipped == 1


def test_question_marks_ignored(tmp_path):
    """Строка с '???' во времени (ночь без записи) игнорируется."""
    path = _write(tmp_path, "2026-08-09;???;0;372;Вс;6,20;\n")
    entries, skipped = parse_rows(path)
    assert entries == []
    assert skipped == 1


def test_single_digit_hour_normalized(tmp_path):
    """Время '0:19' нормализуется в '00:19'."""
    path = _write(tmp_path, "2026-07-26;0:19;53;113;Вс;1,88;\n")
    entries, _ = parse_rows(path)
    assert entries == [("2026-07-26", "00:19")]


def test_mixed_real_history_file(tmp_path):
    """Файл как у пользователя: пропускаются ??? и пустые заготовки,
    нормализуются одноцифровые часы, порядок сохраняется."""
    text = (
        "2026-07-25;22:41;-19;60;Сб;1,00;\n"
        "2026-07-26;0:19;53;113;Вс;1,88;\n"
        "2026-08-09;???;0;372;Вс;6,20;\n"
        "2026-10-20;;;;Вт;0,00;\n"
    )
    entries, skipped = parse_rows(_write(tmp_path, text))
    assert entries == [
        ("2026-07-25", "22:41"),
        ("2026-07-26", "00:19"),
    ]
    assert skipped == 2


def test_simple_format_with_header(tmp_path):
    """Простой формат day,bedtime с заголовком тоже работает."""
    text = "day,bedtime\n2026-10-20,23:15\n"
    entries, _ = parse_rows(_write(tmp_path, text))
    assert entries == [("2026-10-20", "23:15")]


def test_cp1251_encoded_file(tmp_path):
    """Excel-экспорт в windows-1251 читается корректно."""
    raw = "2026-10-15;23:40;40;471;Чт;7,85;прилет\n".encode("cp1251")
    p = tmp_path / "win.csv"
    p.write_bytes(raw)
    entries, _ = parse_rows(str(p))
    assert entries == [("2026-10-15", "23:40")]


def test_import_writes_to_db(tmp_path, empty_db, monkeypatch):
    """Реальный импорт кладёт записи в БД, дубликаты пропускаются."""
    import app as app_module
    monkeypatch.setattr(app_module, "DB_PATH", empty_db)

    path = _write(tmp_path,
                  "2026-10-15;23:40;40;471;Чт;7,85;\n"
                  "2026-10-16;0:19;79;520;Пт;8,66;\n"
                  "2026-10-17;;;;Сб;0,00;\n")
    rc = import_csv(path)
    assert rc == 0
    conn = sqlite3.connect(empty_db)
    rows = set(conn.execute("SELECT day, bedtime FROM sleep_entries"))
    conn.close()
    assert rows == {("2026-10-15", "23:40"), ("2026-10-16", "00:19")}

    # Повторный импорт идемпотентен — ничего не добавляется
    rc = import_csv(path)
    assert rc == 0
    conn = sqlite3.connect(empty_db)
    rows2 = set(conn.execute("SELECT day, bedtime FROM sleep_entries"))
    conn.close()
    assert rows2 == rows


def test_dry_run_writes_nothing(tmp_path, empty_db, monkeypatch):
    """--dry-run ничего не записывает в базу."""
    import app as app_module
    monkeypatch.setattr(app_module, "DB_PATH", empty_db)

    path = _write(tmp_path, "2026-10-15;23:40;40;471;Чт;7,85;\n")
    rc = import_csv(path, dry_run=True)
    assert rc == 0
    conn = sqlite3.connect(empty_db)
    rows = list(conn.execute("SELECT day, bedtime FROM sleep_entries"))
    conn.close()
    assert rows == []
