#!/usr/bin/env bash
# Personal Page Builder 启动脚本（macOS / Linux）
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
    echo "[x] 没有找到 $PY，请先安装 Python 3.8+" >&2
    exit 1
fi

if ! "$PY" -c "import PIL, webview, watchdog" >/dev/null 2>&1; then
    echo "[!] 缺少依赖，正在安装 Pillow / pywebview / watchdog ..."
    "$PY" -m pip install -r requirements.txt
fi

exec "$PY" personal_page_builder.py
