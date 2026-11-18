"""ВК-бот: аниме-серии -> субтитры -> склейка -> 4 тиктока.

Формат сообщения (как в skachka/batch_dl.py):
    ссылка | серии | озвучка | плеер | качество

Примеры:
    https://yummyani.me/anime/1234 | 1-12 | AniLibria | Kodik | 1080
    https://animego.org/anime/... | 5 | - | - | 720
    cvh:1234 | все | - | - | 1080
    ссылка | ?   (только показать доступные озвучки/плееры)

Запуск:  python bot2\\bot2.py
"""

import asyncio
import logging
import os
import re
import shutil
import sys
from pathlib import Path

import paths2  # noqa: F401  — добавляет корень проекта в sys.path

# У api.vk.ru сертификат от CA, которого нет в системном хранилище Windows,
# а в certifi он есть. aiohttp (vkbottle) по умолчанию берёт системные корни,
# поэтому подсовываем ему бандл certifi до создания сессии.
try:
    import certifi as _certifi

    os.environ.setdefault("SSL_CERT_FILE", _certifi.where())
except Exception:  # pragma: no cover - certifi есть в зависимостях
    pass

from vkbottle import Bot, BotTypes

import config
import tiktok2
import video_pipeline2
from pipeline2 import process_job
from queue2 import Job2, QueueManager2

logger = logging.getLogger("vk_bot2")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Консоль Windows часто в cp1251 — эмодзи в прогрессе не должны ронять процесс.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


EPISODES_RE = re.compile(r"^(\d+(-\d+)?|все|all|\?|list|список|-)$", re.IGNORECASE)

HELP = """🤖 Аниме → английские субтитры → склейка → 4 тиктока

Формат сообщения (как в batch_dl):
ссылка | серии | озвучка | плеер | качество

Примеры:
https://yummyani.me/anime/1234 | 1-12 | AniLibria | Kodik | 1080
https://animego.org/anime/... | 5 | - | - | 720
cvh:1234 | все | - | - | 1080
ссылка | ?  — показать доступные озвучки и плееры

Серии: 5, 1-12, все. «-» — все доступные. Качество по умолчанию 1080.
Если серии не указать — скачается 1 серия.

Команды:
/status — состояние ваших задач
/cancel — отменить задачи в очереди"""


def parse_request(text: str) -> dict:
    parts = [p.strip() for p in text.split("|")]
    url = parts[0].strip()
    if not url:
        raise ValueError("Первым полем должна быть ссылка")
    if not (re.match(r"^https?://", url, re.IGNORECASE) or url.lower().startswith("cvh:")):
        raise ValueError(
            "Ссылка должна начинаться с http:// или https:// (или cvh:<id>)"
        )

    parts += ["-"] * (5 - len(parts))
    has_episodes = len([p for p in text.split("|")]) >= 2
    episodes = (parts[1] if has_episodes else "") or "1"
    dub = parts[2] or "-"
    player = parts[3] or "-"
    quality_text = parts[4] or "1080"

    if not EPISODES_RE.match(episodes):
        raise ValueError("Серии: число (5), диапазон (1-12) или «все»")

    if quality_text == "-":
        quality = 1080
    elif quality_text.isdigit():
        quality = int(quality_text)
        if not 240 <= quality <= 2160:
            raise ValueError("Качество: от 240 до 2160, например 1080")
    else:
        raise ValueError("Качество: число, например 1080")

    return {
        "url": url,
        "episodes": episodes,
        "dub": dub,
        "player": player,
        "quality": quality,
    }


queue = QueueManager2(process_job)
bot = Bot(config.VK_TOKEN)


def _random_id() -> int:
    return int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF


def _chunks(text: str, size: int = 3900):
    text = text or ""
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


async def _send(user_id: int, text: str) -> None:
    for chunk in _chunks(text):
        await bot.api.messages.send(
            peer_id=user_id, random_id=_random_id(), message=chunk
        )


async def _on_done(job: Job2) -> None:
    try:
        await _send(job.user_id, job.result or f"✅ Задача №{job.id} готова")
    except Exception as exc:
        logger.exception("Не удалось отправить итог: %s", exc)


async def _on_error(job: Job2, message: str) -> None:
    try:
        await _send(job.user_id, f"❌ Задача №{job.id} не выполнена:\n{message}")
    except Exception as exc:
        logger.exception("Не удалось отправить ошибку: %s", exc)


async def _on_progress(job: Job2, message: str) -> None:
    try:
        await _send(job.user_id, f"№{job.id} | {message}")
    except Exception as exc:
        logger.exception("Не удалось отправить прогресс: %s", exc)


@bot.on.message()
async def message_handler(message: BotTypes.Message) -> None:
    text = (message.text or "").strip()
    peer_id = message.peer_id
    if not text:
        return

    low = text.lower()
    if low in ("/help", "help", "помощь", "/start", "start", "начать"):
        await message.answer(HELP)
        return

    if low == "/cancel":
        cancelled = queue.cancel_user_jobs(peer_id)
        if cancelled:
            await message.answer(f"❌ Отменено задач в очереди: {cancelled}")
        else:
            await message.answer("В очереди нет ваших задач")
        return

    if low == "/status":
        rows = queue.list_user_jobs(peer_id)
        if not rows:
            await message.answer("У вас пока нет задач")
            return
        lines = []
        for job_id, url, episodes, dub, player, quality, status, error in rows:
            short = url if len(url) <= 60 else url[:57] + "..."
            line = f"№{job_id} [{status}] {short} | серии {episodes} | {dub} | {player} | {quality}p"
            if error and status == "failed":
                line += f"\n   ↳ {error[:160]}"
            lines.append(line)
        await message.answer("\n".join(lines))
        return

    try:
        request = parse_request(text)
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}\n\n{HELP}")
        return

    job = queue.submit(
        url=request["url"],
        user_id=peer_id,
        episodes=request["episodes"],
        dub=request["dub"],
        player=request["player"],
        quality=request["quality"],
    )
    await message.answer(
        f"✅ Принято! Задача №{job.id}\n"
        f"Серии: {job.episodes} | озвучка: {job.dub} | плеер: {job.player} | {job.quality}p\n"
        "Начну, как только освободится очередь."
    )


async def _selftest(request_text: str) -> None:
    """Полный цикл без ВК: удобно проверять на реальной ссылке."""
    request = parse_request(request_text)
    job = Job2(
        id=0,
        url=request["url"],
        episodes=request["episodes"],
        dub=request["dub"],
        player=request["player"],
        quality=request["quality"],
        user_id=0,
        status="selftest",
    )

    async def progress(message: str) -> None:
        print(message, flush=True)

    result = await process_job(job, progress)
    print("\n" + "=" * 60)
    print(result)


async def _selftest_local(folder: str) -> None:
    """Офлайн-проверка цепочки на готовой папке с 1.mp4, 2.mp4, ..."""
    source_dir = Path(folder).resolve()
    if not source_dir.is_dir():
        raise SystemExit(f"Папка не найдена: {source_dir}")

    work = paths2.BOT_DIR / "selftest"
    if work.exists():
        shutil.rmtree(work)
    input_dir = work / "input"
    input_dir.mkdir(parents=True)

    copied = 0
    for path in sorted(source_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in video_pipeline2.VIDEO_EXTENSIONS:
            shutil.copy2(path, input_dir / path.name)
            copied += 1
    if copied == 0:
        raise SystemExit(f"В папке нет видеофайлов: {source_dir}")

    async def progress(message: str) -> None:
        print(message, flush=True)

    merged = work / "merged.mp4"
    print(f"Проверка на {copied} файлах из {source_dir} (копии, оригиналы не трогаются)")
    merged_path = await video_pipeline2.process_videos(
        input_dir=input_dir,
        stage_dir=work / "stage",
        merged_path=merged,
        logo_path=paths2.LOGO_PATH,
        progress_coro=progress,
    )
    clips = await asyncio.to_thread(
        tiktok2.make_clips,
        merged_path,
        paths2.TOP_IMAGE,
        paths2.BOTTOM_IMAGE,
        work / "tiktok",
        clips=paths2.DEFAULT_TIKTOK_CLIPS,
        segment_duration=paths2.DEFAULT_SEGMENT_SECONDS,
        segments_per_clip=paths2.DEFAULT_SEGMENTS_PER_CLIP,
        speed=paths2.SPEED_FACTOR,
        log=lambda message: print(message, flush=True),
    )
    print("\n" + "=" * 60)
    print(f"Итог: {merged_path}")
    for clip in clips:
        print(f"Тикток: {clip}")


def main() -> None:
    queue.set_notifiers(_on_done, _on_error, _on_progress)

    if "--selftest-local" in sys.argv:
        index = sys.argv.index("--selftest-local")
        folder = sys.argv[index + 1] if len(sys.argv) > index + 1 else "."
        asyncio.run(_selftest_local(folder))
        return

    if "--selftest" in sys.argv:
        index = sys.argv.index("--selftest")
        request_text = " ".join(sys.argv[index + 1 :]).strip()
        if not request_text:
            raise SystemExit(
                'Использование: python bot2\\bot2.py --selftest "ссылка | 1 | - | - | 1080"'
            )
        asyncio.run(_selftest(request_text))
        return

    logger.info("Бот bot2 запущен")
    try:
        asyncio.run(bot.run_polling())
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем")


if __name__ == "__main__":
    main()
