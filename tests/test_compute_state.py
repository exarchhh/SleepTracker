# -*- coding: utf-8 -*-
"""Регрессионные тесты ядра расчёта штрафа compute_state()."""

from core import IDEAL_MINUTES, MAX_BURN_PER_DAY, compute_state


def _by_day(state, day):
    rows = [r for r in state if r["day"] == day]
    assert rows, "нет строки для %s" % day
    return rows[0]


def test_ideal_bedtime_no_fine():
    """Отбой ровно в 23:00 — ни роста, ни сгорания, штраф 0."""
    state = compute_state([{"day": "2026-10-20", "bedtime": "23:00"}])
    assert len(state) == 1
    row = state[0]
    assert row["delta"] == 0
    assert row["fine_before"] == 0
    assert row["fine_after"] == 0
    assert row["level"] == 0


def test_late_bedtime_adds_fine():
    """Отбой в 23:30 — штраф +30 минут."""
    state = compute_state([{"day": "2026-10-20", "bedtime": "23:30"}])
    row = state[0]
    assert row["delta"] == 30
    assert row["fine_after"] == 30


def test_early_bedtime_burns_fine():
    """Ранний отбой сжигает штраф: 100 -> 40 (22:00, delta -60)."""
    entries = [
        {"day": "2026-10-19", "bedtime": "00:40"},   # delta +100, fine = 100
        {"day": "2026-10-20", "bedtime": "22:00"},   # delta -60, burn 60
    ]
    state = compute_state(entries)
    evening = _by_day(state, "2026-10-20")
    assert evening["delta"] == -60
    assert evening["fine_before"] == 100
    assert evening["fine_after"] == 40


def test_burn_capped_at_120():
    """Сгорание ограничено 120 мин/вечер: 21:00 при штрафе 50 -> 0 (не минус)."""
    entries = [
        {"day": "2026-10-19", "bedtime": "23:50"},   # delta +50, fine = 50
        {"day": "2026-10-20", "bedtime": "21:00"},   # delta -120, но горит только 50
    ]
    state = compute_state(entries)
    evening = _by_day(state, "2026-10-20")
    assert evening["fine_before"] == 50
    assert evening["fine_after"] == 0
    assert evening["capped"] is True
    assert all(r["fine_after"] >= 0 for r in state)


def test_night_shift_belongs_to_previous_evening():
    """Ночная запись 15.10 01:19 — продолжение вечера 14.10 (как в реальной истории)."""
    entries = [
        {"day": "2026-10-14", "bedtime": "23:40"},   # вечер: delta +40
        {"day": "2026-10-15", "bedtime": "01:19"},   # ночь того же вечера
    ]
    state = compute_state(entries)
    night = _by_day(state, "2026-10-15")
    assert night["level"] == 1                       # ночной ярус
    assert night["delta"] == 139                     # 25:19 - 23:00
    assert night["fine_before"] == 40                # штраф вечера 14.10
    assert night["fine_after"] == 179                # 40 + 139


def test_night_shift_on_real_history():
    """Тот же механизм на реальных данных пользователя: 26.07 00:19.

    В исходном Excel у этой ночи дельта 53 — автор считал отбой относительно
    ФАКТИЧЕСКОГО времени засыпания предыдущего вечера (22:41), а не эталонных
    23:00. Наше ядро считает от идеала: 00:19 -> 24:19, delta = 79.
    """
    entries = [
        {"day": "2026-07-25", "bedtime": "22:41"},   # delta -19
        {"day": "2026-07-26", "bedtime": "00:19"},   # ночь того же вечера
    ]
    state = compute_state(entries)
    night = _by_day(state, "2026-07-26")
    assert night["level"] == 1
    assert night["delta"] == 79                     # 24:19 - 23:00
    # Обе записи одного вечера: вечерняя идёт первой, ночная продолжением
    evening = _by_day(state, "2026-07-25")
    assert evening["level"] == 0
    assert evening["delta"] == -19
    assert night["fine_before"] == evening["fine_after"]


def test_fine_never_goes_negative():
    """Серия очень ранних отбоев не уводит штраф в минус."""
    entries = [
        {"day": "2026-10-01", "bedtime": "23:30"},   # +30
        {"day": "2026-10-02", "bedtime": "18:00"},   # -300 (cap 120) -> 0
        {"day": "2026-10-03", "bedtime": "18:00"},   # -300 -> остаётся 0
        {"day": "2026-10-04", "bedtime": "20:00"},   # -180 -> 0
    ]
    state = compute_state(entries)
    assert all(r["fine_after"] >= 0 for r in state)
    assert state[-1]["fine_after"] == 0


def test_chronological_order_with_backdated_entry():
    """Запись, добавленная задним числом, встаёт на своё место в хронологии."""
    backdated = {"day": "2026-10-01", "bedtime": "23:30"}     # delta +30
    later = {"day": "2026-10-02", "bedtime": "23:10"}         # delta +10
    # Вводим старые данные ПОСЛЕ новых — порядок на входе не важен
    state = compute_state([later, backdated])
    assert [r["day"] for r in state] == ["2026-10-01", "2026-10-02"]
    assert state[0]["fine_after"] == 30
    assert state[1]["fine_before"] == 30
    assert state[1]["fine_after"] == 40


def test_duplicates_are_merged():
    """Дубликаты (одинаковые day+bedtime) сливаются в одну запись."""
    e = {"day": "2026-10-10", "bedtime": "23:15"}
    state = compute_state([e, dict(e), dict(e)])
    assert len(state) == 1
    assert state[0]["fine_after"] == 15


def test_two_entries_per_evening_coexist():
    """За один вечер допустимы две записи: вечерняя (день D) и ночная (день D+1)."""
    entries = [
        {"day": "2026-10-20", "bedtime": "23:20"},   # вечер +20
        {"day": "2026-10-21", "bedtime": "00:30"},   # ночь того же вечера +90
    ]
    state = compute_state(entries)
    assert len(state) == 2
    assert state[0]["level"] == 0 and state[0]["fine_after"] == 20
    assert state[1]["level"] == 1 and state[1]["fine_after"] == 110


def test_constants_match_spec():
    """Константы ядра соответствуют архитектурному описанию."""
    assert IDEAL_MINUTES == 1380          # 23:00
    assert MAX_BURN_PER_DAY == 120        # максимум сгорания за вечер
