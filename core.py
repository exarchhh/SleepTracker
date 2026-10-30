# -*- coding: utf-8 -*-
"""SleepTracker — ядро бизнес-логики (расчёт штрафа).

Модуль содержит стабильное ядро, вынесенное из app.py без изменений
логики: evening_anchor, level_of, night_key, effective_evening,
compute_state и связанные константы.

Логика штрафа:
  * Идеальное время отбоя: 23:00 (IDEAL_MINUTES = 1380 минут от полуночи).
  * Отбой позже 23:00 -> штраф растёт на число минут опоздания.
  * Отбой раньше 23:00 -> штраф сгорает, но не более MAX_BURN_PER_DAY минут
    за вечер.
  * Ночные сдвиги: отбой в интервале 00:00-11:59 считается продолжением
    ПРЕДЫДУЩЕГО вечера. За один вечер допустимы две записи: вечерняя
    (12:00-23:59) и ночная (00:00-11:59). Они не перезаписывают друг друга,
    потому что уникальный ключ записи — (day, bedtime).
"""

from datetime import date as _date, datetime

IDEAL_MINUTES = 1380        # 23:00 — идеальное время отбоя
MAX_BURN_PER_DAY = 120      # максимум минут «сгорания» штрафа за один вечер
DAY_MAP = {"Mon": "Пн", "Tue": "Вт", "Wed": "Ср", "Thu": "Чт",
           "Fri": "Пт", "Sat": "Сб", "Sun": "Вс"}


# ---------------------------------------------------------------------------
# Ядро логики штрафа (НЕ МЕНЯТЬ!)
# ---------------------------------------------------------------------------

def _parse(day_str, bedtime_str):
    d = datetime.strptime(day_str, "%Y-%m-%d").date()
    t = datetime.strptime(bedtime_str, "%H:%M").time()
    return d, t


def evening_anchor(day, bedtime):
    """Абсолютный календарный момент отбоя в минутах от эпохи.

    Ночной отбой (00:00-11:59) относится к дню `day`; вечерний (12:00-23:59) —
    тоже к дню `day`. То есть anchor — это просто день + время в минутах
    от начала суток эпохи.
    """
    d, t = _parse(day, bedtime)
    minutes_of_day = t.hour * 60 + t.minute
    days_epoch = d.toordinal() - _date(1970, 1, 1).toordinal()
    return days_epoch * 1440 + minutes_of_day


def level_of(anchor):
    """Ярус записи: 0 = вечерний (12:00-23:59), 1 = ночной (00:00-11:59)."""
    minutes_of_day = anchor % 1440
    return 0 if minutes_of_day >= 720 else 1


def night_key(anchor):
    """Ключ «вечера»: все отбои с 12:00 дня D до 11:59 дня D+1."""
    day_epoch = anchor // 1440
    minutes_of_day = anchor % 1440
    if minutes_of_day < 720:
        # ночь принадлежит предыдущему вечеру
        return day_epoch - 1
    return day_epoch


def effective_evening(day, bedtime):
    """Возвращает (effective_day, anchor, eff_bedtime).

    effective_day — дата «вечера», к которому относится запись;
    eff_bedtime — то же время отбоя (HH:MM).
    """
    anchor = evening_anchor(day, bedtime)
    key = night_key(anchor)
    eff_date = _date.fromordinal(key + _date(1970, 1, 1).toordinal())
    return eff_date.isoformat(), anchor, bedtime


def compute_state(entries):
    """Главная функция расчёта штрафа.

    Принимает список [{'day': 'YYYY-MM-DD', 'bedtime': 'HH:MM'}].
    Возвращает список словарей с полями:
        day, input_day, input_bedtime, shown_bedtime, level,
        delta, fine_before, fine_after, capped
    Сортирует по хронологии, сливает дубли, рассчитывает штраф.
    """
    parsed = []
    for e in entries:
        day, bedtime = e["day"], e["bedtime"]
        anchor = evening_anchor(day, bedtime)
        parsed.append((anchor, day, bedtime))

    # Нормализация записей внутри одного календарного дня:
    #  * В одну дату отход ко сну может быть только один — вечерние записи
    #    (12:00-23:59) одного и того же дня не сосуществуют: оставляем
    #    последнюю по времени отбоя.
    #  * За один вечер допустимы ДВЕ записи: вечерняя (день D, 12:00-23:59)
    #    и ночная (день D+1, 00:00-11:59). Они относятся к разным датам в БД
    #    (разные сущности) и не перезаписывают друг друга.
    #  * Ночных записей одного вечера может быть несколько (например, правка
    #    «15.10 23:40 -> 15.10 01:19» при уже существующей соседней ночи);
    #    в расчёте участвует самая поздняя — она и есть фактический отбой.
    evenings = {}          # night_key -> {"lvl0": rec | None, "lvl1": [recs]}
    order = {}             # night_key -> порядок первого появления
    for idx, (anchor, day, bedtime) in enumerate(sorted(parsed,
                                                       key=lambda x: (x[0], x[1], x[2]))):
        key = night_key(anchor)
        bucket = evenings.setdefault(key, {"lvl0": None, "lvl1": []})
        order.setdefault(key, len(order))
        if level_of(anchor) == 0:
            bucket["lvl0"] = (anchor, day, bedtime)   # последняя побеждает
        else:
            bucket["lvl1"].append((anchor, day, bedtime))

    def _sorted_keys(keys):
        return sorted(keys, key=lambda k: (k, order[k]))

    result = []
    fine = 0
    last_evening = None
    burn_today = 0

    for key in _sorted_keys(evenings.keys()):
        bucket = evenings[key]
        records = []
        if bucket["lvl0"] is not None:
            records.append(bucket["lvl0"])
        if bucket["lvl1"]:
            records.append(bucket["lvl1"][-1])       # самая поздняя ночь

        for anchor, day, bedtime in sorted(records, key=lambda x: x[0]):
            lvl = level_of(anchor)
            minutes_of_day = anchor % 1440

            if key != last_evening:
                last_evening = key
                burn_today = 0

            # Для ночной записи (00:00-11:59) время считается продолжением
            # предыдущего вечера: минуты от полуночи + 1440.
            effective_minutes = minutes_of_day + (1440 if lvl == 1 else 0)
            delta = effective_minutes - IDEAL_MINUTES
            fine_before = fine

            capped = False
            if delta > 0:
                fine += delta
            elif delta < 0:
                burn = min(-delta, MAX_BURN_PER_DAY - burn_today, fine)
                burn = max(burn, 0)
                if -delta > MAX_BURN_PER_DAY or (-delta > burn and fine_before > 0):
                    capped = True
                burn_today += burn
                fine -= burn

            result.append({
                "day": day,
                "input_day": day,
                "input_bedtime": bedtime,
                "shown_bedtime": bedtime,
                "level": lvl,
                "delta": delta,
                "fine_before": fine_before,
                "fine_after": fine,
                "capped": capped,
            })

    return result
