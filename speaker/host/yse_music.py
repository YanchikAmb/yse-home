# Служба музыки спальни. Токены людей лежат на колонке, сюда попадают только на время запроса и в журнал не пишутся.
import json
import logging
import math
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

logging.getLogger("yandex_music").setLevel(logging.CRITICAL)

HOST = "0.0.0.0"
PORT = int(os.environ.get("YSE_BASE", "18765")) + 1
SPEAKER_IP = os.environ.get("YSE_SPEAKER", "192.168.209.105")
SPEAKER = ["/storage/yse/bin/yse-ssh", SPEAKER_IP]
ROOT = "/data/opt/yandex-station"
ID_RE = re.compile(r"^[0-9a-f]{8}$")
FFMPEG = "ffmpeg"
LIBVA = "/lib/x86_64-linux-gnu/libva.so.2.1400.0"
BANDS = 16


def speaker_ip():
    """Адрес, с которого колонка сейчас шлёт звук. Иначе YSE_SPEAKER."""
    ip = os.environ.get("YSE_SPEAKER", "") or SPEAKER_IP
    try:
        import yse_peer
        ip = yse_peer.current() or ip
    except Exception:
        pass
    if ip:
        SPEAKER[1] = ip
    return ip

pending = {}
pending_lock = threading.Lock()
play_lock = threading.Lock()
duck_token = 0
player = {
    "proc": None,
    "tracks": [],
    "pending": [],
    "index": 0,
    "gen": 0,
    "paused": False,
    "ducked": False,
    "stopped": True,
    "manual": False,
    "quiet": False,
    "person": "",
    "title": "",
    "kind": "",
    "station": "",
    "batch_id": "",
    "now_title": "",
    "now_artist": "",
    "now_id": "",
    "now_mark": "",
    "lyrics": "",
    "lyrics_none": False,
    "started": 0.0,
    "thread": None,
}
WAVE = "user:onyourwave"
mark_lock = threading.Lock()
last_mark = {"cmd": "", "at": 0.0}


def log(text):
    print(text, flush=True)


def speaker(cmd, timeout=12):
    return subprocess.run(SPEAKER + [cmd], capture_output=True, timeout=timeout)


def speaker_text(cmd):
    proc = speaker(cmd)
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", "replace")


def person_token(person_id):
    if not ID_RE.match(person_id or ""):
        return ""
    text = speaker_text("cat %s/people/%s/token" % (ROOT, person_id))
    if text.strip():
        return text.strip()
    if os.environ.get("YSE_PLAY") == "http":
        account = load_account()
        if account and account["id"] == person_id:
            return account["token"]
    return ""


def person_by_name(name):
    wanted = (name or "").strip().lower().replace("ё", "е")
    if not wanted:
        return ""
    listing = speaker_text(
        "cd %s/people 2>/dev/null || exit 0; "
        "for d in *; do [ -f \"$d/name\" ] || continue; "
        "printf '%%s\\t' \"$d\"; tr -d '\\n' < \"$d/name\"; printf '\\n'; done" % ROOT
    )
    for line in listing.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2 or not ID_RE.match(parts[0]):
            continue
        got = parts[1].strip().lower().replace("ё", "е")
        if got == wanted or wanted in got or got in wanted:
            return parts[0]
    if os.environ.get("YSE_PLAY") == "http":
        account = load_account()
        got = (account or {}).get("name") or ""
        got = got.strip().lower().replace("ё", "е")
        if account and got and (got == wanted or wanted in got or got in wanted):
            return account["id"]
    return ""


def active_id():
    text = speaker_text("cat %s/settings/music.who 2>/dev/null" % ROOT).strip()
    if ID_RE.match(text):
        return text
    if os.environ.get("YSE_PLAY") == "http":
        account = load_account()
        if account:
            return account["id"]
    return ""


def set_active(person_id):
    if not ID_RE.match(person_id):
        return
    speaker("mkdir -p %s/settings && printf '%%s' %s > %s/settings/music.who" % (ROOT, person_id, ROOT))


ACCOUNT_PATH = "/storage/yse/music-account.json"
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def speaker_ips():
    """Все колонки, для которых на сервере есть служба: вход на одной достаётся каждой."""
    found = []
    try:
        names = os.listdir("/storage/yse")
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".env"):
            continue
        try:
            with open(os.path.join("/storage/yse", name), encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        ip = ""
        base = ""
        for line in text.splitlines():
            if line.startswith("YSE_SPEAKER="):
                ip = line.split("=", 1)[1].strip()
            elif line.startswith("YSE_BASE="):
                base = line.split("=", 1)[1].strip()
        try:
            import yse_peer
            ip = yse_peer.current_for(base, ip)
        except Exception:
            pass
        if IP_RE.match(ip):
            found.append(ip)
    self_ip = speaker_ip()
    if IP_RE.match(self_ip) and self_ip not in found:
        found.append(self_ip)
    return list(dict.fromkeys(found))


def remote(ip, command, data="", timeout=12):
    if not IP_RE.match(ip):
        return False
    try:
        proc = subprocess.run(
            ["/storage/yse/bin/yse-ssh", ip, command],
            input=data.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def save_account(account):
    os.makedirs("/storage/yse", exist_ok=True)
    raw = json.dumps({
        "id": account["id"],
        "name": account.get("name") or "",
        "login": account.get("login") or "",
        "token": account["token"],
    }, ensure_ascii=True)
    tmp = ACCOUNT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(raw)
    os.chmod(tmp, 0o600)
    os.replace(tmp, ACCOUNT_PATH)
    os.chmod(ACCOUNT_PATH, 0o600)


def load_account():
    try:
        with open(ACCOUNT_PATH, encoding="utf-8") as fh:
            account = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(account, dict) or not ID_RE.match(str(account.get("id") or "")):
        return None
    token = str(account.get("token") or "").strip()
    if not token or "\n" in token or len(token) > 2000:
        return None
    account["token"] = token
    account["name"] = " ".join(str(account.get("name") or "").split())[:40]
    account["login"] = " ".join(str(account.get("login") or "").split())[:60]
    return account


def put_account(ip, account, force_who):
    """Кладёт человека на колонку. Токен идёт в stdin, не в команду. Возвращает have/wrote/miss."""
    person_id = account["id"]
    if not ID_RE.match(person_id):
        return "miss"
    have = remote(ip, "test -s %s/people/%s/token" % (ROOT, person_id))
    if have and not force_who:
        return "have"
    payload = "%s\n%s\n%s\n" % (account.get("name") or "", account.get("login") or "", account["token"])
    cmd = (
        "IFS= read -r name; IFS= read -r login; IFS= read -r token; "
        "D=%s/people/%s; mkdir -p \"$D\" %s/settings; "
        "printf '%%s' \"$name\" > \"$D/name\"; "
        "printf '%%s' \"$login\" > \"$D/login\"; "
        "printf '%%s' \"$token\" > \"$D/token\"; "
        "chmod 600 \"$D/token\"; "
    ) % (ROOT, person_id, ROOT)
    if force_who:
        cmd += "printf '%%s' %s > %s/settings/music.who; " % (person_id, ROOT)
    else:
        cmd += "[ -s %s/settings/music.who ] || printf '%%s' %s > %s/settings/music.who; " % (ROOT, person_id, ROOT)
    cmd += "echo ok"
    return "wrote" if remote(ip, cmd, payload) else "miss"


def share_account(account, force_who):
    try:
        save_account(account)
    except OSError as exc:
        log("music account save %s" % type(exc).__name__)
    wrote = have = miss = 0
    for ip in speaker_ips():
        result = put_account(ip, account, force_who)
        if result == "wrote":
            wrote += 1
        elif result == "have":
            have += 1
        else:
            miss += 1
    log("music account wrote %d have %d miss %d" % (wrote, have, miss))


def publish_local_if_needed():
    """Уже существующий вход на этой колонке один раз раздаётся остальным, пока общего файла нет."""
    if load_account():
        return
    person_id = active_id()
    token = person_token(person_id) if person_id else ""
    if not token or "\n" in token:
        return
    share_account({
        "id": person_id,
        "name": speaker_text("tr -d '\\n' < %s/people/%s/name 2>/dev/null" % (ROOT, person_id)).strip()[:40],
        "login": speaker_text("tr -d '\\n' < %s/people/%s/login 2>/dev/null" % (ROOT, person_id)).strip()[:60],
        "token": token.strip(),
    }, False)


def account_loop():
    """Колонка, которая была выключена в момент входа, забирает аккаунт, когда снова отвечает."""
    publish_local_if_needed()
    while True:
        time.sleep(180)
        account = load_account()
        if not account:
            continue
        result = put_account(speaker_ip(), account, False)
        if result != "have":
            log("music account self %s" % result)


def client_for(person_id):
    from yandex_music import Client

    token = person_token(person_id)
    if not token:
        return None
    client = Client(token).init()
    return client


def folded(text):
    return (text or "").lower().replace("ё", "е").strip()


_lists = {}
_lists_lock = threading.Lock()
_owners = {}
GENERATED = ("playlistOfTheDay", "neverHeard", "missedLikes", "recentTracks")


def generated_rows(client, person_id):
    """«Плейлист дня», «Дежавю», «Тайник», «Премьера» из ленты. Владелец у них не человек, а Яндекс:
    номер владельца запоминаем, чтобы collect_tracks открыл список по kind."""
    try:
        found = client.feed().generated_playlists or []
    except Exception as exc:
        log("feed %s" % type(exc).__name__)
        return []
    rows = []
    owners = {}
    for item in sorted(found, key=lambda g: GENERATED.index(g.type) if g.type in GENERATED else 99):
        playlist = getattr(item, "data", None)
        if not getattr(item, "ready", True) or playlist is None or playlist.kind is None or not playlist.title:
            continue
        owner = getattr(getattr(playlist, "owner", None), "uid", None)
        if owner is None:
            continue
        owners[str(playlist.kind)] = int(owner)
        rows.append({"kind": str(playlist.kind), "title": playlist.title,
                     "tracks": int(getattr(playlist, "track_count", 0) or 0), "day": item.type == "playlistOfTheDay"})
    with _lists_lock:
        _owners[person_id] = owners
    return rows


def playlist_owner(person_id, kind, uid):
    with _lists_lock:
        known = _owners.get(person_id)
    if known is None:
        client = client_for(person_id)
        if client is not None:
            generated_rows(client, person_id)
        with _lists_lock:
            known = _owners.get(person_id) or {}
    return known.get(str(kind), uid)


def playlist_rows(person_id):
    """Список плейлистов. Лайки целиком не качаем: из-за них запрос не успевал за 12 с и страница оставалась пустой."""
    now = time.time()
    with _lists_lock:
        hit = _lists.get(person_id)
        if hit and now - hit[0] < 600:
            return hit[1]
    started = time.time()
    client = client_for(person_id)
    if client is None or client.me is None or client.me.account is None:
        return None
    uid = client.me.account.uid
    rows = [
        {"kind": "wave", "title": "Моя волна", "tracks": 0},
        {"kind": "likes", "title": "Мне нравится", "tracks": 0},
    ]
    rows += generated_rows(client, person_id)
    try:
        found = client.users_playlists_list(uid) or []
    except Exception as exc:
        log("playlists %s" % type(exc).__name__)
        found = []
    for item in found:
        title = item.title or ""
        kind = getattr(item, "kind", None)
        if kind is None or not title:
            continue
        if folded(title) in ("моя волна", "мне нравится"):
            continue
        count = getattr(item, "track_count", 0) or 0
        rows.append({"kind": str(kind), "title": title, "tracks": int(count)})
    with _lists_lock:
        _lists[person_id] = (time.time(), rows)
    log("playlists %d %.1fs" % (len(rows), time.time() - started))
    return rows


FILLER = {
    "включи", "включите", "поставь", "поставьте", "запусти", "запустите",
    "играй", "играть", "пожалуйста", "плейлист", "музыку", "музыка",
    "мою", "мой", "моя", "мое", "моё", "мне", "колонка", "колонку", "джарвис",
}


def choose_kind(body, rows):
    text = folded(body)
    text = re.sub(r"[^0-9a-zа-я\s-]", " ", text)
    if "волн" in text:
        return "wave"
    words = [word for word in text.split() if word not in FILLER]
    if any(word.startswith("нрав") or word.startswith("лайк") for word in words):
        return "likes"
    wanted = " ".join(words).strip()
    if len(wanted) >= 3:
        for row in rows or []:
            title = folded(row["title"])
            if wanted in title or title in wanted:
                return row["kind"]
        for row in rows or []:
            title_words = [word for word in folded(row["title"]).split() if len(word) >= 4]
            if any(word in words for word in title_words):
                return row["kind"]
    if not wanted:
        day = [row["kind"] for row in rows or [] if row.get("day")]
        return day[0] if day else "likes"
    return ""


def artist_query(body):
    text = folded(body)
    text = re.sub(r"[^0-9a-zа-я\s-]", " ", text)
    extra = {
        "исполнителя", "исполнитель", "артиста", "артист", "группу", "группа",
        "песни", "песню", "песня", "треки", "трек", "певца", "певец", "моего", "мою",
    }
    words = [word for word in text.split() if word not in FILLER and word not in extra]
    return " ".join(words).strip()


RU_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i",
    "ь": "", "э": "e", "ю": "u", "я": "a",
}


def skeletons(value):
    found = set()
    for kh in ("x", "h"):
        chars = []
        for ch in folded(value):
            if ch == "х":
                chars.append(kh)
            elif ch in RU_LATIN:
                chars.append(RU_LATIN[ch])
            elif "a" <= ch <= "z":
                chars.append(ch)
        cons = "".join(ch for ch in "".join(chars) if ch not in "aeiouy")
        if len(cons) >= 3:
            found.add(cons)
    return found


def name_close(query, name):
    def norm(value):
        return folded(value).replace(" ", "").replace("э", "е").replace("ё", "е")

    left = norm(query)
    right = norm(name)
    if len(left) >= 3 and len(right) >= 3 and (left in right or right in left):
        return True
    stem = left
    for tail in ("ой", "ей", "ом", "ем", "ая", "ую", "а", "я", "у", "ю", "е", "ы"):
        if stem.endswith(tail) and len(stem) - len(tail) >= 4:
            stem = stem[: -len(tail)]
            break
    if stem and len(right) >= 3 and (stem in right or right.startswith(stem) or stem.startswith(right)):
        return True
    left_sets = skeletons(query)
    right_sets = skeletons(name)
    return any(item in other or other in item for item in left_sets for other in right_sets)


def find_artist(client, query):
    found = client.search(query, type_="artist")
    bucket = getattr(found, "artists", None) if found is not None else None
    artists = list(getattr(bucket, "results", None) or [])
    for artist in artists[:5]:
        if name_close(query, getattr(artist, "name", "") or ""):
            return artist
    return None


def open_artist(client, query):
    artist = find_artist(client, query)
    if artist is None:
        stem = artist_query(query)
        if stem != query:
            artist = find_artist(client, stem)
    if artist is None:
        return [], ""
    tracks = []
    try:
        info = client.artists_brief_info(artist.id)
        tracks = usable_tracks(getattr(info, "popular_tracks", None) if info is not None else None)
    except Exception as exc:
        log("artist %s" % type(exc).__name__)
    if not tracks:
        page = client.artists_tracks(artist.id, page=0, page_size=25)
        tracks = usable_tracks(getattr(page, "tracks", None) if page is not None else None)
    title = getattr(artist, "name", "") or query
    return tracks, title


def track_url(track):
    infos = track.get_download_info(get_direct_links=True) or []
    best = None
    best_rate = -1
    for info in infos:
        if getattr(info, "preview", False):
            continue
        codec = (getattr(info, "codec", "") or "").lower()
        if codec not in ("mp3", "aac"):
            continue
        rate = int(getattr(info, "bitrate_in_kbps", 0) or 0)
        link = getattr(info, "direct_link", None)
        if not link and hasattr(info, "get_direct_link"):
            link = info.get_direct_link()
        if link and rate >= best_rate:
            best = link
            best_rate = rate
    return best


def alarm_label(title, artist):
    """Короткая подпись мелодии: без кавычек, чтобы её можно было положить в файл и в JSON."""
    text = " ".join(str(title or "").split())
    who = " ".join(str(artist or "").split())
    if who:
        text = "%s — %s" % (text, who) if text else who
    kept = []
    for ch in text:
        if ch.isalnum() or ch in " -—.,":
            kept.append(ch)
    return "".join(kept).strip()[:80] or "Своя мелодия"


def put_alarm(pcm, label):
    """Сырой сигнал 16 кГц на колонку. Будильник играет его вместо маримбы."""
    if not pcm or len(pcm) < 8000:
        raise RuntimeError("Мелодия пустая")
    label = alarm_label(label, "")
    script = (
        "cat > %s/alarm.raw && mkdir -p %s/settings && printf '%%s\\n' '%s' > %s/settings/alarm.label"
        % (ROOT, ROOT, label, ROOT)
    )
    proc = subprocess.run(SPEAKER + [script], input=pcm, capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError("Колонка не приняла мелодию")


def bake_alarm(data, label):
    """Любой звук → моно, 16 кГц, не длиннее трёх минут."""
    tmp = "/tmp/yse-alarm-src-%s" % os.environ.get("YSE_BASE", "0")
    with open(tmp, "wb") as handle:
        handle.write(data)
    try:
        env = os.environ.copy()
        env["LD_PRELOAD"] = LIBVA
        proc = subprocess.run(
            [FFMPEG, "-y", "-i", tmp, "-t", "180", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
            capture_output=True,
            timeout=90,
            env=env,
        )
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if proc.returncode != 0 or len(proc.stdout) < 8000:
        raise RuntimeError("Не разобрала этот звук")
    put_alarm(proc.stdout, label)


def clear_alarm_tone():
    speaker("rm -f %s/alarm.raw %s/settings/alarm.label" % (ROOT, ROOT), timeout=15)


def alarm_search(query):
    """None — в музыку не вошли. Иначе первые треки по запросу."""
    query = " ".join((query or "").split())[:120]
    if len(query) < 2:
        return []
    client = client_for(active_id())
    if client is None:
        return None
    found = client.search(query, type_="track")
    bucket = getattr(found, "tracks", None) if found is not None else None
    tracks = list(getattr(bucket, "results", None) or [])
    out = []
    for track in tracks[:6]:
        artists = getattr(track, "artists", None) or []
        artist = str(getattr(artists[0], "name", "") or "") if artists else ""
        track_id = str(getattr(track, "id", "") or "")
        if not track_id.isdigit():
            continue
        out.append({
            "id": track_id,
            "title": str(getattr(track, "title", "") or ""),
            "artist": artist,
        })
    return out


def alarm_apply_track(track_id):
    if not str(track_id).isdigit():
        raise RuntimeError("Не тот трек")
    client = client_for(active_id())
    if client is None:
        raise RuntimeError("Сначала войдите в Яндекс Музыку")
    found = client.tracks([str(track_id)])
    track = found[0] if found else None
    if track is None:
        raise RuntimeError("Трек не найден")
    try:
        url = track_url(track)
    except Exception as exc:
        log("alarm link %s" % type(exc).__name__)
        url = ""
    if not url:
        raise RuntimeError("У этого трека нет файла")
    link = url
    data = b""
    for _hop in range(6):
        req = urllib.request.Request(link, headers={"User-Agent": "Lavf/58.76.100", "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                data = resp.read(12 * 1024 * 1024)
            break
        except urllib.error.HTTPError as exc:
            where = exc.headers.get("Location") if exc.code in (301, 302, 303, 307, 308) else None
            if not where:
                raise
            link = urllib.parse.urljoin(link, where)
    else:
        raise RuntimeError("У этого трека нет файла")
    artists = getattr(track, "artists", None) or []
    artist = str(getattr(artists[0], "name", "") or "") if artists else ""
    label = alarm_label(getattr(track, "title", ""), artist)
    bake_alarm(data, label)
    return label


def track_names(track):
    title = getattr(track, "title", "") or ""
    names = []
    for artist in getattr(track, "artists", None) or []:
        name = getattr(artist, "name", "") or ""
        if name:
            names.append(name)
    plain = str(getattr(track, "id", "") or "")
    albums = getattr(track, "albums", None) or []
    album = getattr(albums[0], "id", None) if albums else None
    mark = "%s:%s" % (plain, album) if plain and album else plain
    return title, ", ".join(names), plain, mark


def usable_tracks(items):
    found = []
    for track in items or []:
        if track is not None and getattr(track, "id", None):
            found.append(track)
    return found


def short_id(short):
    tid = getattr(short, "id", None)
    if not tid:
        tid = getattr(short, "track_id", None)
    if not tid:
        return ""
    album = getattr(short, "album_id", None)
    if album:
        return "%s:%s" % (tid, album)
    return str(tid)


def collect_tracks(person_id, kind):
    client = client_for(person_id)
    if client is None or client.me is None or client.me.account is None:
        return [], ""
    uid = playlist_owner(person_id, kind, client.me.account.uid)
    playlist = client.users_playlists(int(kind), uid)
    if playlist is None:
        return [], ""
    if hasattr(playlist, "fetch_tracks"):
        playlist.fetch_tracks()
    title = playlist.title or ""
    tracks = []
    for short in playlist.tracks or []:
        track = getattr(short, "track", None) or short
        if track is None or not getattr(track, "id", None):
            continue
        tracks.append(track)
    return tracks, title


def open_wave(client):
    result = client.rotor_station_tracks(WAVE)
    sequence = getattr(result, "sequence", None) or []
    tracks = usable_tracks(getattr(item, "track", None) for item in sequence)
    batch = getattr(result, "batch_id", "") or ""
    try:
        client.rotor_station_feedback_radio_started(WAVE, from_="yse", batch_id=batch or None)
    except Exception as exc:
        log("wave radio %s" % type(exc).__name__)
    return tracks, "Моя волна", batch


def open_likes(client):
    liked = client.users_likes_tracks()
    ids = []
    for short in getattr(liked, "tracks", None) or []:
        item = short_id(short)
        if item:
            ids.append(item)
    first = usable_tracks(client.tracks(ids[:20]) if ids else [])
    return first, "Мне нравится", ids[20:]


def plain_lyrics(raw):
    if not raw:
        return ""
    text = raw.replace("\r", "")
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"\[\d{1,2}:\d{2}(?:[.:]\d+)?\]", "", line).strip()
        if line:
            lines.append(line)
    body = "\n".join(lines)
    if len(body) > 1800:
        body = body[:1800].rsplit("\n", 1)[0]
    return body


def fill_lyrics(gen, idx, person_id, track_id):
    text = ""
    try:
        client = client_for(person_id)
        if client is not None and track_id:
            info = client.tracks_lyrics(track_id)
            if info is not None:
                text = plain_lyrics(info.fetch_lyrics() or "")
    except Exception as exc:
        log("lyric %s" % type(exc).__name__)
    with play_lock:
        if player["gen"] == gen and player["index"] == idx and player["now_id"] == track_id:
            player["lyrics"] = text
            player["lyrics_none"] = not text


def wave_event(person_id, kind, track_id, batch_id, seconds):
    try:
        client = client_for(person_id)
        if client is None:
            return
        if kind == "start":
            client.rotor_station_feedback_track_started(WAVE, track_id, batch_id=batch_id or None)
        elif kind == "finish":
            client.rotor_station_feedback_track_finished(
                WAVE, track_id, seconds, batch_id=batch_id or None
            )
        elif kind == "skip":
            client.rotor_station_feedback_skip(WAVE, track_id, seconds, batch_id=batch_id or None)
        elif kind in ("like", "dislike"):
            client.rotor_station_feedback(
                WAVE, kind, track_id=track_id, batch_id=batch_id or None, total_played_seconds=seconds
            )
    except Exception as exc:
        log("wave %s %s" % (kind, type(exc).__name__))


def extend_queue(client):
    with play_lock:
        kind = player["kind"]
        person = player["person"]
        if kind == "wave":
            if player["index"] < max(len(player["tracks"]) - 1, 0):
                return
            last = player["tracks"][-1] if player["tracks"] else None
            queue = str(getattr(last, "id", "") or "") if last is not None else ""
            pending = None
        elif kind == "likes":
            if player["index"] < len(player["tracks"]) - 5 or not player["pending"]:
                return
            pending = player["pending"][:20]
            player["pending"] = player["pending"][20:]
            queue = ""
        else:
            return
    if client is None:
        client = client_for(person)
    if client is None:
        return
    try:
        if kind == "wave":
            result = client.rotor_station_tracks(WAVE, queue=queue or None)
            found = usable_tracks(getattr(item, "track", None) for item in (getattr(result, "sequence", None) or []))
            batch = getattr(result, "batch_id", "") or ""
        else:
            found = usable_tracks(client.tracks(pending))
            batch = ""
    except Exception as exc:
        log("queue %s" % type(exc).__name__)
        if pending:
            with play_lock:
                player["pending"] = list(pending) + player["pending"]
        return
    with play_lock:
        have = set()
        for track in player["tracks"]:
            have.add(str(getattr(track, "id", "") or ""))
        for track in found:
            tid = str(getattr(track, "id", "") or "")
            if tid and tid not in have:
                player["tracks"].append(track)
                have.add(tid)
        if batch:
            player["batch_id"] = batch


def kill_player():
    global duck_token
    proc = player["proc"]
    player["proc"] = None
    player["paused"] = False
    player["ducked"] = False
    duck_token += 1
    if proc is None:
        return
    if hasattr(proc, "stop"):
        proc.stop()
        return
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGCONT)
    except OSError:
        pass
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


def band_levels(pcm):
    usable = len(pcm) - (len(pcm) % 4)
    if usable < 512:
        return None
    width = 256
    mono = []
    for i in range(width):
        off = (i * usable // width) // 4 * 4
        left = int.from_bytes(pcm[off:off + 2], "little", signed=True)
        right = int.from_bytes(pcm[off + 2:off + 4], "little", signed=True)
        mono.append((left + right) * 0.5)
    energy = 0.0
    for sample in mono:
        energy += sample * sample
    if energy / len(mono) < 40000:
        return bytes(BANDS)
    out = bytearray(BANDS)
    for band in range(BANDS):
        freq = (band + 1) * 3
        coeff = 2.0 * math.cos(2.0 * math.pi * freq / width)
        prev = 0.0
        prev2 = 0.0
        for sample in mono:
            current = sample + coeff * prev - prev2
            prev2 = prev
            prev = current
        power = prev * prev + prev2 * prev2 - coeff * prev * prev2
        level = int(min(255, math.sqrt(max(power, 0.0)) / 1800))
        out[band] = level
    return bytes(out)


class Stream:
    def __init__(self, url, offset=0.0):
        env = os.environ.copy()
        env["LD_PRELOAD"] = LIBVA
        self.offset = offset
        self.bytes_out = 0
        self.cut = False
        seek = ["-ss", "%.2f" % offset] if offset > 0 else []
        net = []
        if url.startswith("http"):
            net = ["-reconnect", "1", "-reconnect_streamed", "1",
                   "-reconnect_on_network_error", "1", "-reconnect_delay_max", "5"]
        self.ff = subprocess.Popen(
            [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error"]
            + net + seek + ["-i", url, "-f", "s16le", "-ar", "48000", "-ac", "2", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True,
        )
        if os.environ.get("YSE_PLAY") == "http":
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yse_pcm_push.py")
            play_cmd = [sys.executable, script, "http://%s:18080/api/pcm" % speaker_ip(), "48000", "2", "music"]
        else:
            play_cmd = SPEAKER + ["dev=plughw:0,0; grep -q softvol /etc/asound.conf 2>/dev/null && dev=default; "
                                  "aplay -D \"$dev\" -f S16_LE -r 48000 -c 2 -t raw -"]
        self.play = subprocess.Popen(
            play_cmd,
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.eq = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.pid = self.ff.pid
        self._eq_logged = False
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _drain(self):
        try:
            self._err_text = self.ff.stderr.read(500)
        except Exception:
            self._err_text = b""

    def _note_error(self):
        text = getattr(self, "_err_text", b"") or b""
        err = text.decode("utf-8", "replace").strip().splitlines()
        if not err:
            return
        line = re.sub(r"\S*https?://\S+", "<url>", err[-1])
        if "token" in line.lower():
            log("ffmpeg fail")
            return
        log("ffmpeg fail %s" % line[:120])

    @property
    def played(self):
        return self.offset + self.bytes_out / (48000 * 4)

    @property
    def failed(self):
        return not self.cut and self.ff.returncode not in (None, 0)

    def _pump(self):
        self._err_text = b""
        drainer = threading.Thread(target=self._drain, daemon=True)
        drainer.start()
        try:
            while True:
                chunk = self.ff.stdout.read(4800 * 4)
                if not chunk:
                    break
                try:
                    self.play.stdin.write(chunk)
                    self.play.stdin.flush()
                except (BrokenPipeError, OSError):
                    break
                self.bytes_out += len(chunk)
                levels = band_levels(chunk)
                if levels:
                    if not self._eq_logged:
                        self._eq_logged = True
                        log("eq %d" % max(levels))
                    try:
                        self.eq.sendto(levels, (speaker_ip(), 18767))
                    except OSError:
                        pass
        finally:
            try:
                self.play.stdin.close()
            except OSError:
                pass
            try:
                self.eq.sendto(bytes(BANDS), (speaker_ip(), 18767))
            except OSError:
                pass
            if self.ff.poll() is None:
                try:
                    os.killpg(self.ff.pid, signal.SIGTERM)
                except OSError:
                    pass
            self.ff.wait()
            drainer.join(1)
            self._note_error()

    def poll(self):
        if self.thread.is_alive():
            return None
        code = self.ff.poll()
        return 0 if code is None else code

    def wait(self, timeout=None):
        self.thread.join(timeout)
        return self.poll()

    def _signal(self, sig):
        for proc in (self.ff, self.play):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, sig)
                except OSError:
                    pass

    def pause(self):
        self._signal(signal.SIGSTOP)

    def resume(self):
        self._signal(signal.SIGCONT)

    def stop(self):
        self.cut = True
        self._signal(signal.SIGCONT)
        self._signal(signal.SIGTERM)
        self.thread.join(2)
        if self.thread.is_alive():
            self._signal(signal.SIGKILL)
            self.thread.join(2)


def start_stream(url, offset=0.0):
    return Stream(url, offset)


CACHE = "/tmp/yse-music-%d" % PORT


def fetch_track(url):
    os.makedirs(CACHE, exist_ok=True)
    for name in os.listdir(CACHE):
        try:
            os.unlink(os.path.join(CACHE, name))
        except OSError:
            pass
    path = os.path.join(CACHE, "%d.audio" % int(time.time() * 1000))
    size = 0
    try:
        link = url
        for _hop in range(6):
            req = urllib.request.Request(link, headers={"User-Agent": "Lavf/58.76.100", "Accept": "*/*"})
            try:
                resp = urllib.request.urlopen(req, timeout=20)
                break
            except urllib.error.HTTPError as exc:
                where = exc.headers.get("Location") if exc.code in (301, 302, 303, 307, 308) else None
                if not where:
                    raise
                link = urllib.parse.urljoin(link, where)
        else:
            raise OSError("redirects")
        with resp, open(path, "wb") as out:
            while True:
                block = resp.read(65536)
                if not block:
                    break
                size += len(block)
                if size > 80 * 1024 * 1024:
                    raise OSError("too big")
                out.write(block)
    except (OSError, ValueError) as exc:
        log("fetch fail %s %s" % (type(exc).__name__, getattr(exc, "code", "")))
        try:
            os.unlink(path)
        except OSError:
            pass
        return url
    return path


def track_seconds(track):
    try:
        return float(getattr(track, "duration_ms", 0) or 0) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def play_loop(gen):
    loop_client = None
    while True:
        with play_lock:
            if player["gen"] != gen:
                return
            person = player["person"]
            kind = player["kind"]
        try:
            extend_queue(loop_client)
        except Exception as exc:
            log("queue %s" % type(exc).__name__)
        with play_lock:
            if player["gen"] != gen:
                return
            idx = player["index"]
            tracks = player["tracks"]
            if idx >= len(tracks):
                player["stopped"] = True
                player["paused"] = False
                return
            track = tracks[idx]
            batch = player["batch_id"]
        try:
            url = track_url(track)
        except Exception as exc:
            log("track fail %s" % type(exc).__name__)
            url = None
        if not url:
            with play_lock:
                if player["gen"] == gen and player["index"] == idx:
                    player["index"] += 1
            continue
        title, artist, plain, mark = track_names(track)
        with play_lock:
            if player["gen"] != gen:
                return
            player["now_title"] = title
            player["now_artist"] = artist
            player["now_id"] = plain
            player["now_mark"] = mark
            player["lyrics"] = ""
            player["lyrics_none"] = False
            player["started"] = time.time()
            player["stopped"] = False
            player["paused"] = False
        threading.Thread(
            target=fill_lyrics, args=(gen, idx, person, plain), daemon=True
        ).start()
        src = fetch_track(url)
        proc = start_stream(src)
        if kind == "wave" and plain:
            threading.Thread(
                target=wave_event, args=(person, "start", plain, batch, 0), daemon=True
            ).start()
        with play_lock:
            if player["gen"] != gen:
                proc.stop()
                return
            player["proc"] = proc
            player["paused"] = False
        proc.wait()
        length = track_seconds(track)
        log("track end at %ds of %ds%s" % (int(proc.played), int(length), " cut" if proc.cut else ""))
        retries = 0
        while not proc.cut and retries < 3 and (
            (length and proc.played < length - 5) or (proc.failed and not length)
        ):
            with play_lock:
                if player["gen"] != gen or player["index"] != idx or player["manual"] or player["proc"] is not proc:
                    break
            retries += 1
            log("stream resume %d at %ds of %ds" % (retries, int(proc.played), int(length)))
            time.sleep(1)
            if src.startswith("http") or not os.path.exists(src):
                try:
                    url = track_url(track)
                except Exception as exc:
                    log("track fail %s" % type(exc).__name__)
                    break
                if not url:
                    break
                src = fetch_track(url)
            again = start_stream(src, proc.played)
            with play_lock:
                if player["gen"] != gen or player["proc"] is not proc:
                    again.stop()
                    return
                player["proc"] = again
                if player["paused"] or player.get("ducked"):
                    again.pause()
            proc = again
            proc.wait()
        with play_lock:
            if player["proc"] is proc:
                player["proc"] = None
            if player["gen"] != gen:
                return
            manual = player["manual"]
            player["manual"] = False
            started = player["started"] or time.time()
            if not manual and player["index"] == idx:
                player["index"] += 1
                finish = kind == "wave" and plain
            else:
                finish = False
        if finish:
            wave_event(person, "finish", plain, batch, max(0.0, time.time() - started))


def begin(person_id, kind):
    client = client_for(person_id)
    if client is None:
        return False, ""
    pending = []
    batch = ""
    station = ""
    try:
        if kind == "wave":
            tracks, title, batch = open_wave(client)
            station = WAVE
        elif kind == "likes":
            tracks, title, pending = open_likes(client)
        else:
            tracks, title = collect_tracks(person_id, kind)
    except Exception as exc:
        log("begin %s" % type(exc).__name__)
        return False, ""
    if not tracks:
        return False, title
    return begin_tracks(person_id, tracks, title, str(kind), pending, batch, station)


def begin_tracks(person_id, tracks, title, kind, pending=None, batch="", station=""):
    if not tracks:
        return False, title
    with play_lock:
        player["gen"] += 1
        gen = player["gen"]
        kill_player()
        player["tracks"] = tracks
        player["pending"] = pending or []
        player["index"] = 0
        player["person"] = person_id
        player["title"] = title
        player["kind"] = str(kind)
        player["station"] = station
        player["batch_id"] = batch
        player["paused"] = False
        player["stopped"] = False
        player["manual"] = False
        player["now_title"] = ""
        player["now_artist"] = ""
        player["now_id"] = ""
        player["now_mark"] = ""
        player["lyrics"] = ""
        thread = threading.Thread(target=play_loop, args=(gen,), daemon=True)
        player["thread"] = thread
    set_active(person_id)
    thread.start()
    return True, title


def launch_loop():
    player["gen"] += 1
    gen = player["gen"]
    player["stopped"] = False
    player["paused"] = False
    thread = threading.Thread(target=play_loop, args=(gen,), daemon=True)
    player["thread"] = thread
    thread.start()


def end_duck_locked(resume):
    global duck_token
    if not player.get("ducked"):
        return False
    player["ducked"] = False
    duck_token += 1
    proc = player["proc"]
    alive = proc is not None and proc.poll() is None
    if resume and alive and not player["paused"]:
        if hasattr(proc, "resume"):
            proc.resume()
        else:
            os.killpg(proc.pid, signal.SIGCONT)
    return True


def duck_down():
    global duck_token
    with play_lock:
        proc = player["proc"]
        alive = proc is not None and proc.poll() is None
        if player["stopped"] or not alive:
            return False
        if player["paused"] and not player.get("ducked"):
            return False
        if not player.get("ducked"):
            if hasattr(proc, "pause"):
                proc.pause()
            else:
                os.killpg(proc.pid, signal.SIGSTOP)
            player["ducked"] = True
        duck_token += 1
        token = duck_token
    timer = threading.Timer(5.0, duck_finish, args=(token,))
    timer.daemon = True
    timer.start()
    log("duck")
    return True


def duck_finish(token):
    global duck_token
    with play_lock:
        if token != duck_token or not player.get("ducked"):
            return
        player["ducked"] = False
        duck_token += 1
        proc = player["proc"]
        alive = proc is not None and proc.poll() is None
        if alive and not player["paused"]:
            if hasattr(proc, "resume"):
                proc.resume()
            else:
                os.killpg(proc.pid, signal.SIGCONT)
    log("duck up")


def duck_cancel(resume):
    with play_lock:
        return end_duck_locked(resume)


def ctrl(cmd):
    if cmd == "toggle":
        with play_lock:
            paused = bool(player["paused"])
        cmd = "resume" if paused else "pause"
    with play_lock:
        quiet = player["quiet"]
        player["quiet"] = False
        station = player["station"]
        now_id = player["now_id"]
        batch = player["batch_id"]
        person = player["person"]
        started = player["started"] or 0
    seconds = max(0.0, time.time() - started) if started else 0.0
    if cmd == "next" and station and now_id and not quiet:
        wave_event(person, "skip", now_id, batch, seconds)
    with play_lock:
        proc = player["proc"]
        alive = proc is not None and proc.poll() is None
        if cmd == "pause" and alive:
            end_duck_locked(False)
            if not player["paused"]:
                if hasattr(proc, "pause"):
                    proc.pause()
                else:
                    os.killpg(proc.pid, signal.SIGSTOP)
                player["paused"] = True
            return True
        if cmd in ("play", "resume") and alive and (player["paused"] or player.get("ducked")):
            end_duck_locked(False)
            if hasattr(proc, "resume"):
                proc.resume()
            else:
                os.killpg(proc.pid, signal.SIGCONT)
            player["paused"] = False
            player["stopped"] = False
            return True
        if cmd in ("play", "resume") and not alive and player["tracks"]:
            if player["index"] >= len(player["tracks"]) and not player["pending"]:
                player["index"] = 0
            launch_loop()
            return True
        if cmd == "stop":
            end_duck_locked(False)
            player["gen"] += 1
            kill_player()
            player["paused"] = False
            player["stopped"] = True
            return True
        if cmd == "next" and (player["tracks"] or player["pending"]):
            end_duck_locked(False)
            player["manual"] = True
            player["index"] = min(player["index"] + 1, len(player["tracks"]))
            player["paused"] = False
            player["stopped"] = False
            if alive:
                kill_player()
            else:
                launch_loop()
            return True
        if cmd == "prev" and player["tracks"]:
            end_duck_locked(False)
            player["manual"] = True
            player["index"] = max(player["index"] - 1, 0)
            player["paused"] = False
            player["stopped"] = False
            if alive:
                kill_player()
            else:
                launch_loop()
            return True
    return False


def mark(cmd):
    if cmd not in ("like", "dislike"):
        return False
    now = time.time()
    with mark_lock:
        if last_mark["cmd"] == cmd and now - last_mark["at"] < 2:
            return True
        last_mark["cmd"] = cmd
        last_mark["at"] = now
    with play_lock:
        person = player["person"]
        track_id = player["now_mark"] or player["now_id"]
        plain = player["now_id"]
        station = player["station"]
        batch = player["batch_id"]
        started = player["started"] or 0
    if cmd == "like":
        duck_cancel(True)
    else:
        duck_cancel(False)
    if not person or not track_id:
        return False
    seconds = max(0.0, time.time() - started) if started else 0.0
    try:
        client = client_for(person)
        if client is None:
            return False
        if cmd == "like":
            client.users_likes_tracks_add(track_id)
            try:
                client.users_dislikes_tracks_remove(track_id)
            except Exception:
                pass
        else:
            client.users_dislikes_tracks_add(track_id)
            try:
                client.users_likes_tracks_remove(track_id)
            except Exception:
                pass
    except Exception as exc:
        log("mark %s" % type(exc).__name__)
        return False
    if station and plain:
        wave_event(person, cmd, plain, batch, seconds)
    if cmd == "dislike":
        with play_lock:
            player["quiet"] = True
        ctrl("next")
    return True


def now_state():
    with play_lock:
        proc = player["proc"]
        alive = proc is not None and proc.poll() is None
        if alive and player["paused"]:
            state = "pause"
        elif alive:
            state = "play"
        else:
            state = "stop"
        return {
            "state": state,
            "playlist": player["title"] or "",
            "title": player["now_title"] or "",
            "artist": player["now_artist"] or "",
            "kind": player["kind"] or "",
            "lyrics": player["lyrics"] or "",
            "lyrics_none": bool(player.get("lyrics_none")),
            "ducked": bool(player.get("ducked")),
        }


def switch_person(person_id):
    with play_lock:
        same = player["person"] == person_id
        proc = player["proc"]
        alive = proc is not None and proc.poll() is None
        if same and alive and player["paused"]:
            if hasattr(proc, "resume"):
                proc.resume()
            else:
                os.killpg(proc.pid, signal.SIGCONT)
            player["paused"] = False
            player["stopped"] = False
            set_active(person_id)
            return
        if alive and not player["paused"]:
            if hasattr(proc, "pause"):
                proc.pause()
            else:
                os.killpg(proc.pid, signal.SIGSTOP)
            player["paused"] = True
    set_active(person_id)


def login_wait(login_id, device_code, interval, deadline):
    from yandex_music import Client

    while time.time() < deadline:
        try:
            token = Client().poll_device_token(device_code)
        except Exception as exc:
            text = str(exc)
            if "authorization_pending" in text:
                token = None
            else:
                with pending_lock:
                    item = pending.get(login_id)
                    if item is not None:
                        item["error"] = "вход не завершился"
                log("login fail")
                return
        if token is not None:
            access = getattr(token, "access_token", "") or ""
            login = ""
            uid = ""
            try:
                ready = Client(access).init()
                if ready.me is not None and ready.me.account is not None:
                    login = ready.me.account.login or ""
                    uid = str(ready.me.account.uid)
            except Exception:
                log("account fail")
            with pending_lock:
                item = pending.get(login_id)
                if item is not None:
                    item["token"] = access
                    item["login"] = login
                    item["uid"] = uid
            return
        time.sleep(max(interval, 2))
    with pending_lock:
        item = pending.get(login_id)
        if item is not None and not item.get("token"):
            item["error"] = "время вышло"


def start_login(name):
    from yandex_music import Client

    code = Client().request_device_code(device_name="Колонка")
    login_id = os.urandom(4).hex()
    interval = int(getattr(code, "interval", 5) or 5)
    expires = int(getattr(code, "expires_in", 300) or 300)
    with pending_lock:
        pending[login_id] = {
            "token": "",
            "login": "",
            "uid": "",
            "name": " ".join((name or "").split())[:40],
            "error": "",
            "user_code": code.user_code,
            "verification_url": code.verification_url,
        }
    threading.Thread(
        target=login_wait,
        args=(login_id, code.device_code, interval, time.time() + expires),
        daemon=True,
    ).start()
    return {
        "id": login_id,
        "user_code": code.user_code,
        "verification_url": code.verification_url,
    }


def take_login(login_id):
    with pending_lock:
        item = pending.get(login_id)
        if item is None:
            return {"ready": "no", "error": "нет такого входа"}
        if item.get("error") and not item.get("token"):
            return {"ready": "no", "error": item["error"]}
        if not item.get("token"):
            return {
                "ready": "no",
                "user_code": item.get("user_code", ""),
                "verification_url": item.get("verification_url", ""),
            }
        body = {
            "ready": "yes",
            "token": item["token"],
            "login": item.get("login", ""),
            "uid": item.get("uid", ""),
        }
        account = {
            "id": login_id,
            "name": item.get("name") or "",
            "login": item.get("login") or "",
            "token": item["token"],
        }
        item["token"] = ""
        threading.Thread(target=share_account, args=(account, True), daemon=True).start()
        return body


def read_body(handler):
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length < 0 or length > 4000:
        return ""
    return handler.rfile.read(length).decode("utf-8", "replace") if length else ""


def send(handler, code, payload):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def take_alarm_file(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length < 1000 or length > 8 * 1024 * 1024:
            send(self, 200, {"ok": False, "error": "Файл больше 8 МБ или пустой"})
            return
        data = self.rfile.read(length)
        try:
            bake_alarm(data, "Своя мелодия")
        except Exception as exc:
            log("alarm file %s" % type(exc).__name__)
            text = str(exc) if isinstance(exc, RuntimeError) else "Не разобрала этот звук"
            send(self, 200, {"ok": False, "error": text})
            return
        send(self, 200, {"ok": True, "label": "Своя мелодия"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/v1/alarm-file":
            self.take_alarm_file()
            return
        body = read_body(self).strip()
        try:
            if path == "/v1/alarm-search":
                try:
                    tracks = alarm_search(body)
                except Exception as exc:
                    log("alarm search %s" % type(exc).__name__)
                    send(self, 200, {"tracks": [], "error": "Поиск не ответил"})
                    return
                if tracks is None:
                    send(self, 200, {"tracks": [], "error": "Сначала войдите в Яндекс Музыку"})
                    return
                send(self, 200, {"tracks": tracks})
                return
            if path == "/v1/alarm-track":
                try:
                    label = alarm_apply_track(body)
                except Exception as exc:
                    log("alarm track %s" % type(exc).__name__)
                    text = str(exc) if isinstance(exc, RuntimeError) else "Не смогла поставить этот трек"
                    send(self, 200, {"ok": False, "error": text})
                    return
                send(self, 200, {"ok": True, "label": label})
                return
            if path == "/v1/alarm-builtin":
                clear_alarm_tone()
                send(self, 200, {"ok": True, "label": "Встроенная"})
                return
            if path == "/v1/login/start":
                send(self, 200, start_login(body))
                return
            if path == "/v1/who-id":
                if not ID_RE.match(body):
                    send(self, 400, {"error": "id"})
                    return
                switch_person(body)
                send(self, 200, {"ok": True, "id": body})
                return
            if path == "/v1/who":
                person_id = person_by_name(body)
                if not person_id:
                    send(self, 404, {"error": "нет такого человека"})
                    return
                switch_person(person_id)
                send(self, 200, {"ok": True, "id": person_id})
                return
            if path == "/v1/playlist":
                person_id = active_id()
                if not person_id:
                    send(self, 404, {"error": "плейлист не найден"})
                    return
                rows = playlist_rows(person_id)
                found = choose_kind(body, rows)
                if found:
                    ok, title = begin(person_id, found)
                else:
                    query = artist_query(body)
                    if len(query) < 3:
                        send(self, 404, {"error": "не нашёл"})
                        return
                    try:
                        client = client_for(person_id)
                        tracks, title = open_artist(client, query) if client is not None else ([], "")
                    except Exception as exc:
                        log("artist %s" % type(exc).__name__)
                        tracks, title = [], ""
                    if not tracks:
                        log("artist miss")
                        send(self, 404, {"error": "не нашёл"})
                        return
                    ok, title = begin_tracks(person_id, tracks, title, "artist")
                send(self, 200 if ok else 500, {"ok": ok, "title": title, "error": "" if ok else "в списке пусто"})
                return
            if path == "/v1/play":
                parts = body.split()
                if len(parts) != 2 or not ID_RE.match(parts[0]):
                    send(self, 400, {"error": "play"})
                    return
                if parts[1] not in ("wave", "likes") and not parts[1].isdigit():
                    send(self, 400, {"error": "play"})
                    return
                ok, title = begin(parts[0], parts[1])
                send(self, 200 if ok else 500, {"ok": ok, "title": title, "error": "" if ok else "в списке пусто"})
                return
            if path == "/v1/duck":
                if body == "down":
                    send(self, 200, {"ok": duck_down()})
                    return
                if body == "up":
                    send(self, 200, {"ok": duck_cancel(True)})
                    return
                send(self, 400, {"error": "duck"})
                return
            if path == "/v1/ctrl":
                if body not in ("pause", "play", "next", "prev", "stop"):
                    send(self, 400, {"error": "ctrl"})
                    return
                ctrl(body)
                send(self, 200, {"ok": True})
                return
            if path == "/v1/mark":
                if body not in ("like", "dislike"):
                    send(self, 400, {"error": "mark"})
                    return
                send(self, 200, {"ok": mark(body)})
                return
        except Exception as exc:
            log("post fail %s" % type(exc).__name__)
            send(self, 500, {"error": "music"})
            return
        send(self, 404, {"error": "not-found"})

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/v1/login/poll":
                login_id = parse_qs(parsed.query).get("id", [""])[0]
                if not ID_RE.match(login_id):
                    send(self, 400, {"error": "id"})
                    return
                send(self, 200, take_login(login_id))
                return
            if parsed.path == "/v1/playlists":
                person_id = parse_qs(parsed.query).get("id", [""])[0]
                rows = playlist_rows(person_id)
                if rows is None:
                    send(self, 404, {"error": "нет музыки"})
                    return
                send(self, 200, {"playlists": rows})
                return
            if parsed.path == "/v1/now":
                send(self, 200, now_state())
                return
            if parsed.path == "/v1/people":
                account = load_account()
                if not account:
                    send(self, 200, {"who": "", "people": []})
                    return
                send(self, 200, {
                    "who": account["id"],
                    "people": [{
                        "id": account["id"],
                        "name": account.get("name") or "Яндекс",
                        "login": account.get("login") or "",
                    }],
                })
                return
        except Exception as exc:
            log("get fail %s" % type(exc).__name__)
            send(self, 500, {"error": "music"})
            return
        send(self, 404, {"error": "not-found"})


def main():
    threading.Thread(target=account_loop, daemon=True).start()
    log("music listening %s" % PORT)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
