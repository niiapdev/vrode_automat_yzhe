#!/usr/bin/env bash
# setup_deps.sh — установка зависимостей batch_dl.py для Linux/macOS.
# Запуск:
#   bash skachka/setup_deps.sh
# Проверка без изменений:
#   bash skachka/setup_deps.sh --check-only
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname -- "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_ROOT/.venv"
VENV_PY="$VENV_DIR/bin/python"
REQUIREMENTS="$SCRIPT_DIR/requirements-batch_dl.txt"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/setup_deps.log"
mkdir -p "$LOG_DIR"

CHECK_ONLY=0
FORCE=0
SKIP_FFMPEG=0
SKIP_PYTHON=0
NON_INTERACTIVE=0
PROBLEMS=0
for arg in "$@"; do
    case "$arg" in
        --check-only) CHECK_ONLY=1 ;;
        --force) FORCE=1 ;;
        --skip-ffmpeg) SKIP_FFMPEG=1 ;;
        --skip-python) SKIP_PYTHON=1 ;;
        --non-interactive) NON_INTERACTIVE=1 ;;
        -h|--help)
            sed -n '2,9p' "$0"
            exit 0
            ;;
        *)
            echo "Неизвестный аргумент: $arg" >&2
            exit 2
            ;;
    esac
done

exec > >(tee -a "$LOG_FILE") 2>&1

info() { printf '\033[36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m  [OK] %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  [WARN] %s\033[0m\n' "$*"; }
err()  { printf '\033[31m  [ERROR] %s\033[0m\n' "$*" >&2; }
die()  { err "$*"; exit 2; }

trap 'err "Ошибка в строке $LINENO"; exit 2' ERR

if [ "$(id -u)" -eq 0 ]; then
    SUDO=()
elif command -v sudo >/dev/null 2>&1; then
    if [ "$NON_INTERACTIVE" -eq 1 ]; then
        SUDO=(sudo -n)
    else
        SUDO=(sudo)
    fi
else
    SUDO=()
    warn "sudo не найден; системные пакеты можно установить только от root"
fi

run() {
    info "$*"
    "$@"
}

as_root() {
    if [ "${#SUDO[@]}" -gt 0 ]; then
        "${SUDO[@]}" "$@"
    else
        "$@"
    fi
}

run_root() {
    info "$*"
    as_root "$@"
}

detect_os() {
    case "$(uname -s)" in
        Linux*)  echo "linux" ;;
        Darwin*) echo "macos" ;;
        *)       echo "unknown" ;;
    esac
}

python_ready() {
    "$1" -c "import requests, anime_dl_core, yt_dlp; rv=tuple(map(int, requests.__version__.split('.')[:2])); av=tuple(map(int, anime_dl_core.__version__.split('.')[:3])); raise SystemExit(0 if rv >= (2,28) and av >= (0,5,1) else 1)" >/dev/null 2>&1
}

install_packages() {
    local os
    os="$(detect_os)"
    if [ "$os" = "macos" ]; then
        if command -v brew >/dev/null 2>&1; then
            run brew install python ffmpeg
        else
            warn "Homebrew не найден. Установите Python и ffmpeg вручную или поставьте brew:"
            warn '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
        fi
        return 0
    fi
    if command -v apt-get >/dev/null 2>&1; then
        as_root apt-get update -y || warn "apt-get update завершился с ошибкой"
        run_root apt-get install -y python3 python3-venv python3-pip ffmpeg ca-certificates curl xz-utils
    elif command -v dnf >/dev/null 2>&1; then
        run_root dnf install -y python3 python3-pip ffmpeg ca-certificates curl xz
    elif command -v pacman >/dev/null 2>&1; then
        run_root pacman -Sy --needed --noconfirm python python-pip ffmpeg curl xz
    elif command -v zypper >/dev/null 2>&1; then
        run_root zypper install -y python3 python3-pip ffmpeg curl xz
    elif command -v apk >/dev/null 2>&1; then
        run_root apk add --no-cache python3 py3-pip ffmpeg curl xz
    else
        warn "Не найден известный пакетный менеджер (apt/dnf/pacman/zypper/apk/brew)"
    fi
}

find_python() {
    local candidate version major minor
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            version="$("$candidate" -c 'import sys; print(sys.version_info[0], sys.version_info[1])' 2>/dev/null || true)"
            major="${version%% *}"
            minor="${version##* }"
            if [ -n "$major" ] && [ "$major" -ge 3 ] && [ "$minor" -ge 9 ]; then
                command -v "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

download() {
    local url="$1" out="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 4 --retry-delay 2 --connect-timeout 30 -o "$out" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$out" "$url"
    else
        die "Нужен curl или wget для скачивания $url"
    fi
}

install_static_ffmpeg() {
    local os arch url tmp dest found
    os="$(detect_os)"
    arch="$(uname -m)"
    if [ "$os" = "macos" ]; then
        warn "На macOS статическая сборка не устанавливается автоматически; нужен brew install ffmpeg"
        return 1
    fi
    case "$arch" in
        x86_64|amd64) url="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz" ;;
        aarch64|arm64) url="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz" ;;
        *) warn "Нет статической сборки ffmpeg для архитектуры $arch"; return 1 ;;
    esac
    tmp="$(mktemp -d)"
    dest="$HOME/.local/opt/ffmpeg"
    mkdir -p "$dest" "$HOME/.local/bin"
    info "Скачиваю статический ffmpeg: $url"
    download "$url" "$tmp/ffmpeg.tar.xz"
    tar -xJf "$tmp/ffmpeg.tar.xz" -C "$tmp"
    found="$(find "$tmp" -type f -name ffmpeg -perm -u+x | head -n 1 || true)"
    if [ -z "$found" ]; then
        warn "В архиве не найден ffmpeg"
        rm -rf "$tmp"
        return 1
    fi
    cp -f "$found" "$dest/ffmpeg"
    cp -f "$(dirname "$found")/ffprobe" "$dest/ffprobe" 2>/dev/null || true
    chmod +x "$dest/ffmpeg" "$dest/ffprobe" 2>/dev/null || true
    ln -sf "$dest/ffmpeg" "$HOME/.local/bin/ffmpeg"
    ln -sf "$dest/ffprobe" "$HOME/.local/bin/ffprobe" 2>/dev/null || true
    export PATH="$HOME/.local/bin:$PATH"
    rm -rf "$tmp"
    ok "ffmpeg установлен в $dest"
}

info "=== batch_dl: установка зависимостей ==="
echo "Проект : $PROJECT_ROOT"
echo "Скрипт : $SCRIPT_DIR"
if [ "$CHECK_ONLY" -eq 1 ]; then echo "Режим  : только проверка"; else echo "Режим  : установка"; fi

PYTHON_BIN="$(find_python || true)"
if [ -z "$PYTHON_BIN" ]; then
    if [ "$CHECK_ONLY" -eq 1 ]; then
        err "Python 3.9+ не найден"
        exit 1
    fi
    install_packages
    PYTHON_BIN="$(find_python || true)"
fi
[ -n "$PYTHON_BIN" ] || die "Python 3.9+ не найден после установки"
ok "Python: $PYTHON_BIN ($("$PYTHON_BIN" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'))"

if [ "$SKIP_PYTHON" -eq 0 ]; then
    if [ -x "$VENV_PY" ] && [ "$FORCE" -eq 1 ] && [ "$CHECK_ONLY" -eq 0 ]; then
        warn "Удаляю старый .venv (--force)"
        rm -rf "$VENV_DIR"
    fi
    if [ ! -x "$VENV_PY" ]; then
        if [ "$CHECK_ONLY" -eq 1 ]; then
            warn ".venv не найден"
            PROBLEMS=1
        else
            info "Создаю .venv"
            if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
                warn "venv не создался; пробую установить python3-venv"
                install_packages || true
                "$PYTHON_BIN" -m venv "$VENV_DIR" || die "Не удалось создать .venv"
            fi
        fi
    fi
    if [ -x "$VENV_PY" ]; then
        ok ".venv: $VENV_PY"
        if python_ready "$VENV_PY" && [ "$FORCE" -eq 0 ]; then
            ok "Python-пакеты уже установлены"
        elif [ "$CHECK_ONLY" -eq 1 ]; then
            warn "Python-пакеты не установлены"
            PROBLEMS=1
        else
            info "Устанавливаю Python-зависимости"
            if [ -f "$REQUIREMENTS" ]; then
                "$VENV_PY" -m pip install --disable-pip-version-check --no-input --prefer-binary \
                    --timeout 60 --retries 5 -r "$REQUIREMENTS" || \
                "$VENV_PY" -m pip install --disable-pip-version-check --no-input \
                    --timeout 60 --retries 5 -r "$REQUIREMENTS"
            else
                "$VENV_PY" -m pip install --disable-pip-version-check --no-input --prefer-binary \
                    --timeout 60 --retries 5 'requests>=2.31,<3' 'anime-dl-core==0.5.1' 'yt-dlp>=2024.1.1'
            fi
            python_ready "$VENV_PY" || die "Пакеты не импортируются"
            ok "Python-зависимости установлены"
        fi
    fi
else
    warn "Установка Python-пакетов пропущена (--skip-python)"
fi

if [ "$SKIP_FFMPEG" -eq 0 ]; then
    FFMPEG_BIN="$(command -v ffmpeg || true)"
    if [ -z "$FFMPEG_BIN" ] && [ "$CHECK_ONLY" -eq 0 ]; then
        install_packages
        FFMPEG_BIN="$(command -v ffmpeg || true)"
    fi
    if [ -z "$FFMPEG_BIN" ] && [ "$CHECK_ONLY" -eq 0 ]; then
        install_static_ffmpeg || true
        FFMPEG_BIN="$(command -v ffmpeg || true)"
    fi
    if [ -n "$FFMPEG_BIN" ] && "$FFMPEG_BIN" -version >/dev/null 2>&1; then
        ok "ffmpeg: $FFMPEG_BIN"
        "$FFMPEG_BIN" -version | sed -n '1p'
        if command -v ffprobe >/dev/null 2>&1; then
            ok "ffprobe: $(command -v ffprobe)"
        else
            warn "ffprobe не найден"
            if [ "$CHECK_ONLY" -eq 1 ]; then PROBLEMS=1; fi
        fi
        if "$FFMPEG_BIN" -hide_banner -encoders 2>/dev/null | grep 'libx264' >/dev/null 2>&1; then
            ok "кодек libx264 доступен (нужен пайплайну)"
        else
            warn "libx264 не найден в этой сборке ffmpeg"
        fi
    else
        warn "ffmpeg не найден"
        if [ "$CHECK_ONLY" -eq 1 ]; then PROBLEMS=1; fi
    fi
else
    warn "Установка ffmpeg пропущена (--skip-ffmpeg)"
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
    echo ""
    if [ "$PROBLEMS" -ne 0 ]; then
        echo "=== ПРОВЕРКА ЗАВЕРШЕНА: ЕСТЬ ПРОБЛЕМЫ ==="
        echo "Запустите установщик без --check-only:"
        echo "  bash \"$SCRIPT_DIR/setup_deps.sh\""
        exit 1
    fi
    echo "=== ПРОВЕРКА ЗАВЕРШЕНА: ВСЁ В ПОРЯДКЕ ==="
    exit 0
fi

echo ""
echo "=== ГОТОВО ==="
echo "Проверка:"
echo "  \"$VENV_PY\" \"$SCRIPT_DIR/batch_dl.py\" --check"
echo "Запуск:"
echo "  \"$VENV_PY\" \"$SCRIPT_DIR/batch_dl.py\""
