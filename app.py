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

VERSION = "0.6.0"           # MAJ.MIN.PATCH — синхронно с README.md

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
                           "full_date": d.isoformat(),
                           "fine": r["fine_after"]})
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


def compute_avg_bedtime_month(state):
    """Среднее время отбоя за последние 30 календарных дней ("HH:MM")."""
    if not state:
        return None
    last_day = max(datetime.strptime(r["input_day"], "%Y-%m-%d")
                   for r in state)
    cutoff = last_day - timedelta(days=29)
    minutes = []
    for r in state:
        d = datetime.strptime(r["input_day"], "%Y-%m-%d")
        if d >= cutoff:
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
  .chart-panel { min-height: 400px; }
  .chart-box { position: relative; height: 360px; }
  .period-selector { display: flex; gap: 8px; flex-wrap: wrap;
                     margin-bottom: 14px; }
  .period-selector button {
    background: #2a3550; color: #8b94a7; border: none; border-radius: 8px;
    padding: 7px 14px; font-size: 13px; cursor: pointer; min-width: 0;
  }
  .period-selector button:hover { background: #3a4560; }
  .period-selector button.active { background: #2f6fed; color: #fff; }
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
    <div class="card">
      <div class="label">Текущий стрик</div>
      <div class="value {{ 'green' if current_streak > 0 else '' }}">
        {{ current_streak }} {{ streak_word }}
      </div>
    </div>
    <div class="card">
      <div class="label">Средний отбой (мес)</div>
      <div class="value mono">{{ avg_bedtime_month or "—" }}</div>
    </div>
  </div>

  <div class="panel chart-panel">
    {% if chart_data_json %}
    <div class="period-selector">
      <button data-period="week">Неделя</button>
      <button data-period="2weeks">2 недели</button>
      <button data-period="month">Месяц</button>
      <button data-period="3months">3 месяца</button>
      <button data-period="6months">Полгода</button>
      <button data-period="year">Год</button>
      <button data-period="calendar_year" class="active">Календарный год</button>
    </div>
    <div class="chart-box">
      <canvas id="sleepChart"></canvas>
    </div>
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

  // --- Фильтрация данных по выбранному периоду -------------------------
  // Полные даты ("YYYY-MM-DD") нужны только для фильтрации; на оси X
  // остаётся короткая метка "DD.MM".
  function toDate(s) { return new Date(s + 'T00:00:00'); }

  function filterByPeriod(period) {
    if (!chartData.length) return chartData;
    const dates = chartData.map(d => toDate(d.full_date));
    const last = dates.reduce((a, b) => a > b ? a : b);
    let from, to;
    if (period === 'calendar_year') {
      const y = last.getFullYear();
      from = new Date(y, 0, 1);            // 1 января года последней записи
      to = new Date(y, 11, 31, 23, 59, 59); // 31 декабря
    } else {
      const days = { week: 7, '2weeks': 14, month: 30, '3months': 90,
                     '6months': 180, year: 365 }[period] || 365;
      from = new Date(last);
      from.setDate(from.getDate() - days + 1);
      to = last;
    }
    const out = chartData.filter((d, i) => dates[i] >= from && dates[i] <= to);
    return out.length ? out : chartData.slice(-1); // не показывать пустой график
  }

  const gradientColors = v => {          // зелёный (0) -> красный (большие)
    if (v <= 0)   return '#3ecf7a';
    if (v <= 100) return '#8ed173';
    if (v <= 200) return '#e8c341';
    if (v <= 300) return '#ef9a5f';
    return '#f06a6a';
  };

  const initial = filterByPeriod('calendar_year');
  const chart = new Chart(document.getElementById('sleepChart'), {
    type: 'line',
    data: {
      labels: initial.map(d => d.date),
      datasets: [{
        label: 'Штраф, мин',
        data: initial.map(d => d.fine),
        tension: 0.3,               // плавная (сглаженная) линия
        pointRadius: 0,             // без точек на холсте
        pointHitRadius: 10,         // но наведение работает
        borderWidth: 2,
        fill: false,
        segment: {                  // цвет каждого сегмента по значению штрафа
          borderColor: ctx => gradientColors(
            Math.max(ctx.p0.parsed.y, ctx.p1.parsed.y))
        }
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,   // занимать всю высоту .chart-box
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

  // --- Переключение периода ------------------------------------------------
  document.querySelectorAll('.period-selector button').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.period-selector button')
              .forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const filtered = filterByPeriod(btn.dataset.period);
      chart.data.labels = filtered.map(d => d.date);
      chart.data.datasets[0].data = filtered.map(d => d.fine);
      chart.update();
    });
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

    current_streak = compute_streak(state)
    avg_bedtime_month = compute_avg_bedtime_month(state)
    n = current_streak % 10
    if n == 1 and current_streak != 11:
        streak_word = "день"
    elif 2 <= n <= 4 and not (12 <= current_streak % 100 <= 14):
        streak_word = "дня"
    else:
        streak_word = "дней"

    from jinja2 import Template
    html = Template(PAGE).render(
        rows=rows,
        chart_data_json=chart_data_json,
        current_fine=current_fine,
        fine_class=fine_class,
        last_bedtime=last_bedtime,
        total_count=total_count,
        current_streak=current_streak,
        streak_word=streak_word,
        avg_bedtime_month=avg_bedtime_month,
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
