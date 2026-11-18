import asyncio
import logging
import os
import sys
import pathlib
from pathlib import Path
from typing import Awaitable, Callable, Optional

# На Windows DLL-библиотеки CUDA из pip-пакетов nvidia-* лежат не в PATH.
# Держим handles живыми: при сборке мусора os.add_dll_directory отменяет каталог.
_DLL_DIRECTORY_HANDLES = []
if os.name == "nt":
    _site_packages = pathlib.Path(sys.prefix) / "Lib" / "site-packages"
    _cuda_dirs = [
        _site_packages / "nvidia" / "cublas" / "bin",
        _site_packages / "nvidia" / "cuda_runtime" / "bin",
        _site_packages / "nvidia" / "cuda_nvrtc" / "bin",
        _site_packages / "nvidia" / "cudnn" / "bin",
    ]
    _existing_cuda_dirs = [d for d in _cuda_dirs if d.is_dir()]
    for _dir in _existing_cuda_dirs:
        _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(_dir)))
    if _existing_cuda_dirs:
        os.environ["PATH"] = os.pathsep.join(
            [str(d) for d in _existing_cuda_dirs] + [os.environ.get("PATH", "")]
        )

from faster_whisper import WhisperModel
import config

logger = logging.getLogger("translator")

_model: Optional[WhisperModel] = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        logger.info(
            "Загрузка Whisper: model=%s, device=%s, compute_type=%s",
            config.WHISPER_MODEL,
            config.WHISPER_DEVICE,
            config.WHISPER_COMPUTE_TYPE,
        )
        _model = WhisperModel(
            config.WHISPER_MODEL,
            device=config.WHISPER_DEVICE,
            compute_type=config.WHISPER_COMPUTE_TYPE,
        )
    return _model


def _format_ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, rem = divmod(rem, 60)
    secs, millis = divmod(rem, 1)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(secs):02d},{int(millis * 1000):03d}"


def _format_srt(blocks) -> str:
    lines = []
    for idx, (start, end, text) in enumerate(blocks, start=1):
        lines.append(str(idx))
        lines.append(f"{_format_ts(start)} --> {_format_ts(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


async def transcribe_video(
    video_path: Path,
    source_lang: str = "en",
    progress_coro: Optional[Callable[[str], Awaitable[None]]] = None,
    translate_to_ru: bool = False,
    translate_to_en: bool = False,
) -> Path:
    if progress_coro is not None:
        await progress_coro("⏳ Распознавание речи...")
    if translate_to_ru:
        language = "ru"
        task = "transcribe"
    elif translate_to_en:
        language = None
        task = "translate"
    else:
        language = source_lang or None
        task = "transcribe"
    srt_path = video_path.with_suffix(".srt")
    shift = float(config.SUBTITLE_SHIFT_SECONDS or 0.0)

    def _run() -> Path:
        model = _get_model()
        segments, _info = model.transcribe(
            str(video_path),
            language=language,
            task=task,
            word_timestamps=bool(config.WHISPER_WORD_TIMESTAMPS),
            vad_filter=bool(config.WHISPER_VAD),
        )
        blocks = []
        for segment in segments:
            text = (segment.text or "").strip()
            if not text:
                continue
            blocks.append((segment.start + shift, segment.end + shift, text))
        srt_path.write_text(_format_srt(blocks), encoding="utf-8")
        return srt_path

    return await asyncio.to_thread(_run)