#!/usr/bin/env python3
"""Таймкоды серий внутри склеенного видео.

Скрипт ничего не склеивает, не переименовывает и не удаляет: он только читает
длительности готовых файлов (быстро, из заголовка контейнера) и пишет список
вида

    00:00 - 1 серия
    12:32 - 2 серия

Нумерация как в bot2/video_pipeline2.py: файл 1 — дисклеймер, файл 2 — первая
серия. Поэтому первая серия — это 1 + 2 вместе, а вторая начинается после них.

Запуск:
    python timestamps.py
    python timestamps.py "D:\\for-move\\local_out"
    python timestamps.py local_out --merged "local_out\\merged_20261120-222912.mp4"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".m4v", ".mov", ".avi", ".ts", ".flv"}

# "1_subtitled.mp4", "2.mp4", "07 - серия.mkv" -> номер в начале имени
NUMBERED_RE = re.compile(r"^(\d+)(?=[_\-\s.]|$)")

DEFAULT_FOLDER = Path(__file__).resolve().parent / "local_out"


def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def find_ffprobe(explicit: str | None) -> str:
    """ffprobe из --ffprobe, PATH или рядом с ffmpeg."""
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise SystemExit(f"ffprobe не найден: {path}")
        return str(path)

    found = shutil.which("ffprobe")
    if found:
        return found

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        candidate = Path(ffmpeg).with_name(
            "ffprobe.exe" if os.name == "nt" else "ffprobe"
        )
        if candidate.exists():
            return str(candidate)

    raise SystemExit(
        "ffprobe не найден. Установите ffmpeg или укажите путь: --ffprobe path\\to\\ffprobe.exe"
    )


def duration_seconds(ffprobe: str, path: Path) -> float:
    """Длительность файла в секундах. Только чтение заголовков, без декодирования."""
    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration:stream=duration",
        "-of", "json",
        str(path),
    ]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        raw = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
            timeout=60,
        )
    except FileNotFoundError:
        raise SystemExit(f"ffprobe не запускается: {ffprobe}")

    if raw.returncode != 0:
        raise SystemExit(f"ffprobe не смог прочитать {path.name}:\n{raw.stderr.strip()}")

    try:
        data = json.loads(raw.stdout or "{}")
    except json.JSONDecodeError:
        raise SystemExit(f"ffprobe вернул непонятный ответ для {path.name}")

    value = (data.get("format") or {}).get("duration")
    if value not in (None, "N/A"):
        try:
            seconds = float(value)
            if seconds > 0:
                return seconds
        except ValueError:
            pass

    # запасной вариант: самый длинный поток
    streams = data.get("streams") or []
    durations = []
    for stream in streams:
        try:
            durations.append(float(stream.get("duration") or 0))
        except ValueError:
            continue
    if durations and max(durations) > 0:
        return max(durations)

    raise SystemExit(f"не удалось определить длительность: {path.name}")


def durations_for(ffprobe: str, paths: list[Path]) -> list[float]:
    """Длительности параллельно: ffprobe читает только заголовки, файлы не грузятся целиком."""
    if not paths:
        return []
    workers = min(8, len(paths))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda path: duration_seconds(ffprobe, path), paths))


def numbered_videos(folder: Path) -> list[tuple[int, Path]]:
    """Видео с числовым префиксом, отсортированные по номеру (а не по алфавиту)."""
    items: list[tuple[int, Path]] = []
    for path in folder.iterdir():
        if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        match = NUMBERED_RE.match(path.name)
        if not match:
            continue
        items.append((int(match.group(1)), path))
    items.sort(key=lambda item: (item[0], item[1].name.lower()))
    return items


def find_merged(folder: Path) -> Path | None:
    """Самый свежий merged*, если он есть в папке."""
    candidates = [
        p
        for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower() in VIDEO_SUFFIXES
        and p.name.lower().startswith("merged")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def fmt_ts(seconds: float, with_hours: bool) -> str:
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if with_hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def fmt_len(seconds: float) -> str:
    return fmt_ts(seconds, seconds >= 3600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Пишет таймкоды серий для склеенного видео (только чтение).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "folder",
        nargs="?",
        default=str(DEFAULT_FOLDER),
        help="папка с файлами 1_subtitled.mp4, 2_subtitled.mp4, ...",
    )
    parser.add_argument(
        "-o", "--output",
        help="куда писать txt (по умолчанию timestamps.txt в папке)",
    )
    parser.add_argument(
        "--merged",
        help="склеенный файл: имя txt будет <имя>_timestamps.txt, длительность сверится",
    )
    parser.add_argument(
        "--ffprobe",
        help="путь к ffprobe, если его нет в PATH",
    )
    args = parser.parse_args(argv)

    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        print(f"Папка не найдена: {folder}", file=sys.stderr)
        return 2

    # Номера: 1 — дисклеймер, 2 — первая серия, 3 — вторая...
    numbered = numbered_videos(folder)
    if len(numbered) < 2:
        print(
            f"В папке {folder} нужно минимум два видео с числовым номером "
            f"(1 — дисклеймер, 2 — первая серия). Найдено: {len(numbered)}",
            file=sys.stderr,
        )
        return 2

    numbers = [number for number, _ in numbered]
    missing = [n for n in range(numbers[0], numbers[-1] + 1) if n not in numbers]
    if missing:
        print(
            "Внимание: пропущены номера "
            + ", ".join(map(str, missing))
            + " — порядок взят по имеющимся файлам.",
            file=sys.stderr,
        )

    ffprobe = find_ffprobe(args.ffprobe)

    merged = Path(args.merged).expanduser() if args.merged else find_merged(folder)
    if merged is not None and not merged.exists():
        print(f"Внимание: merged-файл не найден: {merged}", file=sys.stderr)
        merged = None

    print(f"Папка: {folder}")
    print(f"Файлов: {len(numbered)}")
    print()

    measured = durations_for(
        ffprobe,
        [path for _, path in numbered] + ([merged] if merged is not None else []),
    )
    durations = measured[: len(numbered)]
    merged_duration = measured[-1] if merged is not None else None

    for index, ((_number, path), seconds) in enumerate(zip(numbered, durations), start=1):
        role = " (дисклеймер)" if index == 1 else ""
        print(f"  {index:>2}. {path.name:<32} {fmt_len(seconds):>8}{role}")

    # Первая серия = дисклеймер + первая серия.
    segments = [durations[0] + durations[1]]
    segments += durations[2:]
    total = sum(segments)

    with_hours = total >= 3600
    lines = []
    start = 0.0
    for episode, length in enumerate(segments, start=1):
        lines.append(f"{fmt_ts(start, with_hours)} - {episode} серия")
        start += length

    text = "\n".join(lines) + "\n"

    if args.output:
        output = Path(args.output).expanduser()
    elif merged is not None:
        output = folder / f"{merged.stem}_timestamps.txt"
    else:
        output = folder / "timestamps.txt"

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8-sig")

    print()
    print("Таймкоды:")
    print(text.rstrip())
    print()
    print(f"Итого: {fmt_len(total)} ({len(segments)} серий)")

    if merged is not None:
        merged_seconds = merged_duration if merged_duration is not None else 0.0
        diff = abs(merged_seconds - total)
        verdict = "совпадает" if diff <= 1.0 else "РАСХОЖДЕНИЕ"
        print(
            f"Проверка по {merged.name}: {fmt_len(merged_seconds)} "
            f"({verdict}, разница {diff:.1f} с)"
        )
        if diff > 1.0:
            print(
                "  Проверьте, что в папке лежат части именно от этого merged.",
                file=sys.stderr,
            )

    print()
    print(f"Записано: {output}")
    return 0


if __name__ == "__main__":
    _setup_console()
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
