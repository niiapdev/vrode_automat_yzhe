"""Общие пути и настройки для цепочки bot2.

Всё новое лежит в подпапке bot2, в корне проекта ничего не меняется.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = Path(__file__).resolve().parent

# Корень проекта в sys.path — чтобы были доступны config, translator, merger_local.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

JOBS_DIR = BOT_DIR / "jobs2"
JOBS_DB = BOT_DIR / "jobs2.sqlite3"
MERGED_DIR = BOT_DIR / "local_out2"
LOG_DIR = BOT_DIR / "logs2"
# Готовые тиктоки: только внутри bot2 (tik_tok-scripts/output не используется).
TIKTOK_OUT_DIR = BOT_DIR / "tiktok_out2"

# Дисклеймер: НЕ удаляется и не изменяется, только копируется в папку задачи.
PREVIEW_VIDEO = ROOT / "local_videos" / "previu" / "1.mp4"
LOGO_PATH = ROOT / "local_videos" / "logo.png"

# Кадры для тиктоков 9:16 (оригиналы в tik_tok-scripts, только чтение).
TIKTOK_DIR = ROOT / "tik_tok-scripts"
TOP_IMAGE = TIKTOK_DIR / "1.png"
BOTTOM_IMAGE = TIKTOK_DIR / "2.png"

# Токен YummyAnime (только чтение, файл skachka не трогаем).
ANIME_TOKEN_PATH = ROOT / "skachka" / "token.txt"

# Параметры нарезки тиктоков (как в tik_tok-scripts/main.py).
DEFAULT_SEGMENT_SECONDS = 10.0
DEFAULT_SEGMENTS_PER_CLIP = 4
DEFAULT_TIKTOK_CLIPS = 4
SPEED_FACTOR = 2.0

for _dir in (JOBS_DIR, MERGED_DIR, LOG_DIR, TIKTOK_OUT_DIR):
    _dir.mkdir(parents=True, exist_ok=True)
