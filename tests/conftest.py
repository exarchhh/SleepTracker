# -*- coding: utf-8 -*-
"""Общие фикстуры для регрессионных тестов SleepTracker."""

import os
import shutil
import sqlite3
import sys

import pytest

# Позволяет импортировать модули проекта из корня репозитория
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _make_db(path):
    """Создаёт пустую БД sleep.db с канонической структурой (как init_db)."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sleep_entries (
            day     TEXT NOT NULL,
            bedtime TEXT NOT NULL,
            UNIQUE (day, bedtime)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deleted_nights (
            evening_day TEXT PRIMARY KEY
        )
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def empty_db(tmp_path):
    """Временная папка с пустой БД sleep.db. Возвращает путь к БД.

    Папка удаляется после теста.
    """
    db_file = tmp_path / "sleep.db"
    _make_db(str(db_file))
    yield str(db_file)
    shutil.rmtree(str(tmp_path), ignore_errors=True)


@pytest.fixture
def app_client(empty_db, monkeypatch):
    """Flask test client, работающий с временной БД empty_db."""
    import app as app_module

    monkeypatch.setattr(app_module, "DB_PATH", empty_db)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


@pytest.fixture
def real_history():
    """Фрагмент реальной истории пользователя: вечер 25.07 + ночь 26.07 00:19."""
    return [
        {"day": "2026-07-25", "bedtime": "22:41"},
        {"day": "2026-07-26", "bedtime": "00:19"},
    ]
