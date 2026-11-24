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
import sqlite3
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
    """Двойной формат: все кейсы §2.8.1 паспорта (units v2: «м» в паре ч/м)."""
    cases = [(0, "0 мин"), (45, "45 мин"), (59, "59 мин"), (60, "60 мин · 1 ч"),
             (120, "120 мин · 2 ч"), (2676, "2676 мин · 44 ч 36 м"),
             (3639, "3639 мин · 60 ч 39 м")]
    for m, expected in cases:
        assert app_module.fmt_fine(m) == expected


def test_fmt_fine_parts_cases():
    """fmt_fine_parts (§2.8.2): строка 1 «N мин», hm пуст при m < 60."""
    cases = [(0, ("0 мин", "")), (45, ("45 мин", "")), (60, ("60 мин", "1 ч")),
             (120, ("120 мин", "2 ч")), (2676, ("2676 мин", "44 ч 36 м"))]
    for m, expected in cases:
        parts = app_module.fmt_fine_parts(m)
        assert (parts["min"], parts["hm"]) == expected


def _seed_history(db_path, count=25):
    """Наполняет временную БД count вечерними записями (23:30, без cap)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO sleep_entries (day, bedtime) VALUES (?, ?)",
            [("2026-10-%02d" % d, "23:30") for d in range(1, count + 1)])
        conn.commit()
    finally:
        conn.close()


def test_index_show_count_default_and_invalid(app_client, empty_db):
    """Порция истории (§2.8.3, Б.2): отсутствие/мусор/0 -> HISTORY_STEP."""
    _seed_history(empty_db)
    for query in ("", "?show_count=abc", "?show_count=0"):
        html = app_client.get("/" + query).get_data(as_text=True)
        assert html.count('data-fine="') == app_module.HISTORY_STEP


def test_index_show_count_explicit_and_all(app_client, empty_db):
    """show_count=20 -> 20 строк; show_all=1 -> все 25, ссылка «еще» скрыта."""
    _seed_history(empty_db)
    html = app_client.get("/?show_count=20").get_data(as_text=True)
    assert html.count('data-fine="') == 20
    html = app_client.get("/?show_all=1").get_data(as_text=True)
    assert html.count('data-fine="') == 25
    assert "?show_count=" not in html


def test_index_renders_pagination_links_and_fine_parts(app_client, empty_db):
    """GET / (§2.9.1): обе кнопки в .more-bar, data-fine-min/-hm, pointsToggle."""
    _seed_history(empty_db)
    html = app_client.get("/").get_data(as_text=True)
    assert 'href="?show_count=20"' in html      # 10 + HISTORY_STEP
    assert "Показать еще 10" in html
    assert 'href="?show_all=1"' in html
    assert 'class="more-bar"' in html           # разметка Б.1 (кнопки в баре)
    assert " · <a" not in html                  # разделителя « · » больше нет
    assert 'data-fine-min="' in html            # карточка «Текущий штраф», стр. 1
    assert 'data-fine-hm="' in html             # строка 2 (приглушённая)
    assert 'id="pointsToggle"' in html          # переключатель «Точки»
    # data-fine в ячейках истории сохранён (полный формат одной строкой)
    assert html.count('data-fine="') == app_module.HISTORY_STEP


def test_index_renders_cap_info_only_for_capped(app_client, empty_db):
    """ⓘ у «(cap)» (§2.9.2): только у capped-записи, без нативного title.

    22:00 при штрафе 150 (delta -60 < MAX, сгорает часть, не до нуля) —
    uncapped; 20:00 при штрафе 90 (delta -180 > MAX) — capped.
    """
    app_client.post("/save", data={"day": "2026-10-20", "bedtime": "01:30"})
    app_client.post("/save", data={"day": "2026-10-21", "bedtime": "22:00"})
    app_client.post("/save", data={"day": "2026-10-22", "bedtime": "20:00"})
    html = app_client.get("/").get_data(as_text=True)
    assert html.count('class="cap-info"') == 1, "cap-info не у одной записи"
    assert "(cap)" in html
    assert "не больше %d минут" % MAX_BURN_PER_DAY in html
    assert 'title="Сгорание' not in html        # дублирующий тултип убран


def test_index_renders_night_badge_with_tooltip(app_client, empty_db):
    """Тег «ночь» (§2.9.3): data-tip только у level-1, у level-0 badge нет.

    Записи вставляются напрямую в БД: 01:30 (ночь вечера 10-19) и 23:30
    (вечер 10-19) — один вечер, через /save правило «один отбой на вечер»
    заменило бы одну записи другой.
    """
    conn = sqlite3.connect(empty_db)
    try:
        conn.executemany(
            "INSERT INTO sleep_entries (day, bedtime) VALUES (?, ?)",
            [("2026-10-20", "01:30"),   # ночь вечера 10-19 (level 1)
             ("2026-10-19", "23:30")])  # вечер 10-19 (level 0)
        conn.commit()
    finally:
        conn.close()
    html = app_client.get("/").get_data(as_text=True)
    assert 'class="badge-night"' in html
    assert 'data-tip="Отбой после полуночи' in html
    assert html.count('class="badge-night"') == 1, "badge не у одной записи"


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
    """Граница лимита (§2.8.5): |delta| == MAX_BURN_PER_DAY -> gain = -max."""
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


def test_fine_gain_burn_exceeds_cap():
    """Превышение лимита (§2.8.5, ревью №9): |delta| > MAX -> сгорает max.

    Отбой 20:00 (delta -180) при штрафе 150: сгорает ровно
    MAX_BURN_PER_DAY (120), не вся дельта; fine_gain == -MAX_BURN_PER_DAY.
    """
    entries = [
        {"day": "2026-10-20", "bedtime": "01:30"},   # ночь вечера 10-19: +150
        {"day": "2026-10-20", "bedtime": "20:00"},   # delta -180, сгорает 120
    ]
    state = compute_state(entries)
    row = [r for r in state if r["day"] == "2026-10-20"
           and r["input_bedtime"] == "20:00"][0]
    assert row["delta"] == -180
    assert row["capped"] is True
    data = app_module.build_chart_data(state)
    gain = [d for d in data if d["full_date"] == "2026-10-20"
            and d["bedtime_min"] == 20 * 60][0]["fine_gain"]
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
