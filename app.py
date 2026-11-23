# -*- coding: utf-8 -*-
"""SleepTracker — локальное веб-приложение для учёта времени отбоя и штрафа.

Основная логика расчёта штрафа вынесена в модуль `core` (см. core.compute_state).
"""

import math
import os
import shutil
import sqlite3
import sys
from datetime import date as _date, datetime, timedelta
from urllib.parse import urlsplit

from flask import (Flask, abort, flash, redirect, render_template,
                   request)

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

VERSION = "0.13.0"          # MAJ.MIN.PATCH — единственная точка истины; синхронизация проверяется tests/test_version_sync.py

COLOR_WINDOW_DAYS = 90   # окно якоря окраски, дней (DECISIONS 25; кандидат в настройки — BACKLOG, Этап 2)
COLOR_FLOOR_MIN = 120    # floor якоря окраски, минут (DECISIONS 25; кандидат в настройки)
HISTORY_STEP = 10   # порция подгрузки истории (кандидат в настройки — BACKLOG, Этап 2)

app = Flask(__name__)
app.secret_key = os.urandom(32)  # нужен для flash-сообщений об ошибках ввода


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

def _effective_evening_date(r):
    """Эффективная дата вечера записи state (datetime.date).

    Ночная запись (level 1) относится к предыдущему вечеру через
    core.effective_evening — общий способ build_chart_data (дисплейная
    дата) и compute_color_anchor (окно якоря окраски). Поведение не
    меняется.
    """
    if r["level"] == 1:
        disp_day, _, _ = core.effective_evening(
            r["input_day"], r["input_bedtime"])
    else:
        disp_day = r["input_day"]
    return datetime.strptime(disp_day, "%Y-%m-%d").date()


def build_chart_data(state):
    """Список [{'date': 'DD.MM', 'full_date': ISO, 'fine': N,
    'bedtime_min': M, 'delta': D, 'fine_gain': G}, ...] для фронтенда
    (Chart.js).

    Каждая запись state -> точка на графике: эффективная дата вечера
    (ночные сдвиги относятся к предыдущему вечеру) и штраф после вечера.
    bedtime_min — минуты отбоя относительно полудня предыдущего дня
    (послеполуночный отбой даёт >720), нужен для динамического среднего.
    delta — отклонение отбоя от идеала в минутах (>0 опоздание, <0 ранний
    отбой), берётся из уже посчитанного state. fine_gain — прирост/сгорание
    штрафа за вечер (fine_after − fine_before), нужен для тултипа. Логика
    расчёта штрафа НЕ затрагивается — только обогащение данных для
    фронтенда (тултипы).
    """
    chart_data = []
    for idx, r in enumerate(state):
        d = _effective_evening_date(r)
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
                           "fine_gain": r["fine_after"] - r["fine_before"],
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
# Двойной формат штрафа и якорь окраски (v0.12.0 — только чтение state)
# ---------------------------------------------------------------------------

def fmt_fine(m):
    """Двойной формат штрафа v2: «N мин» / «N мин · H ч [M м]» (Приложение Б.1)."""
    if m < 60:
        return "%d мин" % m
    h, mm = divmod(m, 60)
    if mm:
        return "%d мин · %d ч %d м" % (m, h, mm)
    return "%d мин · %d ч" % (m, h)


def fmt_fine_parts(m):
    """Карточки: строка 1 «N мин», строка 2 «H ч [M м]» (пусто при m < 60)."""
    if m < 60:
        return {"min": "%d мин" % m, "hm": ""}
    h, mm = divmod(m, 60)
    return {"min": "%d мин" % m,
            "hm": ("%d ч %d м" % (h, mm)) if mm else ("%d ч" % h)}


def compute_color_anchor(state, today=None):
    """Якорь единой шкалы окраски штрафа (Приложение Б.2; DECISIONS 25).

    max(COLOR_FLOOR_MIN, P95 штрафов за окно COLOR_WINDOW_DAYS дней от
    today) — вариант «О3»: устойчив к единичному рекорду (P95 выброс
    отбрасывает) и «оживает» после возврата к нулю (окно очищается —
    первый срыв виден сразу). Перцентиль — nearest-rank:
    sorted_fines[ceil(0.95 * n) - 1]. Дата записи — эффективная дата
    вечера (ночная запись относится к предыдущему вечеру), штраф записи —
    fine_after. Пустое окно или все штрафы окна 0 -> floor.
    """
    today = today or _date.today()
    window_start = today - timedelta(days=COLOR_WINDOW_DAYS - 1)
    fines = sorted(r["fine_after"] for r in state
                   if window_start <= _effective_evening_date(r) <= today)
    if not fines:
        return COLOR_FLOOR_MIN
    p95 = fines[math.ceil(0.95 * len(fines)) - 1]
    return max(COLOR_FLOOR_MIN, p95)


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
    avg %= 1440  # перенос через полночь: "25:30" -> "01:30", как в JS
    return "%02d:%02d" % divmod(avg, 60)


def _bedtime_to_minutes(bedtime):
    hh, mm = bedtime.split(":")
    return int(hh) * 60 + int(mm)




# ---------------------------------------------------------------------------
# Защита POST-роутов (CSRF / DNS-rebinding)
# ---------------------------------------------------------------------------

def _post_guard():
    """Guard POST-роутов /save и /delete: Host / Origin / Sec-Fetch-Site.

    Сервер слушает только 127.0.0.1: посторонний Host (DNS-rebinding),
    Origin чужого сайта (CSRF) и cross-site-запросы отклоняются с 403.
    Отсутствие Origin/Sec-Fetch-Site (curl, тест-клиент) разрешено —
    в этом случае проверяется только Host.
    """
    allowed = ("localhost", "127.0.0.1")
    host = urlsplit("//" + request.host).hostname
    if host is None or host.lower() not in allowed:
        abort(403)
    origin = request.headers.get("Origin")
    if origin:
        parts = urlsplit(origin)
        if (parts.scheme != "http" or parts.hostname is None
                or parts.hostname.lower() not in allowed):
            abort(403)
    sec_fetch = request.headers.get("Sec-Fetch-Site")
    if sec_fetch and sec_fetch.lower() not in ("same-origin", "none"):
        abort(403)


# ---------------------------------------------------------------------------
# Роуты
# ---------------------------------------------------------------------------

def _show_count(raw):
    """Порция истории: int >= 1; мусор/отсутствие -> HISTORY_STEP (Б.2)."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return HISTORY_STEP
    return n if n >= 1 else HISTORY_STEP


@app.route("/")
def index():
    show_all = request.args.get("show_all", "0") == "1"
    show_count = _show_count(request.args.get("show_count"))
    conn = get_db()
    try:
        db_rows = conn.execute(
            "SELECT day, bedtime FROM sleep_entries").fetchall()
        # Память об удалённых ночах (ghost-блокировка, v0.10.0): вечер
        # хранится как ключ эпохи (night_key) — превращаем его обратно
        # в дату вечера для блока «Разрешить повторный ввод».
        ghost_rows = conn.execute(
            "SELECT evening_day FROM deleted_nights ORDER BY evening_day"
        ).fetchall()
    finally:
        conn.close()

    entries = [{"day": r["day"], "bedtime": r["bedtime"]} for r in db_rows]
    state = core.compute_state(entries)
    total_count = len(state)
    # История показывается от последней даты к более ранним; show_all=1
    # перекрывает порцию show_count (рендерит всё, ссылка «еще» скрыта).
    view = (state if show_all else state[-show_count:])[::-1]

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
        rows.append({**r, "day_human": day_human, "delta_text": delta_text,
                     "delta_class": delta_class})

    if state:
        current_fine = state[-1]["fine_after"]
        last = state[-1]
        last_bedtime = "%s %s" % (
            datetime.strptime(last["input_day"], "%Y-%m-%d").strftime("%d.%m"),
            last["input_bedtime"])
    else:
        current_fine, last_bedtime = 0, None
    current_hm = fmt_fine_parts(current_fine)["hm"]

    chart_data = build_chart_data(state)

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

    # Заблокированные вечера для UI-разблокировки: iso — дата вечера
    # «YYYY-MM-DD» (параметр формы /unlock_night), human — «DD.MM.YYYY»
    # тем же способом, каким выше форматируется day_human.
    ghosted_nights = []
    for r in ghost_rows:
        g_date = _date.fromordinal(
            int(r["evening_day"]) + _date(1970, 1, 1).toordinal())
        ghosted_nights.append({
            "iso": g_date.isoformat(),
            "human": g_date.strftime("%d.%m.%Y"),
        })

    html = render_template(
        'index.html',
        rows=rows,
        chart_data=chart_data,
        current_fine=current_fine,
        current_hm=current_hm,
        color_anchor=compute_color_anchor(state),
        last_bedtime=last_bedtime,
        total_count=total_count,
        current_streak=current_streak,
        streak_word=streak_word,
        avg_bedtime=avg_bedtime,
        show_all=show_all,
        show_count=show_count,
        history_step=HISTORY_STEP,
        burn_limit=core.MAX_BURN_PER_DAY,
        today=_date.today().isoformat(),
        version=VERSION,
        ghosted_nights=ghosted_nights,
    )
    return html


@app.route("/save", methods=["POST"])
def save():
    _post_guard()
    day = (request.form.get("day") or "").strip()
    bedtime = (request.form.get("bedtime") or "").strip()
    old_day = (request.form.get("old_day") or "").strip()
    old_bedtime = (request.form.get("old_bedtime") or "").strip()
    try:
        core.evening_anchor(day, bedtime)  # валидация формата
    except ValueError:
        flash("Некорректная дата или время — запись не сохранена.")
        return redirect("/")

    # Правило 1 «правка не меняет вечер» (R1-plus, v0.11.0): инлайн-правка
    # при НЕизменённой дате и смене яруса роллит дату, чтобы запись осталась
    # в СВОЁМ вечере: 0->1 (вечер->ночь) — +1 день (правка 13.11 23:45 на
    # 01:30 даёт запись 14.11 01:30 — ночной слот того же вечера 13.11),
    # 1->0 (ночь->вечер) — -1 день. Явная смена даты (day != old_day)
    # сохраняет перенос между вечерами без ролла.
    if old_day and old_bedtime and day == old_day:
        try:
            old_anchor = core.evening_anchor(old_day, old_bedtime)
        except ValueError:
            old_anchor = None
        if old_anchor is not None:
            probe = core.evening_anchor(day, bedtime)
            old_lvl = core.level_of(old_anchor)
            probe_lvl = core.level_of(probe)
            if old_lvl == 0 and probe_lvl == 1:
                day = (datetime.strptime(old_day, "%Y-%m-%d").date()
                       + timedelta(days=1)).isoformat()
            elif old_lvl == 1 and probe_lvl == 0:
                day = (datetime.strptime(old_day, "%Y-%m-%d").date()
                       - timedelta(days=1)).isoformat()

    # Записи «из будущего» отклоняются по ИТОГОВОЙ дате: опечатка в дате не
    # должна портить статистику (сегодняшний день разрешён; ролл правки
    # вечерней записи сегодняшнего вечера на ночное время до смены
    # календарной даты даёт «завтра» и тоже отклоняется).
    if day > _date.today().isoformat():
        flash("Нельзя добавлять записи с датой в будущем")
        return redirect("/")

    conn = get_db()
    try:
        new_anchor = core.evening_anchor(day, bedtime)
        new_lvl = core.level_of(new_anchor)
        evening_key = core.night_key(new_anchor)  # вечер итоговой записи

        def _ghost(nk_val):
            return conn.execute(
                "SELECT 1 FROM deleted_nights WHERE evening_day=?",
                (str(nk_val),)).fetchone() is not None

        # Память об удалённых ночах: если вечер итоговой НОЧНОЙ записи есть
        # в deleted_nights, сохранение блокируется ДО любых мутаций —
        # «призрак» удалённой ночи не воскресает, заблокированное сохранение
        # не меняет БД (в т.ч. ролл-правка на ghost-вечер). Вечерние записи
        # не ограничиваются.
        if new_lvl == 1 and _ghost(evening_key):
            conn.commit()
            flash("Эта ночная запись была удалена ранее — сохранение отклонено")
            return redirect("/")

        if old_day and old_bedtime:
            # Инлайн-правка: старая запись удаляется по исходным полям
            # (old_day/old_bedtime не роллятся). Правка больше не опустошает
            # старый вечер и ничего не помечает в deleted_nights.
            conn.execute(
                "DELETE FROM sleep_entries WHERE day=? AND bedtime=?",
                (old_day, old_bedtime))

        # Правило 2 — инвариант «один отбой на вечер» (R1-plus, v0.11.0):
        # в каждом вечере ровно одна запись, новая заменяет прежнюю.
        # Очищаются ОБА слота целевого вечера E = evening_key: вечерний
        # (дата E с 12:00) и ночной (дата E+1 до 12:00); прежний перебор
        # конфликтов покрыт этой очисткой. Очистка НЕ ставит пометок в
        # deleted_nights: замена — не удаление.
        e_date = _date.fromordinal(
            evening_key + _date(1970, 1, 1).toordinal())
        e_plus = (e_date + timedelta(days=1)).isoformat()
        conn.execute(
            "DELETE FROM sleep_entries WHERE day=? AND bedtime>='12:00'",
            (e_date.isoformat(),))
        conn.execute(
            "DELETE FROM sleep_entries WHERE day=? AND bedtime<'12:00'",
            (e_plus,))

        conn.execute(
            "INSERT OR REPLACE INTO sleep_entries (day, bedtime) VALUES (?, ?)",
            (day, bedtime))
        # Занятие ночного слота этого вечера «отменяет» память об удалении:
        # слот снова занят легальной записью.
        if new_lvl == 1:
            conn.execute(
                "DELETE FROM deleted_nights WHERE evening_day=?",
                (str(evening_key),))
        conn.commit()
    finally:
        conn.close()
    return redirect("/")


@app.route("/delete", methods=["POST"])
def delete():
    _post_guard()
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


@app.route("/unlock_night", methods=["POST"])
def unlock_night():
    """POST /unlock_night — явное снятие ghost-памяти вечера (v0.10.0).

    Блокировка сохраняется по умолчанию; пользователь одной осознанной
    кнопкой (с confirm-диалогом) разрешает повторный ввод ночной записи.
    Параметр evening_day — строка даты вечера «YYYY-MM-DD»; снимается
    память именно этого вечера (все три вектора блокировки в /save
    лечатся одним DELETE). Идемпотентен: неизвестный вечер не меняет БД.
    """
    _post_guard()
    evening_day = (request.form.get("evening_day") or "").strip()
    try:
        # 12:00 — начало вечера D: night_key такого anchor = ключ вечера D,
        # совпадающий с ключом ночной записи 00:00-11:59 дня D+1.
        anchor = core.evening_anchor(evening_day, "12:00")
    except ValueError:
        anchor = None
    if anchor is not None:
        conn = get_db()
        try:
            conn.execute(
                "DELETE FROM deleted_nights WHERE evening_day=?",
                (str(core.night_key(anchor)),))
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
