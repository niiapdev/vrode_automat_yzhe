#!/usr/bin/env bash
# run_batch_dl.sh — запуск batch_dl.py через .venv с автоустановкой зависимостей.
# Запуск:
#   bash skachka/run_batch_dl.sh --check
#   bash skachka/run_batch_dl.sh
# Все аргументы передаются в batch_dl.py без изменений.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname -- "$SCRIPT_DIR")"
VENV_PY="$PROJECT_ROOT/.venv/bin/python"
BATCH_DL="$SCRIPT_DIR/batch_dl.py"
SETUP_DEPS="$SCRIPT_DIR/setup_deps.sh"

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

if [ ! -f "$BATCH_DL" ]; then
    echo "[ERROR] Не найден batch_dl.py: $BATCH_DL" >&2
    exit 2
fi

ready() {
    [ -x "$VENV_PY" ] && "$VENV_PY" -c "import requests, anime_dl_core, yt_dlp; rv=tuple(map(int, requests.__version__.split('.')[:2])); av=tuple(map(int, anime_dl_core.__version__.split('.')[:3])); raise SystemExit(0 if rv >= (2,28) and av >= (0,5,1) else 1)" >/dev/null 2>&1
}

if ! ready; then
    if [ ! -f "$SETUP_DEPS" ]; then
        echo "[ERROR] Не найден установщик: $SETUP_DEPS" >&2
        exit 2
    fi
    echo "==> Зависимости не готовы, запускаю setup_deps.sh"
    if ! bash "$SETUP_DEPS"; then
        echo "[ERROR] Установка зависимостей завершилась с ошибкой" >&2
        exit 2
    fi
    if ! ready; then
        echo "[ERROR] После установки .venv всё ещё не видит requests/anime_dl_core/yt_dlp" >&2
        exit 2
    fi
fi

exec "$VENV_PY" "$BATCH_DL" "$@"
