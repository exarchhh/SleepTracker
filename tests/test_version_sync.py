# -*- coding: utf-8 -*-
"""Автоматический контроль синхронизации версии (Задача №8, v0.11.2).

Единственная точка истины — строка VERSION в app.py (AGENTS.md §5,
DECISIONS 21). Производные точки — «Текущая версия» в README.md, футеры
BACKLOG.md и AGENTS.md, верхняя запись CHANGELOG.md — сверяются с VERSION.
Тест не знает целевую версию и переживает будущие бампы: любое расхождение
= падающий pytest = незакрытая задача.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(name):
    """Текст файла репозитория в UTF-8; финальный перевод строки не обязателен."""
    return (ROOT / name).read_text(encoding="utf-8")


def _check_version(file_name, text, pattern, version, flags=0):
    """Версия, найденная в файле по шаблону, совпадает с VERSION из app.py."""
    match = re.search(pattern, text, flags)
    assert match, "%s: версия не найдена по шаблону %r" % (file_name, pattern)
    found = match.group(1)
    assert found == version, (
        "%s: рассинхрон версии — ожидалось %s (VERSION в app.py), "
        "фактически %s" % (file_name, version, found))


def test_version_sync():
    """Производные точки версии соответствуют VERSION в app.py."""
    version_m = re.search(r'^VERSION\s*=\s*"(\d+\.\d+\.\d+)"',
                          _read("app.py"), re.MULTILINE)
    assert version_m, 'app.py: не найдена строка VERSION = "MAJ.MIN.PATCH"'
    version = version_m.group(1)

    _check_version("README.md", _read("README.md"),
                   r"\*\*Текущая версия: (\d+\.\d+\.\d+)\*\*", version)
    _check_version("BACKLOG.md", _read("BACKLOG.md"),
                   r"Последнее обновление: v(\d+\.\d+\.\d+)\*", version)
    _check_version("AGENTS.md", _read("AGENTS.md"),
                   r"Последнее обновление: v(\d+\.\d+\.\d+)\*", version)

    changelog_m = re.search(r"^## \[(\d+\.\d+\.\d+)\] - "
                            r"(\d{4}-\d{2}-\d{2})$",
                            _read("CHANGELOG.md"), re.MULTILINE)
    assert changelog_m, ("CHANGELOG.md: не найдена верхняя запись вида "
                         "'## [X.Y.Z] - YYYY-MM-DD' (дата — только формат)")
    assert changelog_m.group(1) == version, (
        "CHANGELOG.md: рассинхрон версии — ожидалось %s (VERSION в app.py), "
        "фактически %s" % (version, changelog_m.group(1)))
