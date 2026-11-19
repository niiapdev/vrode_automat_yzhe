# batch_dl.py — пакетное скачивание аниме

`batch_dl.py` читает `tasks.txt`, находит серии на YummyAnime / AnimeGO / CVH / прямых
embed-ссылках и скачивает их через `ffmpeg`. Скрипт можно запускать из любого каталога:
все пути по умолчанию считаются от самого скрипта, а не от текущего каталога.

## Файлы

| Файл | Назначение |
|---|---|
| `skachka/batch_dl.py` | основной скрипт |
| `skachka/tasks.txt` | список задач |
| `skachka/token.txt` | токен YummyAnime (`X-Application`) |
| `skachka/requirements-batch_dl.txt` | Python-зависимости только для `batch_dl` |
| `skachka/setup_deps.ps1` | установщик для Windows |
| `skachka/setup_deps.sh` | установщик для Linux/macOS |
| `skachka/run_batch_dl.ps1` | запуск через `.venv` + автоустановка (Windows) |
| `skachka/run_batch_dl.sh` | запуск через `.venv` + автоустановка (Linux/macOS) |
| `skachka/downloads/` | видео и `manifest.jsonl` по умолчанию |
| `skachka/logs/` | `batch_dl.log`, `setup_deps.log` |

## Установка

Windows (PowerShell):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\skachka\setup_deps.ps1
```

Linux/macOS:

```bash
bash skachka/setup_deps.sh
```

Проверка без изменений:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\skachka\setup_deps.ps1 -CheckOnly
```

```bash
bash skachka/setup_deps.sh --check-only
```

Установщик ставит Python 3.9+, `.venv`, `requests`, `anime-dl-core`, `yt-dlp`,
`ffmpeg`/`ffprobe`, при необходимости VC++ Redistributable (Windows) и системные
пакеты (Linux). Повторный запуск безопасен. Для включения длинных путей Windows
(>260 символов) и установки VC++ запустите PowerShell от имени администратора;
без прав администратора установщик выведет предупреждение, а остальное установит.

## Формат tasks.txt

```text
# ссылка | серии | озвучка | плеер | качество
https://yummyani.org/anime/example | все | AnimeVost | CVH | 1080
https://animego.org/anime/example | 1-12 | AniLibria | Кодик | 720
cvh::https://example.com/embed/... | 1 | TVShows | CVH | 1080
```

Поля:

- ссылка — страница аниме, `cvh::...`, `aniboom::...` или прямой embed URL;
- серии — `все`, `1-12`, `1,3,5`, `7`, `1-12,15`;
- озвучка — подстрока названия студии, пустое поле = любая;
- плеер — `CVH`, `AniBoom`, `Kodik`, `Sibnet`, пустое = приоритет автоматически;
- качество — максимальное (`1080`, `720`, `480`); если нет — берётся лучшее доступное.

Токен YummyAnime создаётся на <https://site.yummyani.me/dev/applications> и
кладётся в `skachka/token.txt` (только токен, без кавычек).

## Запуск

Из корня проекта:

```powershell
python .\skachka\batch_dl.py --check
python .\skachka\batch_dl.py --dry-run --task 1 --episodes 1
python .\skachka\batch_dl.py
```

Из каталога `skachka`:

```powershell
python .\batch_dl.py
```

Через обёртку (сама создаст/проверит `.venv`):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\skachka\run_batch_dl.ps1 --check
```

```bash
bash skachka/run_batch_dl.sh --check
```

Полезные параметры:

```text
--task N              номер задачи (1-based), можно повторять
--limit-tasks N       первые N задач
--episodes 1-12       переопределить серии
--limit-episodes N    не более N серий на задачу
--quality 720         переопределить качество
--out PATH            каталог загрузок (по умолчанию skachka/downloads)
--manifest PATH       путь к manifest.jsonl
--overwrite           перекачать существующие файлы
--dry-run             только найти ссылки, без скачивания
--stop-on-error       остановиться на первой ошибке
--check               проверить окружение
--self-test           офлайн-самопроверка (29 проверок)
--init                создать шаблон tasks.txt
```

По умолчанию файлы лежат так:

```text
skachka/downloads/<название>/<название> - 01 [AnimeVost].mp4
```

## Manifest и пайплайн субтитров

Каждый успешный файл дописывается в `skachka/downloads/manifest.jsonl`:

```json
{"path": "D:\\for-move\\skachka\\downloads\\...\\...mp4", "relative_path": "...", "size": 123456, "duration": 1440.0, "quality": 720, "player": "CVH", "title": "...", "episode": 1, "dub": "AnimeVost", "saved_at": "..."}
```

Скачать сразу в рабочий каталог Whisper:

```powershell
python .\skachka\batch_dl.py --out D:\for-move\video_for_whisper
```

Затем обработать всё из manifest:

```python
import asyncio
import json
from pathlib import Path

from merger import merge_video_subtitles
from translator import transcribe_video


async def main() -> None:
    manifest = Path(r"D:\for-move\skachka\downloads\manifest.jsonl")
    done: set[Path] = set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        video = Path(record["path"])
        if not video.is_file() or video in done:
            continue
        done.add(video)
        srt = await transcribe_video(video, source_lang="en", translate_to_ru=False)
        final = await merge_video_subtitles(video, srt, translate_to_ru=False)
        print(final)


asyncio.run(main())
```

`merge_video_subtitles` кладёт результат в `config.OUTPUT_DIR` (`video_subtitled`),
`transcribe_video` — `.srt` рядом с видео. Если в `config.py` включён
`BURN_SUBTITLES`, субтитры вжигаются (`libx264`), иначе мультиплексируются как
`mov_text`.

## Если что-то не так

- `--check` печатает найденные Python, `.venv`, ffmpeg, ffprobe, tasks/token и
  доступность каталога загрузок.
- `--self-test` проверяет парсинг задач, имена файлов, manifest, retry и FFmpeg
  на локальном тестовом файле, не обращаясь к сети.
- Лог: `skachka/logs/batch_dl.log`. Секреты (токен) в лог не пишутся.
- При обрыве сети скачивание повторяется, `.part` удаляется, готовый файл
  проверяется через `ffprobe`.
