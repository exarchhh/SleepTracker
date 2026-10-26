# -*- coding: utf-8 -*-
"""Регрессионные тесты расчёта якорей (evening_anchor / level_of / night_key)."""

from app import effective_evening, evening_anchor, level_of, night_key


def test_evening_anchor_day():
    """Якорь для 14.10 23:30 = день от эпохи * 1440 + минуты суток."""
    a = evening_anchor("2026-10-14", "23:30")
    assert a % 1440 == 23 * 60 + 30           # минуты суток сохранены
    # Якоря разных дат различаются ровно на суточный сдвиг
    b = evening_anchor("2026-10-15", "23:30")
    assert b - a == 1440


def test_night_anchor_same_evening():
    """Якорь 15.10 01:19 лежит в интервале того же вечера, что и 14.10 23:30."""
    a = evening_anchor("2026-10-14", "23:30")
    n = evening_anchor("2026-10-15", "01:19")
    assert n > a                              # хронологически позже
    assert n - a < 1440                       # но в пределах одних суток+
    assert night_key(n) == night_key(a)       # тот же «вечер»


def test_level_of_evening_is_0():
    """Вечерний диапазон 12:00-23:59 — ярус 0."""
    for bed in ("12:00", "18:30", "23:00", "23:59"):
        assert level_of(evening_anchor("2026-10-14", bed)) == 0, bed


def test_level_of_night_is_1():
    """Ночной диапазон 00:00-11:59 — ярус 1."""
    for bed in ("00:00", "01:19", "07:45", "11:59"):
        assert level_of(evening_anchor("2026-10-15", bed)) == 1, bed


def test_night_key_same_for_evening_and_night():
    """night_key одинаков для 14.10 23:30 и 15.10 01:19 (один вечер)."""
    a = evening_anchor("2026-10-14", "23:30")
    n = evening_anchor("2026-10-15", "01:19")
    assert night_key(a) == night_key(n)
    # А полуденная запись следующего дня — уже ДРУГОЙ вечер
    d = evening_anchor("2026-10-15", "12:00")
    assert night_key(d) != night_key(n)


def test_effective_evening_maps_night_to_previous_day():
    """Ночная запись 22.10 00:19 относится к вечеру 21.10."""
    eff_day, anchor, bed = effective_evening("2026-10-22", "00:19")
    assert eff_day == "2026-10-21"
    assert bed == "00:19"
    # Вечерняя запись остаётся привязанной к своей дате
    eff_day2, _, _ = effective_evening("2026-10-21", "23:40")
    assert eff_day2 == "2026-10-21"
