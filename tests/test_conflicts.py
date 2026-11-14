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


def test_evening_and_night_replaced(app_client, empty_db):
    """Инвариант «один отбой на вечер» (R1-plus, v0.11.0): добавление ночной
    записи (21.10, 01:19 — вечер 20.10) ЗАМЕНЯЕТ вечернюю запись того же
    вечера (20.10, 23:40). Сосуществование вечер+ночь запрещено.
    """
    app_client.post("/save", data={"day": "2026-10-20", "bedtime": "23:40"})
    app_client.post("/save", data={"day": "2026-10-21", "bedtime": "01:19"})
    rows = _rows(empty_db)
    assert rows == {("2026-10-21", "01:19")}


def test_edit_evening_to_night_replaces_same_evening_night(app_client, empty_db):
    """Правка «23:40 -> 01:19» при неизменённой дате не меняет вечер (R1-plus).

    Ролл 0->1: итоговая дата old_day + 1 день — ночной слот ТОГО ЖЕ вечера.
    Ночная запись (15.10, 00:30) принадлежит вечеру 14.10 — другому вечеру,
    не тронута; edit-путь не ставит пометок в deleted_nights.
    """
    # Ночная запись соседнего вечера 14.10: 15.10 00:30 (проверяем, что
    # очистка вечера 15.10 её не задевает: дата 15.10, но время до 12:00 —
    # это ночной слот ДРУГОГО вечера). Вечернюю запись вечера 14.10 в сетап
    # добавить нельзя: тот же вечер, что и 00:30, — инвариант Правила 2
    # (заменила бы её).
    app_client.post("/save", data={"day": "2026-10-15", "bedtime": "00:30"})
    # Вечерняя запись вечера 15.10
    app_client.post("/save", data={"day": "2026-10-15", "bedtime": "23:40"})
    # Инлайн-правка: вечерняя 23:40 на ночное 01:19, дата не тронута
    app_client.post("/save", data={
        "day": "2026-10-15", "bedtime": "01:19",
        "old_day": "2026-10-15", "old_bedtime": "23:40",
    })
    rows = _rows(empty_db)
    # Ролл: запись стала ночным слотом того же вечера 15.10 -> дата 16.10;
    # соседняя ночь вечера 14.10 не тронута
    assert rows == {("2026-10-15", "00:30"), ("2026-10-16", "01:19")}
    # Edit-путь не помечает вечера: deleted_nights пуст
    conn = sqlite3.connect(empty_db)
    deleted = conn.execute(
        "SELECT COUNT(*) FROM deleted_nights").fetchone()[0]
    conn.close()
    assert deleted == 0


def test_edit_evening_to_night_creates_new_entity_when_free(app_client, empty_db):
    """Ролл-правка «23:40 -> 01:19» без соседей: итог (16.10, 01:19) —
    ночной слот того же вечера 15.10; в истории запись под своим вечером.
    """
    app_client.post("/save", data={"day": "2026-10-15", "bedtime": "23:40"})
    app_client.post("/save", data={
        "day": "2026-10-15", "bedtime": "01:19",
        "old_day": "2026-10-15", "old_bedtime": "23:40",
    })
    rows = _rows(empty_db)
    assert rows == {("2026-10-16", "01:19")}
    # В истории запись отображается под датой вечера 15.10
    resp = app_client.get("/")
    html = resp.data.decode("utf-8")
    assert "15.10.2026" in html and "01:19" in html


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


def test_ghost_night_save_flashes_reason(app_client, empty_db):
    """Ghost-сохранение показывает flash с объяснением (v0.9.0).

    Создаём ночную запись → удаляем её (в deleted_nights появляется строка) →
    повторный POST той же ночи: 302, при переходе по редиректу видна плашка
    с текстом блокировки, записи в БД нет.
    """
    app_client.post("/save", data={"day": "2026-10-16", "bedtime": "00:30"})
    app_client.post("/delete", data={"day": "2026-10-16", "bedtime": "00:30"})
    conn = sqlite3.connect(empty_db)
    ghosts = conn.execute(
        "SELECT COUNT(*) FROM deleted_nights").fetchone()[0]
    conn.close()
    assert ghosts == 1

    resp = app_client.post("/save",
                           data={"day": "2026-10-16", "bedtime": "00:30"},
                           follow_redirects=True)
    assert resp.status_code == 200
    assert ("Эта ночная запись была удалена ранее — сохранение отклонено"
            in resp.get_data(as_text=True))
    assert _rows(empty_db) == set()


def test_unlock_night_offered_after_delete(app_client, empty_db):
    """После удаления ночной записи GET / предлагает разблокировку вечера.

    Ночь 16.10 00:30 принадлежит вечеру 15.10: на странице появляется
    «Разрешить повторный ввод» и дата вечера 15.10.2026.
    """
    app_client.post("/save", data={"day": "2026-10-16", "bedtime": "00:30"})
    app_client.post("/delete", data={"day": "2026-10-16", "bedtime": "00:30"})
    resp = app_client.get("/")
    html = resp.get_data(as_text=True)
    assert "Разрешить повторный ввод" in html
    assert "15.10.2026" in html


def test_unlock_night_allows_reentry(app_client, empty_db):
    """Разблокировка снимает ghost-память: повторный ввод ночи сохраняется.

    Удалённая ночь 16.10 00:30 (вечер 15.10) после POST /unlock_night с
    evening_day=2026-10-15 сохраняется повторно, строка из deleted_nights
    исчезает.
    """
    app_client.post("/save", data={"day": "2026-10-16", "bedtime": "00:30"})
    app_client.post("/delete", data={"day": "2026-10-16", "bedtime": "00:30"})
    conn = sqlite3.connect(empty_db)
    before = conn.execute("SELECT COUNT(*) FROM deleted_nights").fetchone()[0]
    conn.close()
    assert before == 1

    resp = app_client.post("/unlock_night",
                           data={"evening_day": "2026-10-15"})
    assert resp.status_code == 302

    resp = app_client.post("/save",
                           data={"day": "2026-10-16", "bedtime": "00:30"},
                           follow_redirects=True)
    assert resp.status_code == 200
    assert ("Эта ночная запись была удалена ранее — сохранение отклонено"
            not in resp.get_data(as_text=True))
    assert _rows(empty_db) == {("2026-10-16", "00:30")}
    conn = sqlite3.connect(empty_db)
    after = conn.execute("SELECT COUNT(*) FROM deleted_nights").fetchone()[0]
    conn.close()
    assert after == 0


def test_unlock_night_guarded(app_client, empty_db):
    """POST /unlock_night под guard: чужой Origin -> 403."""
    resp = app_client.post("/unlock_night",
                           data={"evening_day": "2026-10-15"},
                           headers={"Origin": "http://evil.com"})
    assert resp.status_code == 403


def test_unlock_night_idempotent(app_client, empty_db):
    """Несуществующий evening_day: 302, состояние БД не меняется.

    Память о РЕАЛЬНОМ удалённом вечере при этом не снимается (ключ вечера
    не совпал) — идемпотентность и точечность операции.
    """
    app_client.post("/save", data={"day": "2026-10-16", "bedtime": "00:30"})
    app_client.post("/delete", data={"day": "2026-10-16", "bedtime": "00:30"})

    resp = app_client.post("/unlock_night", data={"evening_day": "2099-01-01"})
    assert resp.status_code == 302

    from core import evening_anchor, night_key
    real_key = str(night_key(evening_anchor("2026-10-15", "12:00")))
    conn = sqlite3.connect(empty_db)
    ghosts = {r[0] for r in
              conn.execute("SELECT evening_day FROM deleted_nights")}
    conn.close()
    assert ghosts == {real_key}


def test_delete_removes_only_target_entry(app_client, empty_db):
    """Удаление одной записи не затрагивает другие (два разных вечера).

    Сетап на двух ВЕЧЕРНИХ записях разных вечеров: при R1-plus добавление
    ночной записи заменяет вечернюю запись того же вечера, поэтому пары
    «вечер+ночь одного вечера» в БД не бывает.
    """
    app_client.post("/save", data={"day": "2026-10-20", "bedtime": "23:40"})
    app_client.post("/save", data={"day": "2026-10-21", "bedtime": "23:05"})
    app_client.post("/delete", data={"day": "2026-10-21", "bedtime": "23:05"})
    assert _rows(empty_db) == {("2026-10-20", "23:40")}


# --- R1-plus: регресс-сценарий владельца и пограничные случаи ---------------

def test_owner_regression_full_scenario(app_client, empty_db):
    """Сценарий владельца целиком (регресс v0.11.0): POST (12.11, 22:24) ->
    POST (13.11, 23:45) -> инлайн-правка (13.11, 23:45) на 01:30. Раньше
    правка переносила запись на вечер 12.11 (две записи «одного отхода ко
    сну») и скрыто метила вечер 13.11; теперь ролл даёт (14.11, 01:30) —
    ночной слот того же вечера 13.11, deleted_nights пуст.
    """
    app_client.post("/save", data={"day": "2026-11-12", "bedtime": "22:24"})
    app_client.post("/save", data={"day": "2026-11-13", "bedtime": "23:45"})
    app_client.post("/save", data={
        "day": "2026-11-13", "bedtime": "01:30",
        "old_day": "2026-11-13", "old_bedtime": "23:45",
    })
    assert _rows(empty_db) == {("2026-11-12", "22:24"), ("2026-11-14", "01:30")}
    conn = sqlite3.connect(empty_db)
    ghosts = conn.execute(
        "SELECT COUNT(*) FROM deleted_nights").fetchone()[0]
    conn.close()
    assert ghosts == 0


def test_roll_night_to_evening(app_client, empty_db):
    """Ролл 1->0: ночная запись (13.11, 01:30 — вечер 12.11), правка времени
    на 23:00 при неизменённой дате -> вечерний слот ТОГО ЖЕ вечера 12.11.
    """
    app_client.post("/save", data={"day": "2026-11-13", "bedtime": "01:30"})
    app_client.post("/save", data={
        "day": "2026-11-13", "bedtime": "23:00",
        "old_day": "2026-11-13", "old_bedtime": "01:30",
    })
    assert _rows(empty_db) == {("2026-11-12", "23:00")}


def test_explicit_date_change_no_roll(app_client, empty_db):
    """Явная смена даты при правке сохраняет перенос без ролла: правка
    (13.11, 23:45) на day=14.11, bedtime=01:30 -> ровно (14.11, 01:30),
    без двойного сдвига (не 15.11).
    """
    app_client.post("/save", data={"day": "2026-11-13", "bedtime": "23:45"})
    app_client.post("/save", data={
        "day": "2026-11-14", "bedtime": "01:30",
        "old_day": "2026-11-13", "old_bedtime": "23:45",
    })
    assert _rows(empty_db) == {("2026-11-14", "01:30")}


def test_roll_replaces_night_of_target_evening(app_client, empty_db):
    """Ролл-правка заменяет ночную запись целевого вечера: сетап (15.10,
    23:40), (16.10, 02:00); правка 23:40 -> 01:19 -> {(16.10, 01:19)}.
    """
    app_client.post("/save", data={"day": "2026-10-15", "bedtime": "23:40"})
    app_client.post("/save", data={"day": "2026-10-16", "bedtime": "02:00"})
    app_client.post("/save", data={
        "day": "2026-10-15", "bedtime": "01:19",
        "old_day": "2026-10-15", "old_bedtime": "23:40",
    })
    assert _rows(empty_db) == {("2026-10-16", "01:19")}


def test_roll_to_ghost_evening_blocked(app_client, empty_db):
    """Ролл 0->1 на ghost-вечер блокируется ДО мутаций: ночная (16.10, 02:00)
    -> DELETE (16.10, 02:00) метит вечер 15.10 -> вечерняя (15.10, 23:40);
    правка 23:40 -> 01:19 -> ghost-flash, БД не изменилась.

    Сетап: ночь создаётся и удаляется ДО вечерней (обе — слоты вечера 15.10,
    по Правилу 2 одновременное сосуществование невозможно).
    """
    app_client.post("/save", data={"day": "2026-10-16", "bedtime": "02:00"})
    app_client.post("/delete", data={"day": "2026-10-16", "bedtime": "02:00"})
    app_client.post("/save", data={"day": "2026-10-15", "bedtime": "23:40"})
    assert _rows(empty_db) == {("2026-10-15", "23:40")}

    resp = app_client.post("/save", data={
        "day": "2026-10-15", "bedtime": "01:19",
        "old_day": "2026-10-15", "old_bedtime": "23:40",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert ("Эта ночная запись была удалена ранее — сохранение отклонено"
            in resp.get_data(as_text=True))
    assert _rows(empty_db) == {("2026-10-15", "23:40")}


def test_roll_to_tomorrow_rejected_as_future(app_client, empty_db, monkeypatch):
    """Пограничный случай Правила 1: ролл вечерней записи сегодняшнего вечера
    на ночное время до смены календарной даты даёт итоговую дату «завтра» ->
    отклоняется плашкой «Нельзя добавлять записи с датой в будущем» (то же,
    что в форме добавления; после полуночи та же правка проходит).
    """
    import app as app_module
    from datetime import date as real_date

    class FakeDate(real_date):
        @classmethod
        def today(cls):
            return real_date(2026, 11, 13)

    monkeypatch.setattr(app_module, "_date", FakeDate)

    app_client.post("/save", data={"day": "2026-11-13", "bedtime": "23:45"})
    resp = app_client.post("/save", data={
        "day": "2026-11-13", "bedtime": "01:30",
        "old_day": "2026-11-13", "old_bedtime": "23:45",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert ("Нельзя добавлять записи с датой в будущем"
            in resp.get_data(as_text=True))
    assert _rows(empty_db) == {("2026-11-13", "23:45")}
