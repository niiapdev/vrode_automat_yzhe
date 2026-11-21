"""Таймкоды серий для bot2: замер сразу после скачивания, журнал, сообщение в ВК.

Логика та же, что в корневом timestamps.py: файл «1» — дисклеймер, «2» —
первая серия. Поэтому первая серия = дисклеймер + первая скачанная серия.

Отличия, подогнанные под бота:
  * подписи — реальные номера серий (не сквозные 1..N);
  * замер идёт сразу после скачивания, до перемещения/склейки/уборки;
  * каждая серия дописывается в журнал (timings.jsonl) с flush — сбой на
    любом следующем этапе не теряет уже посчитанное;
  * одна попытка ffprobe на файл, таймаут 5 с, общий бюджет замера 20 с;
  * любая ошибка возвращает None/помечает серию, задачу не роняет.

CLI для восстановления:
    python bot2\\timestamps2.py <папка задачи>      # из журнала timings.jsonl
    python bot2\\timestamps2.py --scan <папка>      # пересчитать по видеофайлам
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Optional

JOURNAL_NAME = "timings.jsonl"
REPORT_NAME = "timings.txt"

FFPROBE_TIMEOUT = 5.0   # на один файл: обычный ответ ~30 мс
TOTAL_BUDGET = 20.0     # предохранитель на весь замер
MAX_WORKERS = 8

VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".m4v", ".mov", ".avi", ".ts", ".flv"}
NUMBERED_RE = re.compile(r"^(\d+)(?=[_\-\s.]|$)")


def find_ffprobe() -> Optional[str]:
    """ffprobe из PATH или рядом с ffmpeg. Нет — вернёт None (не ошибка)."""
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
    return None


def duration_seconds(path: Path, ffprobe: Optional[str] = None,
                     timeout: float = FFPROBE_TIMEOUT) -> Optional[float]:
    """Длительность из заголовка контейнера. Одна попытка, без декодирования."""
    ffprobe = ffprobe or find_ffprobe()
    if not ffprobe:
        return None
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
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if raw.returncode != 0:
        return None
    try:
        data = json.loads(raw.stdout or "{}")
    except json.JSONDecodeError:
        return None

    value = (data.get("format") or {}).get("duration")
    if value not in (None, "N/A"):
        try:
            seconds = float(value)
            if seconds > 0:
                return seconds
        except ValueError:
            pass

    durations = []
    for stream in data.get("streams") or []:
        try:
            durations.append(float(stream.get("duration") or 0))
        except ValueError:
            continue
    return max(durations) if durations and max(durations) > 0 else None


def fmt_ts(seconds: float, with_hours: bool) -> str:
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if with_hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def fmt_len(seconds: float) -> str:
    return fmt_ts(seconds, seconds >= 3600)


def _numbered_files(folder: Path) -> list[tuple[int, Path]]:
    items = []
    for path in sorted(Path(folder).iterdir()):
        if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        match = NUMBERED_RE.match(path.name)
        if match:
            items.append((int(match.group(1)), path))
    items.sort(key=lambda item: (item[0], item[1].name.lower()))
    return items


class Timeline:
    """Журнал замеров: дисклеймер + серии, с немедленной записью на диск."""

    def __init__(self, journal: Path, disclaimer: Optional[float] = None,
                 entries: Optional[list] = None) -> None:
        self.journal = Path(journal)
        self.disclaimer = disclaimer
        self.entries: list[dict] = entries or []

    # ---------- замер ----------

    @classmethod
    def measure(
        cls,
        journal: Path,
        disclaimer_video: Path,
        files: Iterable[Path],
        episodes: Optional[Iterable[int]] = None,
        log=None,
    ) -> "Timeline":
        """Замер всех скачанных файлов. Никогда не бросает исключение наружу."""
        journal = Path(journal)
        journal.parent.mkdir(parents=True, exist_ok=True)
        files = [Path(f) for f in files]

        ffprobe = find_ffprobe()
        disclaimer = duration_seconds(disclaimer_video, ffprobe) if ffprobe else None

        timeline = cls(journal, disclaimer)
        timeline._write_header()

        if not ffprobe:
            if log:
                log("таймкоды: ffprobe не найден, длительности не считаются")
            return timeline

        numbers = list(episodes or [])
        if len(numbers) != len(files):
            numbers = list(range(1, len(files) + 1))

        started = time.monotonic()
        pool = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(files))))
        try:
            futures = {
                pool.submit(duration_seconds, path, ffprobe): (index, number, path)
                for index, (number, path) in enumerate(zip(numbers, files))
            }
            for future in as_completed(futures, timeout=TOTAL_BUDGET):
                index, number, path = futures[future]
                try:
                    seconds = future.result()
                except Exception:  # pragma: no cover - защита от неожиданностей
                    seconds = None
                timeline._append(index, number, path, seconds)
        except Exception as exc:  # TimeoutError и всё остальное
            if log:
                log(f"таймкоды: замер прерван ({exc.__class__.__name__}), "
                    f"посчитано {len(timeline.entries)} из {len(files)}")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        # Кто не успел/не смог — попадает в журнал как «нет данных».
        measured = {entry["index"] for entry in timeline.entries}
        for index, (number, path) in enumerate(zip(numbers, files)):
            if index not in measured:
                timeline._append(index, number, path, None)

        # Журнал append-only и пишется по мере готовности — для расчёта
        # возвращаем строгий порядок серий.
        timeline.entries.sort(key=lambda entry: entry["index"])

        elapsed = time.monotonic() - started
        if log:
            log(f"таймкоды: замер {len(files)} файлов за {elapsed:.2f} с")
        return timeline

    def _write_header(self) -> None:
        header = {"disclaimer": self.disclaimer}
        self.journal.write_text(
            json.dumps(header, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def _append(self, index: int, number: int, path: Path,
                seconds: Optional[float]) -> None:
        entry = {
            "index": int(index),
            "episode": None if number is None else int(number),
            "file": Path(path).name,
            "duration": None if seconds is None else round(float(seconds), 3),
        }
        self.entries.append(entry)
        with self.journal.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()

    def retry_missing(self, stage_dir: Path) -> None:
        """Добор по stage/N_subtitled.mp4 для тех, кто не прочитался."""
        folder = Path(stage_dir)
        if not folder.is_dir() or not self.entries:
            return
        ffprobe = find_ffprobe()
        if not ffprobe:
            return
        changed = False
        for index, entry in enumerate(self.entries):
            if entry.get("duration") is not None:
                continue
            position = entry.get("index", index)
            staged = folder / f"{position + 2}_subtitled.mp4"
            if not staged.is_file():
                continue
            seconds = duration_seconds(staged, ffprobe)
            if seconds is not None:
                entry["duration"] = round(float(seconds), 3)
                changed = True
        if changed:
            self._rewrite()

    def _rewrite(self) -> None:
        with self.journal.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"disclaimer": self.disclaimer},
                                ensure_ascii=False) + "\n")
            for entry in self.entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()

    @classmethod
    def from_parts(cls, disclaimer: Optional[float],
                   pairs: Iterable[tuple[int, Path, Optional[float]]]) -> "Timeline":
        """Готовая лента без журнала (для --scan и самопроверки)."""
        timeline = cls(Path(os.devnull), disclaimer)
        timeline.entries = [
            {
                "index": position,
                "episode": int(number),
                "file": Path(path).name,
                "duration": None if seconds is None else round(float(seconds), 3),
            }
            for position, (number, path, seconds) in enumerate(pairs)
        ]
        return timeline

    # ---------- результат ----------

    @property
    def total(self) -> float:
        base = self.disclaimer or 0.0
        return base + sum(
            entry.get("duration") or 0.0 for entry in self.entries
        )

    def starts(self) -> list[tuple[Optional[float], Optional[int]]]:
        """(начало серии, номер). Первая серия — с нуля, дисклеймер идёт перед ней."""
        result: list[tuple[Optional[float], Optional[int]]] = []
        position = 0.0
        broken = False
        for index, entry in enumerate(self.entries):
            number = entry.get("episode")
            result.append((None if broken else position, number))
            seconds = entry.get("duration")
            if seconds is None:
                broken = True
                continue
            position += seconds
            if index == 0:
                position += self.disclaimer or 0.0
        return result

    def lines(self) -> list[str]:
        total = self.total
        with_hours = total >= 3600
        lines = []
        for start, number in self.starts():
            label = f"{number} серия" if number is not None else "серия"
            if start is None:
                lines.append(f"--:-- - {label} (нет данных)")
            else:
                lines.append(f"{fmt_ts(start, with_hours)} - {label}")
        return lines

    def message(self) -> Optional[str]:
        lines = self.lines()
        if not lines:
            return None
        return "⏱ Таймкоды\n\n" + "\n".join(lines)

    def report(self, merged_duration: Optional[float] = None) -> str:
        parts = []
        for start, number in self.starts():
            label = f"{number} серия" if number is not None else "серия"
            when = "--:--:--" if start is None else fmt_len(start)
            parts.append(f"{when} - {label}")
        total = self.total
        text = ["Таймкоды", ""]
        text += parts
        text += ["", f"Итого: {fmt_len(total)} ({len(self.entries)} серий)"]
        if self.disclaimer is not None:
            text.append(f"Дисклеймер: {fmt_len(self.disclaimer)}")
        if merged_duration:
            diff = abs(merged_duration - total)
            verdict = "совпадает" if diff <= 1.0 else "РАСХОЖДЕНИЕ"
            text.append(
                f"Склейка: {fmt_len(merged_duration)} ({verdict}, разница {diff:.1f} с)"
            )
        missing = [str(e.get("episode")) for e in self.entries
                   if e.get("duration") is None]
        if missing:
            text.append("Не определены: серии " + ", ".join(missing))
        return "\n".join(text) + "\n"

    def write_report(self, path: Path, merged_duration: Optional[float] = None) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.report(merged_duration), encoding="utf-8-sig")
        return path

    # ---------- восстановление ----------

    @classmethod
    def load(cls, journal: Path) -> "Timeline":
        journal = Path(journal)
        disclaimer = None
        entries: list[dict] = []
        if journal.is_file():
            for raw in journal.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "disclaimer" in item:
                    disclaimer = item.get("disclaimer")
                elif item.get("file"):
                    entries.append(item)
        entries.sort(key=lambda entry: entry.get("index", 0))
        return cls(journal, disclaimer, entries)


def scan_stage(stage_dir: Path, log=None) -> Optional[str]:
    """Пересчёт прямо по stage/N_subtitled.mp4 (1 — дисклеймер, 2 — серия 1)."""
    folder = Path(stage_dir)
    numbered = _numbered_files(folder)
    if len(numbered) < 2:
        return None
    ffprobe = find_ffprobe()
    if not ffprobe:
        return None
    disclaimer = duration_seconds(numbered[0][1], ffprobe)
    pairs = [
        (number - 1, path, duration_seconds(path, ffprobe))
        for number, path in numbered[1:]
    ]
    timeline = Timeline.from_parts(disclaimer, pairs)
    if log:
        log(f"таймкоды: пересчитано по {len(numbered)} файлам stage")
    return timeline.message()


def _cli(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    scanner = bool(argv) and argv[0] == "--scan"
    folder = Path(argv[1] if scanner and len(argv) > 1 else
                  (argv[0] if argv and not scanner else "."))
    if not folder.is_dir():
        print(f"Папка не найдена: {folder}", file=sys.stderr)
        return 2

    if scanner:
        message = scan_stage(folder, log=print)
        if not message:
            print("Не удалось посчитать (нужно минимум 2 видео и ffprobe)",
                  file=sys.stderr)
            return 2
        print(message)
        return 0

    journal = folder / JOURNAL_NAME
    timeline = Timeline.load(journal)
    if not timeline.entries:
        print(f"Нет журнала {journal}. Попробуйте: --scan {folder}", file=sys.stderr)
        return 2
    print(timeline.message() or "Нет данных")
    print()
    print(timeline.report())
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
