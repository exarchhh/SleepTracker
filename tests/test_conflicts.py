# -*- coding: utf-8 -*-
"""Регрессионные тесты конфликтов слотов записей (правило «один отбой в дату»).

Тестируется через HTTP-роуты /save и /delete на временной БД, чтобы
проверять реальное поведение приложения, а не только чистые функции.
"""

import sqlite3


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {(r[0], r[1]) for r in
                conn.execute("SELECT day, bedtime FROM sleep_entries")}
    finally:
        conn.close()


def test_two_evening_entries_conflict(app_client, empty_db):
    """Две вечерние записи одной даты → конфликт: остаётся только новая."""
    app_client.post("/save", data={"day": "2026-10-20", "bedtime": "23:15"})
    app_client.post("/save", data={"day": "2026-10-20", "bedtime": "23:45"})
    rows = _rows(empty_db)
    assert ("2026-10-20", "23:15") not in rows
    assert rows == {("2026-10-20", "23:45")}


def test_evening_and_night_allowed(app_client, empty_db):
    """Вечерняя запись дня D + ночная запись дня D+1 сосуществуют (один вечер)."""
    app_client.post("/save", data={"day": "2026-10-20", "bedtime": "23:40"})
    app_client.post("/save", data={"day": "2026-10-21", "bedtime": "01:19"})
    rows = _rows(empty_db)
    assert rows == {("2026-10-20", "23:40"), ("2026-10-21", "01:19")}


def test_edit_evening_to_night_replaces_same_evening_night(app_client, empty_db):
    """Правка «15.10 23:40 -> 15.10 01:19» — обычная правка (вариант A).

    Утверждённая логика: внесение данных всегда отталкивается от фактической
    даты отбоя; запись 15.10 01:19 относится к вечеру 14.10 и является её
    обычной правкой. Если на дате новой записи уже есть ночная запись того же
    вечера (15.10 00:30 — ночь вечера 14.10), она заменяется новой, а не
    блокируется. Записи других вечеров при этом не трогаются.
    """
    # Ночная запись того же вечера (вечер 14.10): 15.10 00:30
    app_client.post("/save", data={"day": "2026-10-15", "bedtime": "00:30"})
    # Вечерняя запись вечера 15.10
    app_client.post("/save", data={"day": "2026-10-15", "bedtime": "23:40"})
    # Соседний вечер 13.10 (вечерняя запись) — не должен быть затронут
    app_client.post("/save", data={"day": "2026-10-14", "bedtime": "23:00"})
    # Инлайн-правка: вечерняя 23:40 превращается в ночную 01:19 той же даты
    app_client.post("/save", data={
        "day": "2026-10-15", "bedtime": "01:19",
        "old_day": "2026-10-15", "old_bedtime": "23:40",
    })
    rows = _rows(empty_db)
    # Ночная запись того же вечера заменена (UPDATE): 00:30 -> 01:19
    assert ("2026-10-15", "01:19") in rows
    assert ("2026-10-15", "00:30") not in rows
    # Старая вечерняя запись удалена
    assert ("2026-10-15", "23:40") not in rows
    # Запись соседнего вечера (13.10) не тронута
    assert ("2026-10-14", "23:00") in rows
    # Память об удалённой ночи для этого вечера снята (слот занят легально)
    conn = sqlite3.connect(empty_db)
    deleted = {r[0] for r in conn.execute(
        "SELECT evening_day FROM deleted_nights")}
    conn.close()
    from app import evening_anchor, night_key
    assert night_key(evening_anchor("2026-10-15", "01:19")) not in deleted


def test_edit_evening_to_night_creates_new_entity_when_free(app_client, empty_db):
    """Если ночной записи того же вечера нет — правка создаёт её как новую
    сущность вечера предыдущего дня (вечер 14.10), а вечерняя 23:40 удаляется.
    """
    app_client.post("/save", data={"day": "2026-10-15", "bedtime": "23:40"})
    app_client.post("/save", data={
        "day": "2026-10-15", "bedtime": "01:19",
        "old_day": "2026-10-15", "old_bedtime": "23:40",
    })
    rows = _rows(empty_db)
    assert rows == {("2026-10-15", "01:19")}
    # В истории запись должна отображаться под датой вечера 14.10
    resp = app_client.get("/")
    html = resp.data.decode("utf-8")
    assert "14.10.2026" in html and "01:19" in html


def test_ghost_night_blocks_slot(app_client, empty_db):
    """Попытка занять слот удалённой ночи → блокировка (ночь не воскресает).

    После удаления ночной записи 16.10 00:30 (вечер 15.10) повторное
    сохранение той же пары день+время не должно создавать запись заново.
    """
    app_client.post("/save", data={"day": "2026-10-16", "bedtime": "00:30"})
    app_client.post("/delete", data={"day": "2026-10-16", "bedtime": "00:30"})
    assert ("2026-10-16", "00:30") not in _rows(empty_db)

    # Повторная попытка занять этот ночной слот — блокируется
    app_client.post("/save", data={"day": "2026-10-16", "bedtime": "00:30"})
    assert ("2026-10-16", "00:30") not in _rows(empty_db)

    # При этом обычные записи работают: другой вечер можно создать
    app_client.post("/save", data={"day": "2026-10-17", "bedtime": "23:05"})
    assert ("2026-10-17", "23:05") in _rows(empty_db)


def test_delete_removes_only_target_entry(app_client, empty_db):
    """Удаление одной записи не затрагивает другие (вечер/ночь одного дня)."""
    app_client.post("/save", data={"day": "2026-10-20", "bedtime": "23:40"})
    app_client.post("/save", data={"day": "2026-10-21", "bedtime": "01:19"})
    app_client.post("/delete", data={"day": "2026-10-21", "bedtime": "01:19"})
    assert _rows(empty_db) == {("2026-10-20", "23:40")}
