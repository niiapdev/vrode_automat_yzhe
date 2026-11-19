# batch_dl.py — пакетное скачивание аниме по задачам из tasks.txt
#
# Скачать:   python batch_dl.py
#   задачи в tasks.txt, формат строки:  ссылка | серии | озвучка | плеер | качество
#   серии: 1-12, 5 или все;  "-" = не важно;  качество по умолчанию 1080
#   ссылка | ?  — только показать доступные плееры и озвучки, ничего не качать
#
# Разведка:  python batch_dl.py <ссылка>
#   показывает доступные плееры/озвучки для ссылки и ничего не качает
#
# Источники: зеркала YummyAnime (yummyani.me, yummyani.org, yammyanime.club и т.п.)
# через официальный API (нужен token.txt), animego.*, cvh:<id>, прямые embed-ссылки.

import re
import sys
import subprocess
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import requests
import anime_dl_core as ap
from anime_dl_core.sources import AnimeGo

TASKS = "tasks.txt"
OUT = Path("downloads")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
PRIORITY = ("cvh", "aniboom", "kodik", "sibnet")   # порядок выбора, если плеер "-"

tf = Path("token.txt")
YUMMY_TOKEN = tf.read_text(encoding="utf-8-sig").strip() if tf.exists() else ""


def safe(name):
    """Имя файла/папки, безопасное для Windows."""
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]', " ", name)).strip(" .")[:120]


def parse_episodes(text, available):
    t = text.strip().lower()
    if t in ("", "-", "все", "all"):
        return available
    if "-" in t:
        a, b = t.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(t)]


def is_list_mode(eps_text):
    return eps_text.strip().lower() in ("?", "list", "список")


def print_combos(items, prefix="    "):
    combos = {}
    for n, p, d, _ in items:
        combos.setdefault((p, d), []).append(n)
    for (p, d), eps in sorted(combos.items()):
        print(f"{prefix}{p} / {d} — серии {min(eps)}..{max(eps)}, {len(eps)} шт.")


def save(result, final, max_q):
    """Скачивание через ffmpeg: готовые пропускаются, недокачанные лежат в .part."""
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists() and final.stat().st_size:
        print(f"    уже скачан: {final.name} — пропуск")
        return
    stream = result.best(max_quality=max_q)
    part = final.with_suffix(".part")
    print(f"    {result.player} {stream.quality}p -> {final.name}")
    args = stream.ffmpeg_args(str(part),
                              extra_args=["-y", "-loglevel", "error", "-stats", "-f", "mp4"])
    # okcdn.ru: эта сборка ffmpeg не принимает сертификат CDN — отключаем проверку TLS
    cmd = []
    for a in args:
        if a == "-i":
            cmd += ["-tls_verify", "0"]
        cmd.append(a)
    subprocess.run(cmd, check=True)
    part.replace(final)                        # переименовываем только после успеха


def fetch(embed, title, ep, dub, max_q):
    try:
        base = safe(f"{title} - {ep:02d} [{dub}]")
        final = OUT / safe(title) / (base + ".mp4")
        q = dict(parse_qsl(urlsplit(embed).query))
        if q.get("anime_id"):
            # CVH на YummyAnime: iframe-обёртка iframeCVH.html?anime_id=...
            with ap.CvhPlayer(pub="745", aggr="mali",
                              headers={"Origin": "https://ru.yummyani.me"}) as cvh:
                cvh.default_referer = embed
                result = cvh.extract(q["anime_id"], episode=ep, studio=dub)
        else:
            result = ap.extract(embed)
        save(result, final, max_q)
    except ap.UnsupportedUrl:
        print(f"    [ep {ep:02d}] плеер не поддерживается (например, Alloha) — выберите CVH или Kodik")
    except (ap.AnimeDlCoreError, subprocess.CalledProcessError) as e:
        print(f"    [ep {ep:02d}] ошибка: {e}")


def yummy_data(url):
    """(title, items) из официального API; items: (серия, плеер, озвучка, embed)."""
    if not YUMMY_TOKEN:
        print("    нужен токен в token.txt (создать: https://site.yummyani.me/dev/applications)")
        return None
    slug = re.split(r"[#?]", url.rstrip("/"))[0].rsplit("/", 1)[-1]
    h = {**UA, "X-Application": YUMMY_TOKEN, "Lang": "ru", "Accept": "application/json"}
    r = requests.get(f"https://api.yani.tv/anime/{slug}", headers=h, timeout=30)
    if r.status_code == 404:
        print(f"    '{slug}' не найдено в официальном API YummyAnime.")
        print("    возьмите ссылку на это аниме с old.yummyani.me или ru.yummyani.me")
        return None
    r.raise_for_status()
    anime = r.json()["response"]
    title = anime["title"]
    videos = requests.get(f"https://api.yani.tv/anime/{anime['anime_id']}/videos",
                          headers=h, timeout=30).json()["response"]
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
        d = re.sub(r"^(озвучка|субтитры)\s+", "", (data.get("dubbing") or "?").strip(), flags=re.I)
        items.append((num, p, d, embed))
    return title, items


def run_yummy(url, eps_text, dub, player, max_q):
    data = yummy_data(url)
    if not data:
        return
    title, items = data
    if not items:
        print("    видео не найдены")
        return
    available = sorted({n for n, _, _, _ in items})
    print(f"    «{title}», серии {available[0]}..{available[-1]}")
    if is_list_mode(eps_text):
        print_combos(items)
        print("    для скачивания подходят CVH и Kodik; Alloha библиотека не извлекает")
        return
    for ep in parse_episodes(eps_text, available):
        cand = [it for it in items if it[0] == ep
                and (player == "-" or it[1].lower() == player.lower())
                and (dub == "-" or dub.lower() in it[2].lower())]
        if not cand:
            opts = "; ".join(sorted({f"{p}/{d}" for n, p, d, _ in items if n == ep}))
            print(f"    [ep {ep:02d}] нет '{player}/{dub}'. Есть: {opts or 'серия ещё не вышла'}")
            continue
        cand.sort(key=lambda it: PRIORITY.index(it[1].lower()) if it[1].lower() in PRIORITY else 99)
        _, p, d, embed = cand[0]
        print(f"    [ep {ep:02d}] {p} / {d}")
        fetch(embed, title, ep, d, max_q)


def animego_title(url):
    try:
        m = re.search(r"<h1[^>]*>(.*?)</h1>",
                      requests.get(url, headers=UA, timeout=20).text, re.S)
        if m:
            return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    except requests.RequestException:
        pass
    return None


def run_animego(url, eps_text, dub, player, max_q):
    url = re.split(r"[#?]", url)[0]
    mirror = re.match(r"https?://([^/]+)", url).group(1)
    with AnimeGo(mirror=mirror) as site:
        anime_id = AnimeGo.anime_id(url)
        available = sorted(site.episodes(anime_id))
        if not available:
            print("    серии не найдены")
            return
        title = animego_title(url) or f"anime_{anime_id}"
        print(f"    «{title}», серии {available[0]}..{available[-1]}")
        if is_list_mode(eps_text):
            links = site.players(anime_id, episode=available[0])
            print("    плееры/озвучки (на примере первой серии):")
            for p, d in sorted({(l.player, l.label) for l in links}):
                print(f"    {p} / {d}")
            return
        for ep in parse_episodes(eps_text, available):
            if ep not in available:
                print(f"    [ep {ep:02d}] нет на сайте")
                continue
            links = site.players(anime_id, episode=ep)
            cand = [l for l in links
                    if (player == "-" or l.player.lower() == player.lower())
                    and (dub == "-" or dub.lower() in l.label.lower())]
            if not cand:
                opts = "; ".join(sorted({f"{l.player}/{l.label}" for l in links}))
                print(f"    [ep {ep:02d}] нет '{player}/{dub}'. Есть: {opts}")
                continue
            cand.sort(key=lambda l: PRIORITY.index(l.player.lower())
                      if l.player.lower() in PRIORITY else 99)
            l = cand[0]
            print(f"    [ep {ep:02d}] {l.player} / {l.label}")
            fetch(l.embed, title, ep, l.label, max_q)


def run_cvh(cvh_id, eps_text, dub, max_q):
    with ap.CvhPlayer() as p:
        playlist = p.playlist(cvh_id)
        available = sorted({e.episode for e in playlist})
        title = f"CVH {cvh_id}"
        if is_list_mode(eps_text):
            print(f"    {title}, серии {available[0]}..{available[-1]}")
            combos = {}
            for e in playlist:
                combos.setdefault(e.studio or "?", []).append(e.episode)
            for d, eps in sorted(combos.items()):
                print(f"    CVH / {d} — серии {min(eps)}..{max(eps)}, {len(eps)} шт.")
            return
        for ep in parse_episodes(eps_text, available):
            match = [e for e in playlist if e.episode == ep
                     and (dub == "-" or dub.lower() in (e.studio or "").lower())]
            if not match:
                print(f"    [ep {ep:02d}] озвучка '{dub}' не найдена")
                continue
            e = match[0]
            try:
                res = p.extract(cvh_id, season=e.season, episode=e.episode, studio=e.studio)
                base = safe(f"{title} - {ep:02d} [{e.studio}]")
                save(res, OUT / safe(title) / (base + ".mp4"), max_q)
            except (ap.AnimeDlCoreError, subprocess.CalledProcessError) as err:
                print(f"    [ep {ep:02d}] ошибка: {err}")


def dispatch(url, eps_text, dub, player, max_q):
    if re.search(r"y[au]mmy", url, re.I):
        run_yummy(url, eps_text, dub, player, max_q)
    elif re.search(r"animego\.[^/]+/anime/", url):
        run_animego(url, eps_text, dub, player, max_q)
    elif url.lower().startswith("cvh:"):
        run_cvh(url.split(":", 1)[1].strip(), eps_text, dub, max_q)
    else:                                       # прямая embed-ссылка = одно видео
        res = ap.extract(url)
        name = safe(res.title or "video")
        save(res, OUT / name / f"{name}.mp4", max_q)


# --- режим разведки: python batch_dl.py <ссылка> ---
if len(sys.argv) > 1:
    url = sys.argv[1]
    print(f"=== разведка: {url} ===")
    try:
        if (re.search(r"y[au]mmy", url, re.I)
                or re.search(r"animego\.[^/]+/anime/", url)
                or url.lower().startswith("cvh:")):
            dispatch(url, "?", "-", "-", 1080)
        else:
            res = ap.extract(url)
            print(f"плеер: {res.player}; качества: {res.qualities}")
            for s in res:
                print(f"  {s}")
    except (ap.AnimeDlCoreError, requests.RequestException, KeyError) as e:
        print(f"ошибка: {e}")
    sys.exit(0)

# --- обычный режим: задачи из tasks.txt ---
n = 0
try:
    for line in Path(TASKS).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        n += 1
        parts = [p.strip() for p in line.split("|")]
        parts += ["-"] * (5 - len(parts))
        url, eps_text, dub, player, quality = parts[:5]
        dub, player = dub or "-", player or "-"
        max_q = int(quality) if quality.isdigit() else 1080
        print(f"\n=== [{n}] {url} | серии {eps_text} | {dub} | {player} ===")
        try:
            dispatch(url, eps_text, dub, player, max_q)
        except (ap.AnimeDlCoreError, subprocess.CalledProcessError,
                requests.RequestException, KeyError) as e:
            print(f"    ошибка: {e}")
except KeyboardInterrupt:
    print("\nОстановлено. Готовые файлы целы; недокачанные (.part) перекачаются при следующем запуске.")