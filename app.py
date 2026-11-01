# -*- coding: utf-8 -*-
"""SleepTracker — локальное веб-приложение для учёта времени отбоя и штрафа.

Основная логика расчёта штрафа вынесена в модуль `core` (см. core.compute_state).
"""

import json
import os
import shutil
import sqlite3
import sys
from datetime import date as _date, datetime, timedelta

from flask import Flask, redirect, render_template, request

import core


def _data_dir():
    """Каталог для пользовательских данных (sleep.db и т.п.).

    Обычный запуск (python app.py / run.bat): рядом со скриптом.
    PyInstaller-сборка (.exe): рядом с .exe — данные пользователя должны
    переживать перезапуск и обновление программы.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


DB_PATH = os.environ.get(
    "SLEEPTRACKER_DB",
    os.path.join(_data_dir(), "sleep.db"),
)

# Совместимость со старыми сборками (--onefile): раньше база создавалась в
# каталоге запуска. Если рядом с exe её нет, а в текущей папке она есть —
# переносим её к exe, чтобы история не выглядела пустой.
if getattr(sys, "frozen", False):
    _CWD_DB = os.path.join(os.getcwd(), "sleep.db")
    if (os.path.isfile(_CWD_DB)
            and os.path.abspath(_CWD_DB) != os.path.abspath(DB_PATH)
            and not os.path.exists(DB_PATH)):
        try:
            shutil.copyfile(_CWD_DB, DB_PATH)
        except OSError:
            pass

VERSION = "0.7.0"           # MAJ.MIN.PATCH — синхронно с README.md

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Работа с базой данных
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    try:
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
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Данные для графика (Chart.js)
# ---------------------------------------------------------------------------

def build_chart_data(state):
    """Список [{'date': 'DD.MM', 'full_date': ISO, 'fine': N,
    'bedtime_min': M, 'delta': D}, ...] для фронтенда (Chart.js).

    Каждая запись state -> точка на графике: эффективная дата вечера
    (ночные сдвиги относятся к предыдущему вечеру) и штраф после вечера.
    bedtime_min — минуты отбоя относительно полудня предыдущего дня
    (послеполуночный отбой даёт >720), нужен для динамического среднего.
    delta — отклонение отбоя от идеала в минутах (>0 опоздание, <0 ранний
    отбой), берётся из уже посчитанного state; логика расчёта штрафа НЕ
    затрагивается — только обогащение данных для фронтенда (тултипы).
    """
    chart_data = []
    for idx, r in enumerate(state):
        if r["level"] == 1:
            disp_day, _, _ = core.effective_evening(
                r["input_day"], r["input_bedtime"])
        else:
            disp_day = r["input_day"]
        d = datetime.strptime(disp_day, "%Y-%m-%d")
        hh, mm = map(int, r["input_bedtime"].split(":"))
        # Отбой после полуночи — продолжение предыдущего вечера:
        # нормализуем к единой шкале вечера (23:00 -> 1380, 01:30 -> 1530),
        # как в compute_state. Среднее по таким минутам корректно для
        # ночных сдвигов; перенос через полночь обрабатывает JS.
        mins = hh * 60 + mm
        if mins < 720:  # 00:00-11:59
            mins += 1440
        bedtime_min = mins
        chart_data.append({"date": d.strftime("%d.%m"),
                           "full_date": d.strftime("%Y-%m-%d"),
                           "fine": r["fine_after"],
                           "bedtime_min": bedtime_min,
                           "delta": r.get("delta", 0),
                           "_order": idx})
    # Гарантируем сортировку по дате и хронологии внутри вечера
    # (вечерняя запись lvl0 раньше ночной lvl1). Chart.js рисует линию
    # только по отсортированным данным; state уже отсортирован, но
    # стабильная перестраховка не меняет значений.
    chart_data.sort(key=lambda p: (p["full_date"], p["_order"]))
    for p in chart_data:
        del p["_order"]
    return chart_data


# ---------------------------------------------------------------------------
# Расширенная статистика (только чтение state — логика штрафа не меняется)
# ---------------------------------------------------------------------------

def compute_streak(state):
    """Количество дней подряд без штрафа (delta <= 0), считая от конца."""
    streak = 0
    for r in reversed(state):
        if r["delta"] <= 0:
            streak += 1
        else:
            break
    return streak


def compute_avg_bedtime_period(state, days=None):
    """Среднее время отбоя за период ("HH:MM").

    days=None -> весь доступный диапазон записей (календарный год —
    базовое значение для динамической карточки; JS пересчитывает при
    переключении периода). Иначе — последние `days` календарных дней
    от самой поздней записи. Логика расчёта штрафа не затрагивается.
    """
    if not state:
        return None
    last_day = max(datetime.strptime(r["input_day"], "%Y-%m-%d")
                   for r in state)
    cutoff = (last_day - timedelta(days=days - 1)) if days else None
    minutes = []
    for r in state:
        d = datetime.strptime(r["input_day"], "%Y-%m-%d")
        if cutoff is None or d >= cutoff:
            h, m = divmod(_bedtime_to_minutes(r["input_bedtime"]), 60)
            # Отбой после полуночи показываем как продолжение предыдущего
            # вечера: 25:30 вместо 01:30 — иначе среднее исказится.
            if r["level"] == 1:
                h += 24
            minutes.append(h * 60 + m)
    if not minutes:
        return None
    avg = round(sum(minutes) / len(minutes))
    return "%02d:%02d" % divmod(avg, 60)


def _bedtime_to_minutes(bedtime):
    hh, mm = bedtime.split(":")
    return int(hh) * 60 + int(mm)




# ---------------------------------------------------------------------------
# Роуты
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    show_all = request.args.get("show_all", "0") == "1"
    conn = get_db()
    try:
        db_rows = conn.execute(
            "SELECT day, bedtime FROM sleep_entries").fetchall()
    finally:
        conn.close()

    entries = [{"day": r["day"], "bedtime": r["bedtime"]} for r in db_rows]
    state = core.compute_state(entries)
    total_count = len(state)
    # История показывается от последней даты к более ранним.
    view = (state if show_all else state[-10:])[::-1]

    rows = []
    for r in view:
        # Ночная запись (отбой 00:00-11:59) относится к вечеру ПРЕДЫДУЩЕГО
        # дня: в истории она показывается под своей эффективной датой вечера.
        if r["level"] == 1:
            disp_day, _, _ = core.effective_evening(r["input_day"], r["input_bedtime"])
        else:
            disp_day = r["input_day"]
        d = datetime.strptime(disp_day, "%Y-%m-%d")
        day_human = d.strftime("%d.%m.%Y") + " (" + core.DAY_MAP[d.strftime("%a")] + ")"
        delta = r["delta"]
        if delta > 0:
            delta_text, delta_class = "+%d" % delta, "delta-pos"
        elif delta < 0:
            delta_text = "%d%s" % (delta, " (cap)" if r["capped"] else "")
            delta_class = "delta-neg"
        else:
            delta_text, delta_class = "0", ""
        fine = r["fine_after"]
        fine_class_row = "green" if fine == 0 else ("yellow" if fine < 300 else "red")
        rows.append({**r, "day_human": day_human, "delta_text": delta_text,
                     "delta_class": delta_class, "fine_class_row": fine_class_row})

    if state:
        current_fine = state[-1]["fine_after"]
        last = state[-1]
        last_bedtime = "%s %s" % (
            datetime.strptime(last["input_day"], "%Y-%m-%d").strftime("%d.%m"),
            last["input_bedtime"])
    else:
        current_fine, last_bedtime = 0, None
    fine_class = "green" if current_fine == 0 else (
        "yellow" if current_fine < 300 else "red")

    chart_data = build_chart_data(state)
    chart_data_json = json.dumps(chart_data) if chart_data else ""

    current_streak = compute_streak(state)
    # Базовое значение карточки = активный период по умолчанию
    # (календарный год). JS пересчитывает его при переключении периода.
    avg_bedtime = compute_avg_bedtime_period(state)
    n = current_streak % 10
    if n == 1 and current_streak != 11:
        streak_word = "день"
    elif 2 <= n <= 4 and not (12 <= current_streak % 100 <= 14):
        streak_word = "дня"
    else:
        streak_word = "дней"

    html = render_template(
        'index.html',
        rows=rows,
        chart_data_json=chart_data_json,
        current_fine=current_fine,
        fine_class=fine_class,
        last_bedtime=last_bedtime,
        total_count=total_count,
        current_streak=current_streak,
        streak_word=streak_word,
        avg_bedtime=avg_bedtime,
        show_all=show_all,
        today=_date.today().isoformat(),
        version=VERSION,
    )
    return html


@app.route("/save", methods=["POST"])
def save():
    day = (request.form.get("day") or "").strip()
    bedtime = (request.form.get("bedtime") or "").strip()
    old_day = (request.form.get("old_day") or "").strip()
    old_bedtime = (request.form.get("old_bedtime") or "").strip()
    try:
        core.evening_anchor(day, bedtime)  # валидация формата
    except ValueError:
        return redirect("/")

    conn = get_db()
    try:
        # Одна дата — один отбой: не допускаем двух ВЕЧЕРНИХ записей
        # (12:00-23:59) одной календарной даты. Ночная запись этой же даты —
        # это продолжение ДРУГОГО (предыдущего) вечера, её не трогаем.
        new_anchor = core.evening_anchor(day, bedtime)
        new_lvl = core.level_of(new_anchor)

        def _ghost(nk_val):
            return conn.execute(
                "SELECT 1 FROM deleted_nights WHERE evening_day=?",
                (str(nk_val),)).fetchone() is not None

        # Память об удалённых ночах: если вечер новой НОЧНОЙ записи есть в
        # deleted_nights, сохранение блокируется — «призрак» удалённой ночи
        # не должен воскресать и перезаписывать правки соседнего вечера.
        # Для инлайн-правки проверяется также вечер СТАРОЙ записи: правка
        # «15.10 23:40 -> 15.10 01:19» на уже удалённый ночной слот того же
        # вечера блокируется так же, как обычное сохранение.
        # Вечерние записи при этом не ограничиваются.
        if new_lvl == 1 and _ghost(core.night_key(new_anchor)):
            conn.commit()
            return redirect("/")

        if old_day and old_bedtime:
            # Инлайн-правка существующей записи.
            try:
                old_anchor = core.evening_anchor(old_day, old_bedtime)
            except ValueError:
                old_anchor = None
            old_lvl = core.level_of(old_anchor) if old_anchor is not None else None

            # Правка МЕНЯЕТ ЯРУС (например «15.10 23:40 -> 15.10 01:19»).
            # Утверждённая логика (вариант A): это обычная правка времени,
            # перевалившего за полночь. Новая ночная запись относится к вечеру
            # ПРЕДЫДУЩЕГО дня (вечер 14.10) и является обычной правкой этой
            # ночной сущности: если на дате новой записи уже есть ночная
            # запись того же вечера — она заменяется (UPDATE), а не
            # блокируется. Память в deleted_nights при этом снимается,
            # поскольку слот снова занят легальной записью.
            if old_lvl == 0 and new_lvl == 1:
                conn.execute(
                    "DELETE FROM sleep_entries WHERE day=? AND bedtime<?",
                    (day, "12:00"))

            conn.execute(
                "DELETE FROM sleep_entries WHERE day=? AND bedtime=?",
                (old_day, old_bedtime))
            # Помечаем вечер старой записи в deleted_nights, если она была
            # единственной в своём вечере и её ярус меняется при правке
            # (например «15.10 23:40 -> 15.10 01:19»). Это защищает от
            # перезаписи ночной записи соседнего вечера.
            if old_anchor is not None:
                still_there = conn.execute(
                    "SELECT 1 FROM sleep_entries WHERE day=? LIMIT 1",
                    (old_day,)).fetchone()
                if old_lvl != new_lvl and not still_there:
                    conn.execute(
                        "INSERT OR REPLACE INTO deleted_nights (evening_day) "
                        "VALUES (?)",
                        (core.night_key(old_anchor),))

        clash = conn.execute(
            "SELECT bedtime FROM sleep_entries WHERE day=?", (day,)).fetchall()
        for row in clash:
            other = row["bedtime"]
            if (other == bedtime or
                    core.level_of(core.evening_anchor(day, other)) == new_lvl):
                conn.execute(
                    "DELETE FROM sleep_entries WHERE day=? AND bedtime=?",
                    (day, other))

        conn.execute(
            "INSERT OR REPLACE INTO sleep_entries (day, bedtime) VALUES (?, ?)",
            (day, bedtime))
        # Новая ночная запись этого вечера «отменяет» память об удалении:
        # слот снова занят легальной записью.
        if new_lvl == 1:
            conn.execute(
                "DELETE FROM deleted_nights WHERE evening_day=?",
                (str(core.night_key(core.evening_anchor(day, bedtime))),))
        conn.commit()
    finally:
        conn.close()
    return redirect("/")


@app.route("/delete", methods=["POST"])
def delete():
    day = (request.form.get("day") or "").strip()
    bedtime = (request.form.get("bedtime") or "").strip()
    conn = get_db()
    try:
        try:
            anchor = core.evening_anchor(day, bedtime)
            if core.level_of(anchor) == 1:
                conn.execute(
                    "INSERT OR REPLACE INTO deleted_nights (evening_day) "
                    "VALUES (?)",
                    (core.night_key(anchor),))
        except ValueError:
            pass
        conn.execute(
            "DELETE FROM sleep_entries WHERE day=? AND bedtime=?",
            (day, bedtime))
        conn.commit()
    finally:
        conn.close()
    return redirect("/")


@app.route("/healthz")
def healthz():
    try:
        conn = get_db()
        conn.execute("SELECT 1")
        conn.close()
        return "ok", 200
    except Exception as exc:  # pragma: no cover
        return "error: %s" % exc, 500


def _main():
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    _main()
