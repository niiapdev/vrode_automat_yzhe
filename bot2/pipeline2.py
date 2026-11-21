"""Оркестратор одного запроса ВК-бота.

ссылка | серии | озвучка | плеер | качество
  -> скачивание (batch_dl2)
  -> копия дисклеймера local_videos/previu/1.mp4 как «1»
  -> скачанные серии нумеруются с 2
  -> Whisper + перевод на англ. + субтитры + logo.png (video_pipeline2)
  -> склейка всех по номерам
  -> 4 тиктока 9:16 из склейки (tiktok2, кадры 1.png / 2.png)
  -> уборка промежуточных файлов, итог в local_out2/ и tiktok_out2/<название>/

В ВК уходят только этапные сообщения: скачивание -> обработка/склейка -> готово,
плюс отдельным сообщением таймкоды серий (timestamps2).
Подробности (каждая серия, каждый тикток) идут в консоль.
"""

import asyncio
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

import batch_dl2
import paths2
import tiktok2
import timestamps2
import video_pipeline2

logger = logging.getLogger("pipeline2")


class JobError(RuntimeError):
    pass


def _console_log(message: str) -> None:
    """Технические логи — только в консоль, в ВК они не уходят."""
    text = str(message).strip()
    if text:
        logger.info("%s", text)


async def _console_progress(message: str) -> None:
    _console_log(message)


def _episode_label(episodes, downloaded: int) -> str:
    """«серия 4» / «серии 1-15» / «серии 1, 3, 5»."""
    numbers = sorted({int(n) for n in episodes or []})
    if not numbers:
        return "серия 1" if downloaded <= 1 else f"серии 1-{downloaded}"
    if len(numbers) == 1:
        return f"серия {numbers[0]}"
    if numbers == list(range(numbers[0], numbers[-1] + 1)):
        return f"серии {numbers[0]}-{numbers[-1]}"
    return "серии " + ", ".join(str(n) for n in numbers)


def _merged_name(title: str, episodes, downloaded: int, job_tag: str) -> str:
    """Название аниме + какие серии внутри; фолбэк — служебное имя задачи."""
    if title:
        stem = batch_dl2.safe(f"{title} - {_episode_label(episodes, downloaded)}")
        if stem:
            return f"{stem}.mp4"
    return f"merged_{job_tag}.mp4"


def _free_path(path: Path) -> Path:
    """Не перезаписываем уже готовые файлы: «имя (2).mp4», «имя (3).mp4», ..."""
    path = Path(path)
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise JobError(f"Слишком много файлов с именем {path.name}")


def _rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(paths2.ROOT))
    except ValueError:
        return str(path)


def _validate_inputs() -> None:
    if not paths2.PREVIEW_VIDEO.is_file():
        raise JobError(f"Не найден дисклеймер: {_rel(paths2.PREVIEW_VIDEO)}")
    if not paths2.TOP_IMAGE.is_file():
        raise JobError(f"Не найдена картинка 1.png: {_rel(paths2.TOP_IMAGE)}")
    if not paths2.BOTTOM_IMAGE.is_file():
        raise JobError(f"Не найдена картинка 2.png: {_rel(paths2.BOTTOM_IMAGE)}")


async def process_job(
    job,
    progress: Callable[[str], Awaitable[None]],
) -> tuple[str, list[str]]:
    """Полный цикл обработки одной задачи.

    Возвращает (итоговый текст, [дополнительные сообщения]) — вторым идут
    таймкоды, если их удалось посчитать.
    """
    _validate_inputs()

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    job_tag = f"job{job.id:04d}_{timestamp}"
    job_dir = paths2.JOBS_DIR / job_tag
    downloads_dir = job_dir / "downloads"
    input_dir = job_dir / "input"
    stage_dir = job_dir / "stage"

    for directory in (downloads_dir, input_dir, stage_dir):
        directory.mkdir(parents=True, exist_ok=True)

    # Этап 1 из 3.
    await progress(
        "📥 Скачивание: "
        f"серии {job.episodes} | озвучка {job.dub} | плеер {job.player} | {job.quality}p"
    )

    download_result = await asyncio.to_thread(
        batch_dl2.download_series,
        job.url,
        episodes=job.episodes,
        dub=job.dub,
        player=job.player,
        quality=job.quality,
        out_dir=downloads_dir,
        token_path=paths2.ANIME_TOKEN_PATH,
        log=_console_log,  # подробности скачивания — в консоль
    )

    if not download_result.files:
        detail = "\n".join(download_result.problems[:5]) or "ни одной серии не скачалось"
        raise JobError(f"Скачивание не удалось: {detail}")

    downloaded = len(download_result.files)

    # Таймкоды: замер сразу после скачивания, пока файлы ещё в downloads.
    # Любой сбой здесь не должен ломать задачу — поэтому отдельный try.
    timeline = None
    try:
        timeline = await asyncio.to_thread(
            timestamps2.Timeline.measure,
            job_dir / timestamps2.JOURNAL_NAME,
            paths2.PREVIEW_VIDEO,
            download_result.files,
            download_result.episodes,
            _console_log,
        )
    except Exception:
        logger.exception("Таймкоды: замер не удался, продолжаю без них")

    # Имя итогового файла: название аниме + какие серии внутри.
    merged_path = _free_path(
        paths2.MERGED_DIR
        / _merged_name(
            download_result.title,
            download_result.episodes,
            downloaded,
            job_tag,
        )
    )
    tiktok_dir = _free_path(paths2.TIKTOK_OUT_DIR / merged_path.stem)

    # Этап 2 из 3. Дальше в ВК ничего не уходит, всё пишется в консоль.
    await progress(
        f"🎬 Скачано серий: {downloaded}. Обрабатываю: субтитры + склейка…"
    )

    # 1 — всегда дисклеймер (копия, оригинал остаётся на месте).
    preview_target = input_dir / "1.mp4"
    await asyncio.to_thread(shutil.copy2, paths2.PREVIEW_VIDEO, preview_target)

    # Скачанные серии: 2, 3, 4, ... по возрастанию номера серии.
    numbering = {}
    for index, source in enumerate(download_result.files, start=2):
        target = input_dir / f"{index}_{source.name}"
        if target.exists():
            target.unlink()
        await asyncio.to_thread(shutil.move, str(source), str(target))
        numbering[index] = source.name

    await video_pipeline2.process_videos(
        input_dir=input_dir,
        stage_dir=stage_dir,
        merged_path=merged_path,
        logo_path=paths2.LOGO_PATH,
        progress_coro=_console_progress,  # в ВК не дублируем
    )

    # Добор для серий, у которых не прочиталась длительность: части ещё живы.
    if timeline is not None:
        try:
            await asyncio.to_thread(timeline.retry_missing, stage_dir)
        except Exception:
            logger.exception("Таймкоды: добор по stage не удался")

    clips = await asyncio.to_thread(
        tiktok2.make_clips,
        merged_path,
        paths2.TOP_IMAGE,
        paths2.BOTTOM_IMAGE,
        tiktok_dir,
        clips=paths2.DEFAULT_TIKTOK_CLIPS,
        segment_duration=paths2.DEFAULT_SEGMENT_SECONDS,
        segments_per_clip=paths2.DEFAULT_SEGMENTS_PER_CLIP,
        speed=paths2.SPEED_FACTOR,
        log=_console_log,
    )

    # Уборка промежуточных файлов: остаются merged, тиктоки и маленький отчёт.
    for directory in (downloads_dir, input_dir, stage_dir):
        await asyncio.to_thread(shutil.rmtree, directory, True)

    # Таймкоды: отчёт в папку задачи + отдельное сообщение в ВК.
    timeline_message = None
    elapsed = None
    if timeline is not None:
        try:
            merged_seconds = await asyncio.to_thread(
                timestamps2.duration_seconds, merged_path
            )
            await asyncio.to_thread(
                timeline.write_report,
                job_dir / timestamps2.REPORT_NAME,
                merged_seconds,
            )
            timeline_message = timeline.message()
            elapsed = timestamps2.fmt_len(timeline.total)
        except Exception:
            logger.exception("Таймкоды: отчёт не удался, продолжаю без них")

    summary = {
        "job": job.id,
        "url": job.url,
        "episodes": job.episodes,
        "downloaded_episodes": download_result.episodes,
        "dub": job.dub,
        "player": job.player,
        "quality": job.quality,
        "title": download_result.title,
        "downloaded": downloaded,
        "numbering": numbering,
        "merged_name": merged_path.name,
        "merged": _rel(merged_path),
        "tiktok_dir": _rel(tiktok_dir),
        "tiktok": [_rel(p) for p in clips],
        "timings": None if timeline is None else {
            "journal": _rel(job_dir / timestamps2.JOURNAL_NAME),
            "report": _rel(job_dir / timestamps2.REPORT_NAME),
            "total": elapsed,
            "disclaimer": timeline.disclaimer,
            "entries": timeline.entries,
        },
        "problems": download_result.problems,
        "finished_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    (job_dir / "job.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Этап 3 из 3 — финальный отчёт.
    lines = [
        "✅ ГОТОВО!",
        "",
        f"Серий: {downloaded} | видео в склейке: {downloaded + 1} "
        "(дисклеймер + серии)",
        "",
        f"🎬 {_rel(merged_path)}",
        "",
        f"📱 Тиктоки ({len(clips)} шт.): {_rel(tiktok_dir)}",
    ]
    if download_result.problems:
        lines += ["", "⚠️ Замечания по скачиванию:"]
        lines += [f"• {p.strip()}" for p in download_result.problems[:8]]
    if timeline is None:
        lines += ["", "⚠️ Таймкоды не посчитаны — подробности в консоли бота"]
    return "\n".join(lines), ([timeline_message] if timeline_message else [])
