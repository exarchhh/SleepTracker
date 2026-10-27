# -*- coding: utf-8 -*-
"""SleepTracker — локальное веб-приложение для учёта времени отбоя и штрафа.

Логика штрафа:
  * Идеальное время отбоя: 23:00 (IDEAL_MINUTES = 1380 минут от полуночи).
  * Отбой позже 23:00 -> штраф растёт на число минут опоздания.
  * Отбой раньше 23:00 -> штраф сгорает, но не более MAX_BURN_PER_DAY минут за вечер.
  * Ночные сдвиги: отбой в интервале 00:00-11:59 считается продолжением
    ПРЕДЫДУЩЕГО вечера. За один вечер допустимы две записи: вечерняя
    (12:00-23:59) и ночная (00:00-11:59). Они не перезаписывают друг друга,
    потому что уникальный ключ записи — (day, bedtime).
"""

import json
import os
import shutil
import sqlite3
import sys
from datetime import date as _date, datetime, time as _time, timedelta

from flask import Flask, redirect, request


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

VERSION = "0.5.1"           # MAJ.MIN.PATCH — синхронно с README.md

IDEAL_MINUTES = 1380        # 23:00 — идеальное время отбоя
MAX_BURN_PER_DAY = 120      # максимум минут «сгорания» штрафа за один вечер
DAY_MAP = {"Mon": "Пн", "Tue": "Вт", "Wed": "Ср", "Thu": "Чт",
           "Fri": "Пт", "Sat": "Сб", "Sun": "Вс"}

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


# ---------------------------------------------------------------------------
# Данные для графика (Chart.js)
# ---------------------------------------------------------------------------

def build_chart_data(state):
    """Список [{'date': 'DD.MM', 'fine': N}, ...] для фронтенда (Chart.js).

    Каждая запись state -> точка на графике: эффективная дата вечера
    (ночные сдвиги относятся к предыдущему вечеру) и штраф после вечера.
    Логика расчёта штрафа не затрагивается — только подготовка данных.
    """
    chart_data = []
    for r in state:
        if r["level"] == 1:
            disp_day, _, _ = effective_evening(r["input_day"],
                                               r["input_bedtime"])
        else:
            disp_day = r["input_day"]
        d = datetime.strptime(disp_day, "%Y-%m-%d")
        chart_data.append({"date": d.strftime("%d.%m"),
                           "fine": r["fine_after"]})
    return chart_data


# ---------------------------------------------------------------------------
# HTML-шаблон
# ---------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Учёт сна</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
    background: #10151f; margin: 0; padding: 24px 16px; color: #e6eaf2;
  }
  .wrap { max-width: 860px; margin: 0 auto; }
  h1 { margin: 0 0 4px; font-size: 26px; }
  .subtitle { color: #8b94a7; margin: 0 0 20px; font-size: 14px; }
  .cards { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 20px; }
  .card {
    background: #1a2130; border-radius: 12px; padding: 14px 18px;
    box-shadow: 0 1px 3px rgba(0,0,0,.4); flex: 1 1 180px;
  }
  .card .label { font-size: 12px; color: #8b94a7; text-transform: uppercase;
                 letter-spacing: .04em; }
  .card .value { font-size: 22px; font-weight: 700; margin-top: 4px; }
  .green  { color: #3ecf7a; }
  .yellow { color: #e8c341; }
  .red    { color: #f06a6a; }
  .panel { background: #1a2130; border-radius: 12px; padding: 18px;
           box-shadow: 0 1px 3px rgba(0,0,0,.4); margin-bottom: 20px; }
  #sleepChart { width: 100% !important; }
  form.inline-form { display: flex; gap: 10px; flex-wrap: wrap; align-items: end; }
  label.field { font-size: 13px; color: #8b94a7; display: flex;
                flex-direction: column; gap: 4px; }
  input[type=date], input[type=time] {
    padding: 8px 10px; border: 1px solid #3a4356; border-radius: 8px;
    font-size: 14px; background: #232b3d; color: #e6eaf2;
  }
  button, .btn {
    background: #2f6fed; color: #fff; border: none; border-radius: 8px;
    padding: 9px 16px; font-size: 14px; cursor: pointer; min-width: 60px;
  }
  button:hover, .btn:hover { background: #4b82f5; }
  .table-scroll { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 14px;
          min-width: 640px; }
  th { text-align: left; color: #8b94a7; font-size: 12px;
       text-transform: uppercase; letter-spacing: .04em;
       padding: 8px 10px; border-bottom: 2px solid #333d52; }
  td { padding: 9px 10px; border-bottom: 1px solid #262f42; vertical-align: middle; }
  tr:nth-child(even) td { background: #1d2536; }
  .mono { font-family: "SF Mono", Consolas, "Courier New", monospace; }
  .badge-night {
    background: #2b3560; color: #aab8ff; border-radius: 999px;
    font-size: 11px; padding: 2px 8px; margin-left: 6px; white-space: nowrap;
  }
  .delta-pos { color: #f06a6a; }
  .delta-neg { color: #3ecf7a; }
  form.edit { display: inline-flex; gap: 6px; align-items: center; }
  form.edit input[type=time] { width: 96px; padding: 5px 6px; }
  form.edit button { padding: 5px 10px; min-width: 60px; }
  form.del { display: inline; }
  form.del button { background: #c94040; padding: 5px 12px; min-width: 60px; }
  form.del button:hover { background: #e05555; }
  .empty { color: #8b94a7; font-style: italic; padding: 12px 0; }
  a.more { color: #6f9bff; }
</style>
</head>
<body>
<div class="wrap">
  <h1>🌙 Учёт сна</h1>
  <p class="subtitle">Отбой вовремя — штраф меньше. Идеальное время: 23:00. Версия {{ version }}</p>

  <div class="cards">
    <div class="card">
      <div class="label">Текущий штраф</div>
      <div class="value {{ fine_class }}">{{ current_fine }} мин</div>
    </div>
    <div class="card">
      <div class="label">Записей</div>
      <div class="value">{{ total_count }}</div>
    </div>
    <div class="card">
      <div class="label">Последний отбой</div>
      <div class="value mono">{{ last_bedtime or "—" }}</div>
    </div>
  </div>

  <div class="panel">
    {% if chart_data_json %}
      <canvas id="sleepChart" height="260"></canvas>
    {% else %}
      <div class="empty">Добавьте первую запись, чтобы увидеть график.</div>
    {% endif %}
  </div>

  <div class="panel">
    <form class="inline-form" method="post" action="/save">
      <label class="field">Дата
        <input type="date" name="day" value="{{ today }}" required>
      </label>
      <label class="field">Отбой
        <input type="time" name="bedtime" value="23:00" required>
      </label>
      <button type="submit">Сохранить</button>
    </form>
  </div>

  <div class="panel">
    <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>Дата</th>
          <th>Отбой</th>
          <th>Изменение</th>
          <th>Штраф после вечера</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td>{{ r.day_human }}{% if r.level == 1 %} <span class="badge-night">ночь</span>{% endif %}</td>
          <td>
            <form class="edit" method="post" action="/save">
              <input type="hidden" name="old_day" value="{{ r.input_day }}">
              <input type="hidden" name="old_bedtime" value="{{ r.input_bedtime }}">
              <input type="hidden" name="day" value="{{ r.input_day }}">
              <input type="time" name="bedtime" value="{{ r.input_bedtime }}">
              <button type="submit">OK</button>
            </form>
          </td>
          <td class="{{ r.delta_class }}">{{ r.delta_text }}</td>
          <td class="mono {{ r.fine_class_row }}">{{ r.fine_after }}</td>
          <td>
            <form class="del" method="post" action="/delete">
              <input type="hidden" name="day" value="{{ r.input_day }}">
              <input type="hidden" name="bedtime" value="{{ r.input_bedtime }}">
              <button type="submit">Удалить</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>
    {% if not show_all and rows|length < total_count %}
    <div style="text-align:center; margin-top:16px;">
      <a class="more" href="?show_all=1" style="text-decoration:none;">
        Показать еще ({{ total_count - rows|length }} записей)
      </a>
    </div>
    {% endif %}
  </div>
</div>
{% if chart_data_json %}
<script>
  const chartData = {{ chart_data_json | safe }};

  new Chart(document.getElementById('sleepChart'), {
    type: 'line',
    data: {
      labels: chartData.map(d => d.date),
      datasets: [{
        label: 'Штраф, мин',
        data: chartData.map(d => d.fine),
        tension: 0.3,               // плавная (сглаженная) линия
        pointRadius: 0,             // без точек на холсте
        pointHitRadius: 10,         // но наведение работает
        borderWidth: 2,
        fill: false,
        segment: {                  // цвет каждого сегмента по значению штрафа
          borderColor: ctx => {
            const v = Math.max(ctx.p0.parsed.y, ctx.p1.parsed.y);
            if (v <= 0)   return '#3ecf7a';
            if (v <= 100) return '#8ed173';
            if (v <= 200) return '#e8c341';
            if (v <= 300) return '#ef9a5f';
            return '#f06a6a';
          }
        }
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {                 // стандартные тултипы: дата + значение
          backgroundColor: '#232b3d',
          titleColor: '#e6eaf2',
          bodyColor: '#e6eaf2',
          borderColor: '#3a4356',
          borderWidth: 1,
          callbacks: {
            label: ctx => ' Штраф: ' + ctx.parsed.y + ' мин'
          }
        }
      },
      scales: {
        x: {
          grid: { color: '#3a4356' },
          ticks: { color: '#8b94a7', maxRotation: 45, autoSkip: true }
        },
        y: {
          beginAtZero: true,
          grid: { color: '#3a4356' },
          ticks: { color: '#8b94a7' }
        }
      }
    }
  });
</script>
{% endif %}
</body>
</html>
"""


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
    state = compute_state(entries)
    total_count = len(state)
    # История показывается от последней даты к более ранним.
    view = (state if show_all else state[-10:])[::-1]

    rows = []
    for r in view:
        # Ночная запись (отбой 00:00-11:59) относится к вечеру ПРЕДЫДУЩЕГО
        # дня: в истории она показывается под своей эффективной датой вечера.
        if r["level"] == 1:
            disp_day, _, _ = effective_evening(r["input_day"], r["input_bedtime"])
        else:
            disp_day = r["input_day"]
        d = datetime.strptime(disp_day, "%Y-%m-%d")
        day_human = d.strftime("%d.%m.%Y") + " (" + DAY_MAP[d.strftime("%a")] + ")"
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

    from jinja2 import Template
    html = Template(PAGE).render(
        rows=rows,
        chart_data_json=chart_data_json,
        current_fine=current_fine,
        fine_class=fine_class,
        last_bedtime=last_bedtime,
        total_count=total_count,
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
        evening_anchor(day, bedtime)  # валидация формата
    except ValueError:
        return redirect("/")

    conn = get_db()
    try:
        # Одна дата — один отбой: не допускаем двух ВЕЧЕРНИХ записей
        # (12:00-23:59) одной календарной даты. Ночная запись этой же даты —
        # это продолжение ДРУГОГО (предыдущего) вечера, её не трогаем.
        new_anchor = evening_anchor(day, bedtime)
        new_lvl = level_of(new_anchor)

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
        if new_lvl == 1 and _ghost(night_key(new_anchor)):
            conn.commit()
            return redirect("/")

        if old_day and old_bedtime:
            # Инлайн-правка существующей записи.
            try:
                old_anchor = evening_anchor(old_day, old_bedtime)
            except ValueError:
                old_anchor = None
            old_lvl = level_of(old_anchor) if old_anchor is not None else None

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
                        (night_key(old_anchor),))

        clash = conn.execute(
            "SELECT bedtime FROM sleep_entries WHERE day=?", (day,)).fetchall()
        for row in clash:
            other = row["bedtime"]
            if (other == bedtime or
                    level_of(evening_anchor(day, other)) == new_lvl):
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
                (str(night_key(evening_anchor(day, bedtime))),))
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
            anchor = evening_anchor(day, bedtime)
            if level_of(anchor) == 1:
                conn.execute(
                    "INSERT OR REPLACE INTO deleted_nights (evening_day) "
                    "VALUES (?)",
                    (night_key(anchor),))
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
