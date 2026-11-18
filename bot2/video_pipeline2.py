"""Клон логики local_bot.py как функции для ВК-бота.

Вход: папка с пронумерованными видео (1.mp4 — дисклеймер, 2..N — серии).
Выход: один склеенный файл merged.mp4.

Логика 1:1 с local_bot.py:
  * нумерация по ведущим цифрам имени файла;
  * Whisper (GPU) + перевод на английский + субтитры;
  * водяной знак logo.png при прожиге;
  * пустой SRT -> видео копируется без субтитров;
  * склейка перекодированием с подгонкой размеров;
  * проверка длительности/рассинхрона.

Дополнительно: если у сегмента нет аудиодорожки, добавляется тишина,
иначе ffmpeg concat падает.
"""

import asyncio
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Awaitable, Callable, Optional

import ffmpeg

import merger_local2
import translator

logger = logging.getLogger("video_pipeline2")

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv"}


class Pipeline2Error(RuntimeError):
    pass


async def _noop(_message: str) -> None:
    pass


def _numbered_videos(input_dir: Path) -> list:
    result = []
    for path in sorted(Path(input_dir).iterdir()):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        match = re.match(r"^\s*(\d+)", path.stem)
        if match:
            result.append((int(match.group(1)), path))
    return sorted(result, key=lambda item: (item[0], item[1].name))


def _video_size(path: Path) -> tuple:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    values = proc.stdout.split()
    try:
        return int(values[0]), int(values[1])
    except (IndexError, ValueError):
        return 1920, 1080


def _has_audio(path: Path) -> bool:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_type",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    return "audio" in proc.stdout.lower()


def _ensure_audio(path: Path, work_dir: Path) -> Path:
    """Если в файле нет звука — добавляет дорожку тишины (для concat)."""
    if _has_audio(path):
        return path
    out = work_dir / f"silent_{path.stem}.mp4"
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-i", str(path),
            "-map", "0:a", "-map", "1:v",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not out.is_file():
        logger.warning("Не удалось добавить тишину в %s: %s", path.name, proc.stderr[-200:])
        return path
    return out


def _build_concat(parts, output: Path) -> None:
    sizes = [_video_size(p) for p in parts]
    target_w = max(size[0] for size in sizes)
    target_h = max(size[1] for size in sizes)
    uniform = all(size == (target_w, target_h) for size in sizes)
    streams = []
    for path, size in zip(parts, sizes):
        src = ffmpeg.input(str(path))
        if uniform:
            video = src.video
        else:
            video = (
                src.video
                .filter("scale", target_w, target_h, force_original_aspect_ratio="decrease")
                .filter("pad", target_w, target_h, "(ow-iw)/2", "(oh-ih)/2")
                .filter("setsar", "1")
            )
        audio = src.audio.filter(
            "aformat", sample_fmts="fltp", sample_rates=48000, channel_layouts="stereo"
        )
        streams.append(video)
        streams.append(audio)
    joined = ffmpeg.concat(*streams, v=1, a=1)
    out = ffmpeg.output(
        joined,
        str(output),
        vcodec="libx264",
        preset="veryfast",
        crf="18",
        pix_fmt="yuv420p",
        acodec="aac",
        movflags="+faststart",
        **{"b:a": "192k"},
    )
    try:
        ffmpeg.run(out, overwrite_output=True, quiet=True, capture_stderr=True)
    except ffmpeg.Error as exc:
        detail = exc.stderr.decode("utf-8", "ignore").strip().splitlines()
        raise Pipeline2Error(detail[-1][:300] if detail else "Ошибка ffmpeg") from exc


def _stream_duration(path: Path, stream_type: str) -> float:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", f"{stream_type}:0",
            "-show_entries", "stream=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return -1.0


def _validate_merged(parts, final: Path) -> tuple:
    sum_v = 0.0
    sum_a = 0.0
    for part in parts:
        sum_v += _stream_duration(part, "v")
        sum_a += _stream_duration(part, "a")
    final_v = _stream_duration(final, "v")
    final_a = _stream_duration(final, "a")
    reasons = []
    if sum_v > 0 and abs(final_v - sum_v) > 1.0:
        reasons.append(f"видео {final_v:.1f}с != сумма сегментов {sum_v:.1f}с")
    if final_v > 0 and final_a > 0 and abs(final_v - final_a) > 1.0:
        reasons.append(f"рассинхрон v={final_v:.1f}с / a={final_a:.1f}с")
    return (not reasons), "; ".join(reasons)


async def process_videos(
    input_dir: Path,
    stage_dir: Path,
    merged_path: Path,
    logo_path: Optional[Path] = None,
    progress_coro: Optional[Callable[[str], Awaitable[None]]] = None,
) -> Path:
    """Транскрибация + субтитры + водяной знак + склейка. Возвращает merged-файл."""
    progress = progress_coro or _noop
    input_dir = Path(input_dir)
    stage_dir = Path(stage_dir)
    merged_path = Path(merged_path)
    stage_dir.mkdir(parents=True, exist_ok=True)
    merged_path.parent.mkdir(parents=True, exist_ok=True)

    items = _numbered_videos(input_dir)
    if not items:
        raise Pipeline2Error(f"В {input_dir} нет пронумерованных видео (1.mp4, 2.mp4, ...)")

    seen = {}
    for number, path in items:
        seen.setdefault(number, []).append(path)
    for number, paths in seen.items():
        if len(paths) > 1:
            logger.warning(
                "Номер %s присвоен нескольким файлам: %s",
                number,
                ", ".join(p.name for p in paths),
            )

    done_numbers = []
    total = len(items)
    for index, (number, path) in enumerate(items, start=1):
        out_file = stage_dir / f"{number}_subtitled.mp4"
        await progress(f"🎙 {index}/{total}: {path.name} — распознавание речи...")
        try:
            srt_path = await translator.transcribe_video(
                path, progress_coro=progress, translate_to_en=True
            )
            if srt_path.stat().st_size == 0:
                logger.info("%s: речи не обнаружено — копирую видео без субтитров", number)
                shutil.copyfile(path, out_file)
            else:
                await merger_local2.merge_video_subtitles(
                    path,
                    srt_path,
                    stage_dir=stage_dir,
                    out_name=out_file.name,
                    watermark_path=logo_path if logo_path and logo_path.is_file() else None,
                    progress_coro=progress,
                )
        except Exception as exc:
            logger.exception("Ошибка обработки %s: %s", path.name, exc)
            (path.with_suffix(".srt")).unlink(missing_ok=True)
            continue
        (path.with_suffix(".srt")).unlink(missing_ok=True)
        done_numbers.append(number)
        await progress(f"✅ {index}/{total}: {path.name} готов")

    parts = [stage_dir / f"{n}_subtitled.mp4" for n in sorted(done_numbers)]
    parts = [p for p in parts if p.is_file()]
    if not parts:
        raise Pipeline2Error("Ни один фрагмент не обработан — склеивать нечего")

    silent_dir = stage_dir / "_audio_fix"
    silent_dir.mkdir(parents=True, exist_ok=True)
    parts = [await asyncio.to_thread(_ensure_audio, p, silent_dir) for p in parts]

    await progress(f"🔗 Склейка {len(parts)} видео в порядке нумерации...")
    await asyncio.to_thread(_build_concat, parts, merged_path)

    ok, reasons = _validate_merged(parts, merged_path)
    if not ok:
        logger.error("Итоговый файл рассинхронизирован: %s", reasons)
        await progress(f"⚠️ Проверка склейки: {reasons}")

    # Как в local_bot.py: пронумерованные исходники после успеха удаляются.
    for number in sorted(done_numbers):
        for path in seen.get(number, []):
            path.unlink(missing_ok=True)

    await progress(f"🎬 Итоговое видео: {merged_path.name}")
    return merged_path
