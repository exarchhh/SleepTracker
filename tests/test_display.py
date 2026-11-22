# -*- coding: utf-8 -*-
"""Тесты двойного формата штрафа и якоря окраски (Задача №9, v0.12.0).

fmt_fine — двойной формат «N мин · H ч [M мин]» (Приложение Б.1 паспорта);
compute_color_anchor — якорь единой шкалы max(floor, P95 nearest-rank)
(Приложение Б.2, DECISIONS 25) с негативными проверками: выброс не задаёт
якорь, пустое окно -> floor; fine_gain в build_chart_data — прирост/сгорание
штрафа за вечер; рендер payload color_anchor и data-fine-разметки.
"""

import math
import re
from datetime import date, timedelta

import app as app_module
from core import MAX_BURN_PER_DAY, compute_state

TODAY = date(2026, 11, 22)   # явная today для якоря (даты не от часов сервера)
W = app_module.COLOR_WINDOW_DAYS


def _rec(days_ago, fine):
    """Запись state: вечер TODAY - days_ago (level 0) со штрафом fine."""
    return {"input_day": (TODAY - timedelta(days=days_ago)).isoformat(),
            "input_bedtime": "23:00", "level": 0, "fine_after": fine}


def test_fmt_fine_cases():
    """Двойной формат: все кейсы §2.8.1 паспорта."""
    cases = [(0, "0 мин"), (45, "45 мин"), (59, "59 мин"), (60, "60 мин · 1 ч"),
             (120, "120 мин · 2 ч"), (2676, "2676 мин · 44 ч 36 мин"),
             (3639, "3639 мин · 60 ч 39 мин")]
    for m, expected in cases:
        assert app_module.fmt_fine(m) == expected


def test_anchor_empty_state_returns_floor():
    """Пустой state -> floor."""
    assert app_module.compute_color_anchor([], today=TODAY) \
        == app_module.COLOR_FLOOR_MIN


def test_anchor_all_zero_fines_return_floor():
    """Все штрафы окна 0 -> floor (P95 = 0, max с floor)."""
    state = [_rec(i, 0) for i in range(10)]
    assert app_module.compute_color_anchor(state, today=TODAY) \
        == app_module.COLOR_FLOOR_MIN


def test_anchor_ignores_records_outside_window():
    """Записи старше окна исключаются; пустое окно -> floor (не рекорд)."""
    state = [_rec(W + 5, 5000), _rec(W + 1, 3000)]
    assert app_module.compute_color_anchor(state, today=TODAY) \
        == app_module.COLOR_FLOOR_MIN


def test_anchor_window_boundary_inclusive():
    """Граница окна: today-(W-1) включается, today-W — нет."""
    in_window = [_rec(W - 1, 400)]
    assert app_module.compute_color_anchor(in_window, today=TODAY) == 400
    out_of_window = [_rec(W, 400)]
    assert app_module.compute_color_anchor(out_of_window, today=TODAY) \
        == app_module.COLOR_FLOOR_MIN


def test_anchor_p95_nearest_rank_no_outlier():
    """P95 nearest-rank: 21 запись (0x11 + 60..600) -> якорь 540 (§2.8.2)."""
    state = [_rec(i, 0) for i in range(11)]
    for i, v in enumerate([60, 120, 180, 240, 300, 360, 420, 480, 540, 600]):
        state.append(_rec(20 + i, v))
    assert len(state) == 21
    expected = state[math.ceil(0.95 * 21) - 1]["fine_after"]
    assert expected == 540
    assert app_module.compute_color_anchor(state, today=TODAY) == 540


def test_anchor_outlier_record_ignored():
    """Негативная проверка якоря: единичный рекорд 3639 НЕ задаёт якорь."""
    state = [_rec(i, 0) for i in range(11)]
    for i, v in enumerate([60, 120, 180, 240, 300, 360, 420, 480, 540, 600]):
        state.append(_rec(20 + i, v))
    state.append(_rec(0, 3639))              # выброс в окне
    assert app_module.compute_color_anchor(state, today=TODAY) == 600
    assert app_module.compute_color_anchor(state, today=TODAY) != 3639


def test_anchor_night_record_uses_effective_evening():
    """Ночная запись относится к предыдущему вечеру — как в build_chart_data.

    Запись 2026-11-23 01:30 (level 1) — вечер 2026-11-22 = TODAY: попадает
    в окно; по календарной дате input_day была бы «в будущем» и отпала.
    """
    state = [{"input_day": "2026-11-23", "input_bedtime": "01:30",
              "level": 1, "fine_after": 400}]
    assert app_module.compute_color_anchor(state, today=TODAY) == 400


def test_fine_gain_late_evening():
    """Вечер с опозданием: fine_gain == delta > 0."""
    state = compute_state([{"day": "2026-10-20", "bedtime": "23:30"}])
    data = app_module.build_chart_data(state)
    assert data[0]["delta"] == 30
    assert data[0]["fine_gain"] == data[0]["delta"] > 0


def test_fine_gain_burn_capped_minus_max():
    """Ранний отбой при штрафе > 0 и |delta| > MAX_BURN_PER_DAY: gain = -max."""
    entries = [
        {"day": "2026-10-20", "bedtime": "01:30"},   # ночь вечера 10-19: +150
        {"day": "2026-10-20", "bedtime": "21:00"},   # delta -120, сгорает 120
    ]
    state = compute_state(entries)
    row = [r for r in state if r["day"] == "2026-10-20"
           and r["input_bedtime"] == "21:00"][0]
    assert row["fine_before"] == 150
    assert row["delta"] == -MAX_BURN_PER_DAY
    data = app_module.build_chart_data(state)
    gain = [d for d in data if d["full_date"] == "2026-10-20"
            and d["bedtime_min"] == 21 * 60][0]["fine_gain"]
    assert gain == -MAX_BURN_PER_DAY


def test_fine_gain_early_at_zero_fine():
    """Ранний отбой при штрафе 0: gain = 0 (сжигать нечего)."""
    state = compute_state([{"day": "2026-10-20", "bedtime": "22:00"}])
    data = app_module.build_chart_data(state)
    assert data[0]["delta"] < 0
    assert data[0]["fine_gain"] == 0


def test_index_renders_color_anchor_and_data_fine(app_client, empty_db):
    """GET /: payload color_anchor, data-fine в истории; классов больше нет.

    До v0.12.0 цвет штрафа задавал сервер классами fine_class/fine_class_row;
    теперь сервер отдаёт якорь одним полем, красит JS. Мёртвого кода
    (gradientColors) в разметке быть не должно.
    """
    app_client.post("/save", data={"day": "2026-10-20", "bedtime": "23:30"})
    app_client.post("/save", data={"day": "2026-10-21", "bedtime": "01:30"})
    resp = app_client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    m = re.search(r"const COLOR_ANCHOR = (\d+);", html)
    assert m, "COLOR_ANCHOR отсутствует в payload"
    assert int(m.group(1)) >= app_module.COLOR_FLOOR_MIN
    assert 'data-fine="' in html, "data-fine нет в ячейках/карточке"
    assert "fine_class" not in html and "fine_class_row" not in html
    assert "gradientColors" not in html
    assert "SleepTracker v" + app_module.VERSION in html
