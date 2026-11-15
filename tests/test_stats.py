# -*- coding: utf-8 -*-
"""Тесты расширенной статистики app.py (compute_avg_bedtime_period).

Средний отбой в едином формате «ЧЧ:ММ» с переносом через полночь
(avg %= 1440) — Python теперь показывает то же, что и JS.
"""

import app as app_module


def test_avg_single_night_entry():
    """Ночная запись lvl1 00:19 -> среднее «00:19» (перенос через полночь).

    00:19 на шкале вечера: 19 + 24*60 = 1459 минут; до v0.8.0 Python
    выводил «24:19», теперь — «00:19», как JS.
    """
    state = [{"input_day": "2026-07-26", "input_bedtime": "00:19",
              "level": 1}]
    assert app_module.compute_avg_bedtime_period(state) == "00:19"


def test_avg_wraps_over_midnight():
    """Среднее >1440 минут -> «ЧЧ:ММ» следующего дня (< 24:00)."""
    state = [
        # 00:19 -> 1459 мин; 01:30 -> 1530 мин (25:30); среднее 1494.5 -> 1494
        {"input_day": "2026-07-26", "input_bedtime": "00:19", "level": 1},
        {"input_day": "2026-07-27", "input_bedtime": "01:30", "level": 1},
    ]
    result = app_module.compute_avg_bedtime_period(state)
    assert result == "00:54"
    assert result < "24:00"
