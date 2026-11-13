# -*- coding: utf-8 -*-
"""Тесты валидации ввода /save: flash-сообщения и даты из будущего.

Заодно — рендер данных графика через `| tojson` (вместо
json.dumps + | safe) на роуте index.
"""

import sqlite3
from datetime import date, timedelta


def _count(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM sleep_entries").fetchone()[0]
    finally:
        conn.close()


def test_save_invalid_format_flashes(app_client, empty_db):
    """Невалидный формат -> flash с текстом ошибки, записи в БД нет."""
    resp = app_client.post("/save",
                           data={"day": "2026-10-20", "bedtime": "25:99"},
                           follow_redirects=True)
    assert resp.status_code == 200
    assert "Некорректная дата или время" in resp.get_data(as_text=True)
    assert _count(empty_db) == 0


def test_save_future_date_rejected(app_client, empty_db):
    """Завтрашняя дата -> отклонена с flash, БД не изменилась."""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    resp = app_client.post("/save",
                           data={"day": tomorrow, "bedtime": "23:15"},
                           follow_redirects=True)
    assert resp.status_code == 200
    assert ("Нельзя добавлять записи с датой в будущем"
            in resp.get_data(as_text=True))
    assert _count(empty_db) == 0


def test_save_today_allowed(app_client, empty_db):
    """Сегодняшняя дата разрешена — запись сохраняется."""
    today = date.today().isoformat()
    resp = app_client.post("/save", data={"day": today, "bedtime": "23:15"})
    assert resp.status_code == 302
    assert _count(empty_db) == 1


def test_index_renders_chart_data_via_tojson(app_client, empty_db):
    """GET / при непустой БД: chartData — JSON-массив через tojson."""
    app_client.post("/save", data={"day": "2026-10-20", "bedtime": "23:15"})
    app_client.post("/save", data={"day": "2026-10-21", "bedtime": "23:30"})
    resp = app_client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "chartData = [" in html
    assert "| safe" not in html
    assert "chart_data_json" not in html


def test_index_empty_db_has_no_chart_block(app_client, empty_db):
    """GET / при пустой БД: 200, заглушка «добавьте запись», без chartData."""
    resp = app_client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "chartData" not in html
    assert "Добавьте первую запись" in html
