"""Клон tik_tok-scripts/main.py как функции для ВК-бота.

Оригинал не меняется. Логика та же:
  4 случайных 10-сек фрагмента -> ускорение x2 -> склейка ->
  вертикаль 1080x1920, сверху 1.png, снизу 2.png.

Отличия: видео задаётся параметром (а не случайное из cwd), вместо sys.exit
поднимаются исключения, ошибки и прогресс идут через callback `log`.
"""

import json
import logging
import random
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger("tiktok2")


class TikTokError(RuntimeError):
    pass


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_video: bool
    has_audio: bool


@dataclass
class Segment:
    start: float
    end: float
    duration: float


def get_video_info(video_path: Path) -> VideoInfo:
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(video_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        raise TikTokError(f"ffprobe не смог прочитать видео: {e.stderr[-300:]}") from e
    except json.JSONDecodeError as e:
        raise TikTokError("Не удалось разобрать вывод ffprobe") from e

    format_info = data.get("format", {})
    duration = float(format_info.get("duration", 0))

    has_video = False
    has_audio = False
    width = 0
    height = 0
    fps = 30.0

    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            has_video = True
            width = stream.get("width", 0)
            height = stream.get("height", 0)
            r_frame_rate = stream.get("r_frame_rate", "30/1")
            try:
                num, den = map(int, r_frame_rate.split("/"))
                if den > 0:
                    fps = num / den
            except (ValueError, ZeroDivisionError):
                fps = 30.0
        elif stream.get("codec_type") == "audio":
            has_audio = True

    if not has_video:
        raise TikTokError("В исходном видео нет видеодорожки")

    return VideoInfo(
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        has_video=has_video,
        has_audio=has_audio,
    )


def select_random_segments(
    duration: float, segment_duration: float = 10.0, count: int = 4
) -> List[Segment]:
    if duration < segment_duration:
        raise TikTokError(
            f"Видео короче {segment_duration:.0f} секунд — тиктоки сделать нельзя"
        )

    max_start = duration - segment_duration

    if duration >= segment_duration * count:
        available_starts = []
        step = max_start / (count * 2)
        for i in range(count):
            zone_start = i * 2 * step
            zone_end = min(zone_start + 2 * step, max_start)
            if zone_start < zone_end:
                available_starts.append(random.uniform(zone_start, zone_end))
        while len(available_starts) < count:
            available_starts.append(random.uniform(0, max_start))
    else:
        available_starts = [random.uniform(0, max_start) for _ in range(count)]

    segments = []
    for start in available_starts:
        end = min(start + segment_duration, duration)
        segments.append(Segment(start=start, end=end, duration=end - start))
    return segments


def format_time(seconds: float) -> str:
    mins = int(seconds // 60)
    secs = seconds % 60
    return f"{mins:02d}:{secs:05.2f}"


def create_sped_up_segment(
    input_path: Path,
    output_path: Path,
    start: float,
    duration: float,
    speed: float = 2.0,
    has_audio: bool = True,
) -> bool:
    video_filter = f"setpts={1 / speed}*PTS"

    if has_audio:
        audio_filter = "atempo=2.0"
        filter_complex = f"[0:v]{video_filter}[v];[0:a]{audio_filter}[a]"
        map_args = ["-map", "[v]", "-map", "[a]"]
    else:
        filter_complex = f"[0:v]{video_filter}[v]"
        map_args = ["-map", "[v]"]

    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", str(start),
        "-t", str(duration),
        "-i", str(input_path),
        "-filter_complex", filter_complex,
        *map_args,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(output_path),
    ]

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error("Ошибка нарезки фрагмента: %s", (e.stderr or "")[-300:])
        return False


def concatenate_segments(segment_paths: List[Path], output_path: Path) -> bool:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        for seg_path in segment_paths:
            f.write(f"file '{seg_path.absolute()}'\n")
        concat_file = f.name

    try:
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-c", "copy",
            str(output_path),
        ]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error("Ошибка склейки фрагментов: %s", (e.stderr or "")[-300:])
        return False
    finally:
        try:
            Path(concat_file).unlink()
        except OSError:
            pass


def create_vertical_composition(
    video_path: Path,
    top_image_path: Path,
    bottom_image_path: Path,
    output_path: Path,
    video_info: VideoInfo,
    target_width: int = 1080,
    target_height: int = 1920,
) -> bool:
    video_aspect = video_info.width / video_info.height
    target_video_width = target_width
    target_video_height = int(target_width / video_aspect)

    if target_video_height > target_height * 0.6:
        target_video_height = int(target_height * 0.5)
        target_video_width = int(target_video_height * video_aspect)

    target_video_width = target_video_width if target_video_width % 2 == 0 else target_video_width - 1
    target_video_height = target_video_height if target_video_height % 2 == 0 else target_video_height - 1

    remaining_height = target_height - target_video_height
    top_image_height = remaining_height // 2
    # crop не умеет нечётные размеры в yuv420p — выравниваем, чтобы сумма была ровно target_height
    if top_image_height % 2:
        top_image_height -= 1
    bottom_image_height = remaining_height - top_image_height

    video_scale = (
        f"scale={target_video_width}:{target_video_height}:force_original_aspect_ratio=decrease,"
        f"pad={target_width}:{target_video_height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )
    top_scale = (
        f"scale={target_width}:{top_image_height}:force_original_aspect_ratio=increase,"
        f"crop={target_width}:{top_image_height}"
    )
    bottom_scale = (
        f"scale={target_width}:{bottom_image_height}:force_original_aspect_ratio=increase,"
        f"crop={target_width}:{bottom_image_height}"
    )

    filter_parts = [
        f"[0:v]{video_scale}[v_scaled]",
        f"[1:v]{top_scale}[top_scaled]",
        f"[2:v]{bottom_scale}[bottom_scaled]",
        f"[top_scaled][v_scaled][bottom_scaled]vstack=inputs=3[v_out]",
    ]

    filter_complex = ";".join(filter_parts)
    map_args = ["-map", "[v_out]", "-map", "0:a?"] if video_info.has_audio else ["-map", "[v_out]"]

    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", str(video_path),
        "-i", str(top_image_path),
        "-i", str(bottom_image_path),
        "-filter_complex", filter_complex,
        *map_args,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error("Ошибка вертикальной композиции: %s", (e.stderr or "")[-300:])
        return False


def make_clips(
    source_video: Path,
    top_image: Path,
    bottom_image: Path,
    out_dir: Path,
    clips: int = 4,
    segment_duration: float = 10.0,
    segments_per_clip: int = 4,
    speed: float = 2.0,
    log: Optional[Callable[[str], None]] = None,
) -> List[Path]:
    """Делает `clips` тиктоков (каждый — как один запуск tik_tok-scripts/main.py)."""
    say = log or (lambda _m: None)
    source_video = Path(source_video)
    out_dir = Path(out_dir)
    top_image = Path(top_image)
    bottom_image = Path(bottom_image)

    for path, label in ((source_video, "исходное видео"), (top_image, "1.png"), (bottom_image, "2.png")):
        if not path.is_file():
            raise TikTokError(f"Не найден файл: {label} ({path})")

    info = get_video_info(source_video)
    say(
        f"    исходник: {info.duration:.1f}с, {info.width}x{info.height}, "
        f"звук: {'да' if info.has_audio else 'нет'}"
    )
    if info.duration < segment_duration:
        raise TikTokError(
            f"Видео короче {segment_duration:.0f} секунд — тиктоки сделать нельзя"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    results: List[Path] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for clip_index in range(1, clips + 1):
            say(f"    тикток {clip_index}/{clips}: выбор фрагментов...")
            segments = select_random_segments(
                info.duration, segment_duration, segments_per_clip
            )
            for order, seg in enumerate(segments, 1):
                say(
                    f"      фрагмент {order}: "
                    f"{format_time(seg.start)} - {format_time(seg.end)}"
                )

            segment_paths = []
            for order, seg in enumerate(segments, 1):
                seg_out = tmp / f"clip{clip_index}_seg{order}.mp4"
                ok = create_sped_up_segment(
                    source_video,
                    seg_out,
                    seg.start,
                    seg.duration,
                    speed=speed,
                    has_audio=info.has_audio,
                )
                if not ok:
                    raise TikTokError(
                        f"Не удалось нарезать фрагмент {order} для тиктока {clip_index}"
                    )
                segment_paths.append(seg_out)

            concat_out = tmp / f"clip{clip_index}_concat.mp4"
            if not concatenate_segments(segment_paths, concat_out):
                raise TikTokError(f"Не удалось склеить фрагменты тиктока {clip_index}")

            output_path = out_dir / f"tiktok_{clip_index:02d}.mp4"
            if not create_vertical_composition(
                concat_out, top_image, bottom_image, output_path, info
            ):
                raise TikTokError(f"Не удалось собрать вертикальное видео {clip_index}")

            results.append(output_path)
            say(f"      готово: {output_path.name}")

    return results
