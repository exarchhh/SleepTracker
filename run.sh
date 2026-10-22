#!/usr/bin/env bash
# SleepTracker: автозапуск локального сервера (Linux/macOS)
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    python3 launcher.py
else
    python launcher.py
fi
