# -*- coding: utf-8 -*-
"""Тесты guard'а POST-роутов: Host / Origin / Sec-Fetch-Site.

Guard закрывает CSRF (запросы с чужих сайтов) и DNS-rebinding (подменённый
Host). Отсутствие Origin/Sec-Fetch-Site (curl, тест-клиент) разрешено —
проверяется только Host. GET-роуты guard'ом не трогаются.
"""

import sqlite3

FORM = {"day": "2026-10-20", "bedtime": "23:15"}


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {(r[0], r[1]) for r in
                conn.execute("SELECT day, bedtime FROM sleep_entries")}
    finally:
        conn.close()


def test_save_rejects_foreign_origin(app_client, empty_db):
    """POST /save с Origin чужого сайта -> 403, запись не создаётся."""
    resp = app_client.post("/save", data=FORM,
                           headers={"Origin": "http://evil.com"})
    assert resp.status_code == 403
    assert _rows(empty_db) == set()


def test_save_rejects_foreign_host(app_client, empty_db):
    """POST /save с подменённым Host (DNS-rebinding) -> 403."""
    resp = app_client.post("/save", data=FORM,
                           headers={"Host": "evil.com"})
    assert resp.status_code == 403
    assert _rows(empty_db) == set()


def test_save_rejects_cross_site_fetch(app_client, empty_db):
    """POST /save с Sec-Fetch-Site: cross-site -> 403."""
    resp = app_client.post("/save", data=FORM,
                           headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403
    assert _rows(empty_db) == set()


def test_save_accepts_same_origin_browser_headers(app_client, empty_db):
    """Легитимный браузерный запрос same-origin -> 302, запись в БД."""
    resp = app_client.post(
        "/save", data=FORM,
        headers={"Origin": "http://127.0.0.1:5000",
                 "Sec-Fetch-Site": "same-origin"})
    assert resp.status_code == 302
    assert ("2026-10-20", "23:15") in _rows(empty_db)


def test_save_without_origin_and_fetch_site(app_client, empty_db):
    """curl/тест-клиент: Origin и Sec-Fetch-Site отсутствуют — работает."""
    resp = app_client.post("/save", data=FORM)
    assert resp.status_code == 302
    assert ("2026-10-20", "23:15") in _rows(empty_db)


def test_delete_rejects_any_violation(app_client, empty_db):
    """POST /delete: нарушение любого из трёх проверок -> 403."""
    app_client.post("/save", data=FORM)
    for headers in ({"Origin": "http://evil.com"},
                    {"Host": "evil.com"},
                    {"Sec-Fetch-Site": "cross-site"}):
        resp = app_client.post("/delete",
                               data={"day": "2026-10-20",
                                     "bedtime": "23:15"},
                               headers=headers)
        assert resp.status_code == 403, headers
    # Все попытки удаления отклонены — запись осталась на месте.
    assert ("2026-10-20", "23:15") in _rows(empty_db)


def test_get_routes_not_guarded(app_client):
    """GET-роуты (/ и /healthz) работают без проверок guard'а."""
    assert app_client.get("/").status_code == 200
    assert app_client.get("/healthz").status_code == 200
