"""Импортируемая версия skachka/batch_dl.py для ВК-бота.

Оригинал (skachka/batch_dl.py) не изменяется. Здесь та же логика:
ссылка | серии | озвучка | плеер | качество, те же плееры, приоритеты,
повторное использование уже скачанных файлов и обход TLS на okcdn.ru.

Отличия:
  * ничего не выполняется при импорте (нет чтения tasks.txt);
  * прогресс идёт через callback `log`, а не в stdout;
  * папка скачивания — параметр `out_dir`;
  * возвращается результат: DownloadResult(files=[...], problems=[...],
    title=..., episodes=[...]) — название аниме и номера реально скачанных серий
    нужны, чтобы красиво назвать итоговый файл.
"""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlsplit

import requests

import anime_dl_core as ap
from anime_dl_core.sources import AnimeGo

from paths2 import ANIME_TOKEN_PATH

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}
PRIORITY = ("cvh", "aniboom", "kodik", "sibnet")  # порядок выбора, если плеер "-"
DEFAULT_QUALITY = 1080

VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".m4v", ".mov"}


class DownloadError(RuntimeError):
    pass


@dataclass
class DownloadResult:
    files: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    title: str = ""           # оригинальное название аниме (как его отдаёт источник)
    episodes: list = field(default_factory=list)  # номера реально скачанных серий


def safe(name: str) -> str:
    """Имя файла/папки, безопасное для Windows."""
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]', " ", name)).strip(" .")[:120]


def parse_episodes(text: str, available):
    t = (text or "").strip().lower()
    if t in ("", "-", "все", "all"):
        return list(available)
    if "-" in t:
        a, b = t.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(t)]


def is_list_mode(eps_text: str) -> bool:
    return (eps_text or "").strip().lower() in ("?", "list", "список")


def _combos(items) -> list:
    combos = {}
    for n, p, d, _ in items:
        combos.setdefault((p, d), []).append(n)
    return [
        f"{p} / {d} — серии {min(eps)}..{max(eps)}, {len(eps)} шт."
        for (p, d), eps in sorted(combos.items())
    ]


class _Ctx:
    """Контекст одного запуска скачивания: папка, лог, токен, найденные файлы."""

    def __init__(self, out_dir: Path, log: Callable[[str], None], token: str):
        self.out = Path(out_dir)
        self.log = log
        self.token = token
        self.result = DownloadResult()
        self._seen = set()

    def set_title(self, title: Optional[str]) -> None:
        """Название аниме: первое непустое значение побеждает."""
        if not self.result.title and title:
            self.result.title = str(title).strip()

    def problem(self, message: str) -> None:
        self.result.problems.append(message)
        self.log(message)

    def add(self, path: Path) -> None:
        key = str(Path(path).resolve()).lower()
        if key not in self._seen:
            self._seen.add(key)
            self.result.files.append(Path(path))


def _save(result, final: Path, max_q: int, ctx: _Ctx) -> Optional[Path]:
    """Скачивание через ffmpeg: готовые пропускаются, недокачанные лежат в .part."""
    final = Path(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists() and final.stat().st_size:
        ctx.log(f"    уже скачан: {final.name} — пропуск")
        return final

    stream = result.best(max_quality=max_q)
    part = final.with_suffix(".part")
    ctx.log(f"    {result.player} {stream.quality}p -> {final.name}")
    args = stream.ffmpeg_args(
        str(part), extra_args=["-y", "-loglevel", "error", "-stats", "-f", "mp4"]
    )
    # okcdn.ru: эта сборка ffmpeg не принимает сертификат CDN — отключаем проверку TLS
    cmd = []
    for a in args:
        if a == "-i":
            cmd += ["-tls_verify", "0"]
        cmd.append(a)
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        part.unlink(missing_ok=True)
        detail = (proc.stderr or "").strip().splitlines()
        raise DownloadError(detail[-1][:300] if detail else "ffmpeg завершился с ошибкой")
    part.replace(final)  # переименовываем только после успеха
    return final


def _fetch(embed, title, ep, dub, max_q, ctx: _Ctx) -> None:
    base = safe(f"{title} - {ep:02d} [{dub}]")
    final = ctx.out / safe(title) / (base + ".mp4")
    q = dict(parse_qsl(urlsplit(embed).query))
    if q.get("anime_id"):
        # CVH на YummyAnime: iframe-обёртка iframeCVH.html?anime_id=...
        with ap.CvhPlayer(
            pub="745", aggr="mali", headers={"Origin": "https://ru.yummyani.me"}
        ) as cvh:
            cvh.default_referer = embed
            result = cvh.extract(q["anime_id"], episode=ep, studio=dub)
    else:
        result = ap.extract(embed)
    path = _save(result, final, max_q, ctx)
    if path is not None:
        ctx.set_title(title)
        ctx.add(path)


def _yummy_data(url, ctx: _Ctx):
    """(title, items) из официального API; items: (серия, плеер, озвучка, embed)."""
    if not ctx.token:
        ctx.problem("    нужен токен в skachka/token.txt")
        return None
    slug = re.split(r"[#?]", url.rstrip("/"))[0].rsplit("/", 1)[-1]
    h = {**UA, "X-Application": ctx.token, "Lang": "ru", "Accept": "application/json"}
    r = requests.get(f"https://api.yani.tv/anime/{slug}", headers=h, timeout=30)
    if r.status_code == 404:
        ctx.problem(f"    '{slug}' не найдено в официальном API YummyAnime")
        ctx.log("    возьмите ссылку на это аниме с old.yummyani.me или ru.yummyani.me")
        return None
    r.raise_for_status()
    anime = r.json()["response"]
    title = anime["title"]
    videos = requests.get(
        f"https://api.yani.tv/anime/{anime['anime_id']}/videos", headers=h, timeout=30
    ).json()["response"]
    if isinstance(videos, dict):
        videos = videos.get("videos", [])
    items = []
    for v in videos:
        try:
            num = int(str(v["number"]).split(".")[0])
        except (KeyError, ValueError):
            continue
        embed = v.get("iframe_url") or ""
        if embed.startswith("//"):
            embed = "https:" + embed
        data = v.get("data") or {}
        p = re.sub(r"^плеер\s+", "", (data.get("player") or "?").strip(), flags=re.I)
        d = re.sub(
            r"^(озвучка|субтитры)\s+", "", (data.get("dubbing") or "?").strip(), flags=re.I
        )
        items.append((num, p, d, embed))
    return title, items


def _run_yummy(url, eps_text, dub, player, max_q, ctx: _Ctx) -> None:
    data = _yummy_data(url, ctx)
    if not data:
        return
    title, items = data
    if not items:
        ctx.problem("    видео не найдены")
        return
    available = sorted({n for n, _, _, _ in items})
    ctx.log(f"    «{title}», серии {available[0]}..{available[-1]}")
    if is_list_mode(eps_text):
        for line in _combos(items):
            ctx.log(f"    {line}")
        ctx.log("    для скачивания подходят CVH и Kodik; Alloha библиотека не извлекает")
        return
    for ep in parse_episodes(eps_text, available):
        cand = [
            it
            for it in items
            if it[0] == ep
            and (player == "-" or it[1].lower() == player.lower())
            and (dub == "-" or dub.lower() in it[2].lower())
        ]
        if not cand:
            opts = "; ".join(sorted({f"{p}/{d}" for n, p, d, _ in items if n == ep}))
            ctx.problem(
                f"    [ep {ep:02d}] нет '{player}/{dub}'. "
                f"Есть: {opts or 'серия ещё не вышла'}"
            )
            continue
        cand.sort(
            key=lambda it: PRIORITY.index(it[1].lower()) if it[1].lower() in PRIORITY else 99
        )
        _, p, d, embed = cand[0]
        ctx.log(f"    [ep {ep:02d}] {p} / {d}")
        try:
            _fetch(embed, title, ep, d, max_q, ctx)
        except ap.UnsupportedUrl:
            ctx.problem(
                f"    [ep {ep:02d}] плеер не поддерживается (например, Alloha) — "
                "выберите CVH или Kodik"
            )
        except (ap.AnimeDlCoreError, DownloadError, subprocess.CalledProcessError) as e:
            ctx.problem(f"    [ep {ep:02d}] ошибка: {e}")


def _animego_title(url) -> Optional[str]:
    try:
        m = re.search(
            r"<h1[^>]*>(.*?)</h1>", requests.get(url, headers=UA, timeout=20).text, re.S
        )
        if m:
            return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    except requests.RequestException:
        pass
    return None


def _run_animego(url, eps_text, dub, player, max_q, ctx: _Ctx) -> None:
    url = re.split(r"[#?]", url)[0]
    mirror = re.match(r"https?://([^/]+)", url).group(1)
    with AnimeGo(mirror=mirror) as site:
        anime_id = AnimeGo.anime_id(url)
        available = sorted(site.episodes(anime_id))
        if not available:
            ctx.problem("    серии не найдены")
            return
        title = _animego_title(url) or f"anime_{anime_id}"
        ctx.log(f"    «{title}», серии {available[0]}..{available[-1]}")
        if is_list_mode(eps_text):
            links = site.players(anime_id, episode=available[0])
            ctx.log("    плееры/озвучки (на примере первой серии):")
            for p, d in sorted({(l.player, l.label) for l in links}):
                ctx.log(f"    {p} / {d}")
            return
        for ep in parse_episodes(eps_text, available):
            if ep not in available:
                ctx.problem(f"    [ep {ep:02d}] нет на сайте")
                continue
            links = site.players(anime_id, episode=ep)
            cand = [
                l
                for l in links
                if (player == "-" or l.player.lower() == player.lower())
                and (dub == "-" or dub.lower() in l.label.lower())
            ]
            if not cand:
                opts = "; ".join(sorted({f"{l.player}/{l.label}" for l in links}))
                ctx.problem(f"    [ep {ep:02d}] нет '{player}/{dub}'. Есть: {opts}")
                continue
            cand.sort(
                key=lambda l: PRIORITY.index(l.player.lower())
                if l.player.lower() in PRIORITY
                else 99
            )
            l = cand[0]
            ctx.log(f"    [ep {ep:02d}] {l.player} / {l.label}")
            try:
                _fetch(l.embed, title, ep, l.label, max_q, ctx)
            except ap.UnsupportedUrl:
                ctx.problem(f"    [ep {ep:02d}] плеер не поддерживается")
            except (ap.AnimeDlCoreError, DownloadError, subprocess.CalledProcessError) as e:
                ctx.problem(f"    [ep {ep:02d}] ошибка: {e}")


def _run_cvh(cvh_id, eps_text, dub, max_q, ctx: _Ctx) -> None:
    with ap.CvhPlayer() as p:
        playlist = p.playlist(cvh_id)
        available = sorted({e.episode for e in playlist})
        if not available:
            ctx.problem("    серии не найдены")
            return
        title = f"CVH {cvh_id}"
        ctx.set_title(title)
        if is_list_mode(eps_text):
            ctx.log(f"    {title}, серии {available[0]}..{available[-1]}")
            combos = {}
            for e in playlist:
                combos.setdefault(e.studio or "?", []).append(e.episode)
            for d, eps in sorted(combos.items()):
                ctx.log(f"    CVH / {d} — серии {min(eps)}..{max(eps)}, {len(eps)} шт.")
            return
        for ep in parse_episodes(eps_text, available):
            match = [
                e
                for e in playlist
                if e.episode == ep and (dub == "-" or dub.lower() in (e.studio or "").lower())
            ]
            if not match:
                ctx.problem(f"    [ep {ep:02d}] озвучка '{dub}' не найдена")
                continue
            e = match[0]
            try:
                res = p.extract(
                    cvh_id, season=e.season, episode=e.episode, studio=e.studio
                )
                base = safe(f"{title} - {ep:02d} [{e.studio}]")
                path = _save(res, ctx.out / safe(title) / (base + ".mp4"), max_q, ctx)
                if path is not None:
                    ctx.set_title(title)
                    ctx.add(path)
            except (ap.AnimeDlCoreError, DownloadError, subprocess.CalledProcessError) as err:
                ctx.problem(f"    [ep {ep:02d}] ошибка: {err}")


def _dispatch(url, eps_text, dub, player, max_q, ctx: _Ctx) -> None:
    if re.search(r"y[au]mmy", url, re.I):
        _run_yummy(url, eps_text, dub, player, max_q, ctx)
    elif re.search(r"animego\.[^/]+/anime/", url):
        _run_animego(url, eps_text, dub, player, max_q, ctx)
    elif url.lower().startswith("cvh:"):
        _run_cvh(url.split(":", 1)[1].strip(), eps_text, dub, max_q, ctx)
    else:  # прямая embed-ссылка = одно видео
        res = ap.extract(url)
        name = safe(res.title or "video")
        path = _save(res, ctx.out / name / f"{name}.mp4", max_q, ctx)
        if path is not None:
            ctx.set_title(res.title)
            ctx.add(path)


def _episode_of(path: Path) -> int:
    m = re.search(r"\s-\s(\d+)\s*\[", path.stem)
    return int(m.group(1)) if m else 10 ** 9


def _read_token(token_path: Path) -> str:
    p = Path(token_path)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8-sig").strip()


def download_series(
    url: str,
    episodes: str = "1",
    dub: str = "-",
    player: str = "-",
    quality=1080,
    out_dir: Path = None,
    token_path: Path = ANIME_TOKEN_PATH,
    log: Callable[[str], None] = print,
) -> DownloadResult:
    """Скачивает серии по параметрам как в tasks.txt у batch_dl.

    Возвращает DownloadResult: files (по возрастанию номера серии) и problems.
    """
    out_dir = Path(out_dir) if out_dir is not None else Path.cwd() / "downloads"
    out_dir.mkdir(parents=True, exist_ok=True)
    eps_text = (str(episodes) if episodes is not None else "1").strip() or "1"
    dub = (str(dub) if dub is not None else "-").strip() or "-"
    player = (str(player) if player is not None else "-").strip() or "-"
    quality_text = str(quality).strip() if quality is not None else ""
    max_q = int(quality_text) if quality_text.isdigit() else DEFAULT_QUALITY

    ctx = _Ctx(out_dir, log, _read_token(token_path))
    ctx.log(f"    источник: {url}")
    ctx.log(f"    серии: {eps_text} | озвучка: {dub} | плеер: {player} | до {max_q}p")

    _dispatch(url, eps_text, dub, player, max_q, ctx)

    # На всякий случай добираем уже лежащие на диске файлы (докачка после сбоя).
    for path in out_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES:
            ctx.add(path)

    ctx.result.files.sort(key=lambda p: (_episode_of(p), p.name.lower()))
    # Номера серий — из имён файлов («Название - 07 [озвучка].mp4»).
    episodes = sorted({_episode_of(p) for p in ctx.result.files} - {10 ** 9})
    ctx.result.episodes = episodes
    return ctx.result
