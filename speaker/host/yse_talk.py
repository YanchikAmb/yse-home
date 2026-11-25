# Ответы колонке. Спальня с галочкой «Локально» присылает уже разобранный текст (/v1/ask),
# иначе колонка присылает саму фразу (/v1/hear) и текст считает выбранное распознавание.
# Кто отвечает — settings/nn.txt: yandex, ha, openai, google. Время, температуру и погоду знаем сами.
import array
import asyncio
import datetime
import difflib
import io
import json
import math
import os
import queue
import re
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websockets

HOST = "0.0.0.0"
BASE = int(os.environ.get("YSE_BASE", "18765"))
PORT = BASE + 3
MUSIC_URL = "http://127.0.0.1:%d" % (BASE + 1)
HA_URL = os.environ.get("YSE_HA_URL", "http://192.168.209.15:8123").rstrip("/")


def demo_token():
    value = os.environ.get("YSE_DEMO_TOKEN", "")
    if not value:
        try:
            with open(os.environ.get("YSE_DEMO_FILE", "/storage/yse/demo.token"), encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError:
            value = ""
    return value


DEMO_TOKEN = demo_token()
VOICE = {
    "alisa": "shitova",
    "dmitry": "zahar",
    "svetlana": "oksana",
}
SPEAKER = ["/storage/yse/bin/yse-ssh", os.environ.get("YSE_SPEAKER", "192.168.209.105")]
ROOT = "/data/opt/yandex-station"
LIBVA = "/lib/x86_64-linux-gnu/libva.so.2.1400.0"
OWN_VOICE_URL = os.environ.get("YSE_VOICE_URL", "http://127.0.0.1:18952")
talk_lock = threading.Lock()


def log(text):
    print(text, flush=True)


def speaker_text(command):
    try:
        proc = subprocess.run(
            SPEAKER + [command], timeout=8, check=False,
            capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.decode("utf-8", "replace").strip()


def person_token():
    if os.environ.get("YSE_PLAY") == "http":
        return ""
    who = speaker_text("cat %s/settings/music.who" % ROOT)
    if len(who) != 8:
        return ""
    return speaker_text("cat %s/people/%s/token" % (ROOT, who))


page_key = {"value": ""}
ha_skip_until = 0.0
speaker_cache = {}


def speaker_cached(name, command, ttl=20):
    now = time.time()
    item = speaker_cache.get(name)
    if item and now - item[0] < ttl:
        return item[1]
    value = speaker_text(command)
    speaker_cache[name] = (now, value)
    return value


def push_http(path, data, content_type):
    if os.environ.get("YSE_PLAY") != "http":
        return
    ip = os.environ.get("YSE_SPEAKER", "")
    if not ip:
        return
    req = urllib.request.Request(
        "http://%s:18080%s" % (ip, path), data=data, method="POST",
        headers={"Content-Type": content_type},
    )
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except OSError:
        pass


MALE_VOICES = {"dmitry", "own:jarvis", "own:jarvis_new", "own:vihrov", "own:eugene"}


def pcm_rms(audio):
    """Громкость s16le. Тишина после своего же голоса не должна запускать ответ."""
    count = 0
    acc = 0
    for i in range(0, len(audio) - 1, 16):
        sample = int.from_bytes(audio[i:i + 2], "little", signed=True)
        acc += sample * sample
        count += 1
    if count == 0:
        return 0.0
    return (acc / count) ** 0.5


def missed_sentence():
    """Пустая фраза после слова. Род — по выбранному голосу."""
    voice = (speaker_settings().get("voice.txt") or "alisa").strip().lower()
    if voice in MALE_VOICES:
        return "Не расслышал."
    return "Не расслышала."


def publish_heard(phrase):
    text = (phrase or "").replace("\r", " ").replace("\n", " ").strip()
    if not text:
        text = "не расслышала"

    def work():
        if os.environ.get("YSE_PLAY") == "http":
            push_http("/api/heard", text.encode("utf-8"), "text/plain; charset=utf-8")
            return
        try:
            subprocess.run(
                SPEAKER + ["cat > %s/heard.txt" % ROOT],
                input=text.encode("utf-8"), timeout=8, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    threading.Thread(target=work, daemon=True).start()


def ha_post(path, body, content_type="application/json", timeout=4):
    """POST в компонент yse_station с ключом страницы колонки. None — HA недоступен или ключ не принят."""
    global ha_skip_until
    if time.time() < ha_skip_until:
        return None
    for _ in range(2):
        if not page_key["value"]:
            tok = os.environ.get("YSE_HA_TOKEN", "").strip()
            if not tok:
                folder = os.environ.get("YSE_SETTINGS", "").strip()
                if folder:
                    try:
                        tok = open(os.path.join(folder, "token"), "r", encoding="utf-8").read().strip()
                    except OSError:
                        tok = ""
            page_key["value"] = tok or speaker_text("cat %s/token" % ROOT)
        if not page_key["value"]:
            return None
        req = urllib.request.Request(HA_URL + path, data=body, method="POST", headers={
            "Content-Type": content_type, "X-Token": page_key["value"]})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                page_key["value"] = ""
                continue
            log("ha fail %s %s" % (path, exc.code))
            return None
        except (OSError, ValueError) as exc:
            log("ha fail %s %s" % (path, type(exc).__name__))
            return None
    log("ha key refused")
    ha_skip_until = time.time() + 600
    return None


def ha_ask(phrase, agent):
    """agent: builtin — только команды дома, default — помощник HA.
    Возвращает (узнал ли HA фразу, текст ответа) или (None, "") если HA недоступен."""
    data = ha_post("/api/yse_station/ask", json.dumps(
        {"text": phrase, "agent": agent, "language": "ru"}).encode("utf-8"))
    if data is None:
        return None, ""
    return bool(data.get("matched")), str(data.get("speech") or "")


SETTING_FILES = ("stt.txt", "nn.txt", "llm.url", "llm.model", "persona.txt", "wake.txt", "voice.txt",
                 "news.txt", "news.url", "news.model", "news.key")


def speaker_settings():
    folder = os.environ.get("YSE_SETTINGS", "").strip()
    if folder:
        out = {}
        for name in SETTING_FILES:
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as fh:
                    out[name] = " ".join(fh.read().split())[:300]
            except OSError:
                out[name] = ""
        return out
    raw = speaker_cached("settings", (
        "cd %s/settings && for f in %s; do printf '%%s=' $f; "
        "n=300; [ \"$f\" = persona.txt ] && n=3000; "
        "head -c $n $f 2>/dev/null | tr '\\n' ' '; echo; done"
        % (ROOT, " ".join(SETTING_FILES))), 5)
    out = {}
    for line in raw.splitlines():
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def stt_engine(settings=None):
    """T-one с 11.11 не используем: телефонная модель не слышит микрофон колонки."""
    engine = (settings if settings is not None else speaker_settings()).get("stt.txt") or "vosk-small"
    if engine == "tone":
        return "yandex"
    return engine


def voice_name():
    saved = speaker_settings().get("voice.txt") or "alisa"
    if saved.startswith("own:"):
        return "shitova"
    return VOICE.get(saved, saved or "shitova")


def persona_text():
    return " ".join((speaker_settings().get("persona.txt") or "").split())[:3000]


# Мужские имена и уменьшительные на -а/-я. Остальные на -а/-я считаем женскими.
MALE_NAMES = frozenset("""
никита илья кузьма фома лука данила гаврила савва добрыня
алеша леша миша гриша паша федя петя коля толя сеня ваня гоша кеша
лева степа сережа дима рома вова боря витя гена костя митя фима филя проша
""".split())
# По этим именам пол не выбрать.
AMBIGUOUS_NAMES = frozenset("саша женя валя шура слава".split())
# Женские имена не на -а/-я.
FEMALE_NAMES = frozenset("любовь нинель руфь эсфирь рахиль юдифь ассоль адель гузель айгуль гюзель николь".split())


def address_title(name):
    """«сэр» или «мэм» по имени. Пусто, если имени нет или пол по нему неясен."""
    first = ""
    for word in str(name or "").replace("ё", "е").lower().split():
        word = "".join(ch for ch in word if ch.isalpha())
        if word:
            first = word
            break
    if not first or first in AMBIGUOUS_NAMES:
        return ""
    if first in MALE_NAMES:
        return "сэр"
    if first in FEMALE_NAMES or first.endswith(("а", "я")):
        return "мэм"
    return "сэр"


def address_clause(person):
    """Короткая установка обращения. В характер не пишется: пол считается по имени."""
    if not person:
        return ""
    title = address_title(person[0])
    if not title:
        return ""
    return " Обращайся к собеседнику только «%s»." % title


def persona_for_yandex(phrase, fresh, person=None):
    """У Яндекса нет отдельного поля характера, весь текст запроса не длиннее 400 знаков.
    Характер добавляется только в первое сообщение сессии. Продолжение в те же 5 минут идёт без него."""
    note = address_clause(person).strip()
    note = (note + " ") if note else ""
    if not fresh:
        return note + phrase
    persona = persona_text()
    if not persona:
        return note + phrase
    lead = "Стиль, не цитируй его и не отвечай на него: "
    tail = ". " + note + "Вопрос: " + phrase
    room = 400 - len(lead) - len(tail)
    if room < 40:
        return note + phrase
    return lead + persona[:room] + tail


def clock_fields():
    now = speaker_clock()
    return now.strftime("%Y%m%dT%H%M%S"), str(int(now.timestamp()))


def speaker_clock():
    raw = speaker_text("date +%s")
    try:
        stamp = int(raw)
    except ValueError:
        return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
    return datetime.datetime.fromtimestamp(stamp, datetime.timezone(datetime.timedelta(hours=3)))


_ONES_M = "ноль один два три четыре пять шесть семь восемь девять".split()
_ONES_F = "ноль одна две три четыре пять шесть семь восемь девять".split()
_TEENS = "десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать шестнадцать семнадцать восемнадцать девятнадцать".split()
_TENS = " двадцать тридцать сорок пятьдесят".split()


def _num(n, feminine):
    ones = _ONES_F if feminine else _ONES_M
    if n < 10:
        return ones[n]
    if n < 20:
        return _TEENS[n - 10]
    ten, one = divmod(n, 10)
    if one == 0:
        return _TENS[ten - 2]
    return _TENS[ten - 2] + " " + ones[one]


def _unit(n, one, few, many):
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    tail = n % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def clock_sentence(moment):
    hour, minute = moment.hour, moment.minute
    if hour == 0 and minute == 0:
        return "Сейчас полночь."
    if hour == 12 and minute == 0:
        return "Сейчас полдень."
    hours = "%s %s" % (_num(hour, False), _unit(hour, "час", "часа", "часов"))
    if minute == 0:
        return "Сейчас %s ровно." % hours
    minutes = "%s %s" % (_num(minute, True), _unit(minute, "минута", "минуты", "минут"))
    return "Сейчас %s %s." % (hours, minutes)


def is_clock_question(phrase):
    text = " ".join(phrase.lower().replace("ё", "е").replace("?", " ").replace("!", " ").split())
    if any(word in text for word in ("будильник", "таймер", "через", "остал")):
        return False
    if " до " in (" " + text + " "):
        return False
    keys = (
        "который час", "сколько времени", "сколько сейчас времени",
        "какое сейчас время", "какое время", "текущее время",
        "скажи время", "подскажи время", "время сейчас", "сколько время",
        "во сколько сейчас",
    )
    return any(key in text for key in keys)


def answer_text(payload):
    voice = payload.get("voice_response") or {}
    speech = (voice.get("output_speech") or {}).get("text")
    if speech:
        return str(speech).strip()
    response = payload.get("response") or {}
    card = response.get("card") or {}
    if isinstance(card, dict) and card.get("text"):
        return str(card["text"]).strip()
    for card in response.get("cards") or []:
        if isinstance(card, dict) and card.get("text"):
            return str(card["text"]).strip()
    return ""


VINS_DEVICE = str(uuid.uuid5(uuid.NAMESPACE_URL, "yse-talk-%d" % BASE))
VINS_IDLE = 300
vins_state = {"token": "", "req": "", "sid": "", "at": 0.0}


async def vins_text(token, phrase):
    """Одна колонка для Яндекса и продолжение сессии: «ещё», «дальше» помнят прошлый ответ."""
    device = VINS_DEVICE
    client_time, stamp = clock_fields()
    request_id = str(uuid.uuid4())
    header = {"request_id": request_id}
    if (vins_state["req"] and vins_state["token"] == token
            and time.time() - vins_state["at"] < VINS_IDLE):
        header["prev_req_id"] = vins_state["req"]
        if vins_state["sid"]:
            header["session_id"] = vins_state["sid"]
    async with websockets.connect(
        "wss://voiceservices.yandex.net/uni.ws", open_timeout=8, max_size=4_000_000
    ) as ws:
        await ws.send(json.dumps({"event": {"header": {
            "messageId": str(uuid.uuid4()), "name": "SynchronizeState", "namespace": "System"
        }, "payload": {
            "accept_invalid_auth": True, "auth_token": token,
            "speechkitVersion": "3.28.0", "uuid": device
        }}}))
        await ws.send(json.dumps({"event": {"header": {
            "messageId": str(uuid.uuid4()), "name": "TextInput", "namespace": "Vins", "streamId": 1
        }, "payload": {
            "header": header,
            "application": {
                "app_id": "aliced", "app_version": "1.0",
                "os_version": "6.0", "platform": "Linux", "uuid": device,
                "lang": "ru-RU", "timezone": "Europe/Moscow",
                "client_time": client_time, "timestamp": stamp
            },
            "request": {"event": {"type": "text_input", "text": phrase[:400]},
                        "voice_session": True}
        }}}))
        for _ in range(8):
            try:
                msg = await asyncio.wait_for(ws.recv(), 5)
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            if isinstance(msg, bytes):
                continue
            data = json.loads(msg)
            header = (data.get("directive") or {}).get("header") or {}
            if header.get("name") != "VinsResponse":
                continue
            payload = (data.get("directive") or {}).get("payload") or {}
            vins_state.update(token=token, req=request_id, at=time.time(),
                              sid=str((payload.get("header") or {}).get("session_id") or ""))
            return answer_text(payload)
    return ""


def ogg_piece(blob):
    if len(blob) >= 8 and blob[4:8] == b"OggS":
        return blob[4:]
    if blob.startswith(b"OggS"):
        return blob
    if len(blob) > 4:
        return blob[4:]
    return b""


async def tts_ogg(token, text, voice):
    device = str(uuid.uuid4())
    audio = bytearray()
    async with websockets.connect(
        "wss://voiceservices.yandex.net/uni.ws", open_timeout=8, max_size=8_000_000
    ) as ws:
        await ws.send(json.dumps({"event": {"header": {
            "messageId": str(uuid.uuid4()), "name": "SynchronizeState", "namespace": "System"
        }, "payload": {
            "accept_invalid_auth": True, "auth_token": token,
            "speechkitVersion": "3.28.0", "uuid": device
        }}}))
        await ws.send(json.dumps({"event": {"header": {
            "messageId": str(uuid.uuid4()), "name": "Generate", "namespace": "TTS", "streamId": 1
        }, "payload": {
            "text": text[:500], "lang": "ru-RU", "voice": voice,
            "emotion": "good", "format": "audio/ogg;codecs=opus"
        }}}))
        for _ in range(200):
            try:
                msg = await asyncio.wait_for(ws.recv(), 4)
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            if isinstance(msg, bytes):
                audio += ogg_piece(msg)
                continue
            data = json.loads(msg)
            if "streamcontrol" in data and audio:
                break
            header = (data.get("directive") or {}).get("header") or {}
            if header.get("name") == "EventException":
                return b""
    return bytes(audio)


def music_post(path, body):
    req = urllib.request.Request(
        MUSIC_URL + path, data=body.encode("utf-8"), method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except (OSError, urllib.error.URLError):
        return


def music_ask(path, body, timeout=40):
    """(код ответа, json) службы музыки; (0, {}) — служба не ответила."""
    req = urllib.request.Request(MUSIC_URL + path, data=body.encode("utf-8"), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
        except ValueError:
            return exc.code, {}
    except (OSError, ValueError):
        return 0, {}


MUSIC_MINE = ("мою", "мой", "моя", "мое", "мне")
PLAY_VERB = re.compile(r"^(включи|включите|поставь|поставьте|запусти|запустите|играй|сыграй)\b")


def music_kind(phrase):
    """Как общее ухо Станции (ear.c act_on): "list" — плейлист/волна/исполнитель, "who" — «музыку Маши»,
    "like"/"dislike", "play" — «включи <что-то>», решается после HA."""
    t = " ".join(re.sub(r"[^\w\s-]", " ", phrase.lower().replace("ё", "е")).split())
    words = t.split()
    if not words or re.search(r"свет|телевизор|блютуз|будильник|громкост", t):
        return "", ""
    if len(words) <= 3 and (t.endswith("дизлайк") or "не нравится" in t):
        return "dislike", t
    if len(words) <= 3 and re.fullmatch(r"(поставь |мне )?(лайк|нравится)( пожалуйста)?", t):
        return "like", t
    mine = re.search(r"\b(мою|мой|моя|мое|моё|мне)\b", t) and "волн" not in t and re.search(r"музык|плейлист", t)
    if not re.search(r"\b(включи|включите|поставь|поставьте|запусти|запустите|играй|сыграй|давай)\b", t) \
            and not re.match(r"(музык|плейлист|волн)", t) and not mine:
        return "", ""
    m = re.search(r"\bмузык[уаи]\b(.*)", t)
    if m:
        name = [w for w in m.group(1).split() if w not in MUSIC_MINE and w != "пожалуйста"]
        if name:
            return "who", " ".join(name)
        if mine:
            return "mine", ""
        return "list", t
    if re.search(r"плейлист", t) and mine:
        skip = MUSIC_MINE + ("включи", "включите", "поставь", "поставьте", "запусти", "запустите",
                             "играй", "сыграй", "давай", "пожалуйста", "плейлист")
        rest = [w for w in t.split() if w not in skip]
        if not rest:
            return "mine", ""
    if re.search(r"плейлист|\bволн", t):
        return "list", t
    if PLAY_VERB.match(t) and len(words) >= 2:
        return "play", t
    return "", ""


def music_answer(kind, text, person=None):
    """Пустая строка — музыка пошла, говорить ничего не надо. Иначе текст для ответа."""
    if kind == "mine":
        if not person:
            return "Не узнала, кто говорит."
        code, _data = music_ask("/v1/who", person[0])
        if code != 200:
            return "Не нашла твою музыку."
        code, data = music_ask("/v1/playlist", "мне нравится")
        log("music mine %s %d %s" % (person[0], code, (data.get("title") or data.get("error") or "")[:40]))
        if code == 200 and data.get("ok"):
            return ""
        if code == 0:
            return "Музыка сейчас не отвечает."
        return "Не нашла твою музыку."
    if kind in ("like", "dislike"):
        music_ask("/v1/mark", kind)
        return ""
    if kind == "who":
        code, _data = music_ask("/v1/who", text)
        if code == 200:
            return ""
    code, data = music_ask("/v1/playlist", text)
    log("music %s %d %s" % (kind, code, (data.get("title") or data.get("error") or "")[:40]))
    if code == 200 and data.get("ok"):
        return ""
    if code == 0:
        return "Музыка сейчас не отвечает."
    if data.get("error") == "плейлист не найден":
        return "Сначала войдите в Яндекс Музыку на странице колонки."
    return "Не нашла такую музыку."


def play_ogg(ogg):
    env = os.environ.copy()
    if os.path.exists(LIBVA):
        env["LD_PRELOAD"] = LIBVA
    pcm = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"],
        input=ogg, capture_output=True, timeout=30, env=env,
    ).stdout
    return play_raw([pcm], 2)


def player_argv(channels):
    """Колонки на Linux слушают aplay по SSH. Макс принимает тот же PCM по HTTP."""
    if os.environ.get("YSE_PLAY") == "http":
        ip = os.environ.get("YSE_SPEAKER", "127.0.0.1")
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yse_pcm_push.py")
        return [sys.executable, script, "http://%s:18080/api/pcm" % ip, "48000", str(channels), "voice"]
    return SPEAKER + [("dev=plughw:0,0; grep -q softvol /etc/asound.conf 2>/dev/null && dev=default; "
                       "aplay -D \"$dev\" -f S16_LE -r 48000 -c %d -t raw -") % channels]


def play_raw(chunks, channels):
    """s16le 48 кГц кусками, пока они приходят; музыка приглушена всё это время."""
    started = time.time()
    sent = 0
    quiet = {"run": True}
    lock = threading.Lock()

    def keep_quiet():
        while True:
            time.sleep(3)
            with lock:
                if not quiet["run"]:
                    return
                music_post("/v1/duck", "down")

    music_post("/v1/duck", "down")
    threading.Thread(target=keep_quiet, daemon=True).start()
    threading.Thread(target=listen_barge, args=(quiet, lock), daemon=True).start()
    play = subprocess.Popen(player_argv(channels), stdin=subprocess.PIPE)
    with turn_lock:
        turn["procs"] = [play]
    try:
        for chunk in chunks:
            if turn["cut"]:
                raise BrokenPipeError
            play.stdin.write(chunk)
            play.stdin.flush()
            sent += len(chunk)
        play.stdin.close()
        while play.poll() is None:
            if turn["cut"]:
                break
            try:
                play.wait(timeout=0.4)
            except subprocess.TimeoutExpired:
                continue
        if turn["cut"] and play.poll() is None:
            play.kill()
            play.wait(timeout=5)
        log("pace %.1f %.1f" % (sent / (96000.0 * channels), time.time() - started))
        return 1 if turn["cut"] else (play.returncode or 0)
    except (BrokenPipeError, OSError):
        if play.poll() is None:
            play.kill()
        play.wait(timeout=5)
        return 1
    finally:
        with turn_lock:
            turn["procs"] = []
        with lock:
            quiet["run"] = False
            if not turn["cut"]:
                music_post("/v1/duck", "up")


turn = {"busy": False, "cut": False, "procs": []}
turn_lock = threading.Lock()


def clean_tail(seconds=2.0):
    """Последние секунды очищенного микрофона. Свой голос колонки из него уже вычтен."""
    try:
        proc = subprocess.run(
            SPEAKER + ["cat /data/opt/yandex-station/clean.ring"],
            capture_output=True, timeout=4,
        )
    except (OSError, subprocess.TimeoutExpired):
        return b""
    data = proc.stdout
    if len(data) < 16:
        return b""
    magic, cap, pos, _pad = struct.unpack_from("<IIII", data, 0)
    if magic != 0x524E4759 or cap < 16000 or pos < 16000:
        return b""
    if 16 + cap * 2 > len(data):
        return b""
    need = min(int(16000 * seconds), cap)
    out = bytearray()
    for i in range(need):
        at = (pos - need + i) & (cap - 1)
        out += data[16 + at * 2:18 + at * 2]
    return bytes(out)


def is_barge(said, wake):
    """Короткое «стоп» или слово пробуждения. Длинная фраза — это свой голос, его не берём."""
    text = " ".join((said or "").lower().replace("ё", "е").replace(",", " ").split())
    words = text.split()
    if not words or len(words) > 4:
        return False
    if is_stop(text):
        return True
    wake = " ".join((wake or "").lower().replace("ё", "е").split())
    return bool(wake) and wake in words


def listen_barge(active, lock):
    """Пока колонка говорит, раз в полторы секунды слушает «стоп» и слово пробуждения."""
    wake = (speaker_settings().get("wake.txt") or "").strip()
    while True:
        time.sleep(1.6)
        with lock:
            if not active["run"]:
                return
        if turn["cut"]:
            return
        try:
            said = hear_text(clean_tail())
        except Exception as exc:
            log("barge hear fail %s" % type(exc).__name__)
            continue
        if not is_barge(said, wake):
            continue
        log("barge hear %s" % said)
        stopped = is_stop(said)
        cut_turn()
        if stopped:
            music_post("/v1/duck", "up")
        else:
            speaker_text("echo %d > %s/hear.go" % (int(time.time() * 1000), ROOT))
        return


VOL_UP = re.compile(r"((сделай|сделайте|еще|чуть|немного|намного|гораздо|сильно|музыку|звук|по)\s+)*"
                    r"(громче|погромче|прибавь( звук| громкость)?|увеличь( звук| громкость)?)"
                    r"(\s+(музыку|звук|пожалуйста|еще))*")
VOL_DOWN = re.compile(r"((сделай|сделайте|еще|чуть|немного|намного|гораздо|сильно|музыку|звук|по)\s+)*"
                      r"(тише|потише|убавь( звук| громкость)?|уменьши( звук| громкость)?)"
                      r"(\s+(музыку|звук|пожалуйста|еще))*")


def volume_command(phrase):
    """«громче», «тише», «громкость 30», «громкость на 5» (шкала Алисы 1–10), «какая громкость».
    Возвращает ("ask", 0), ("set", процент), ("step", ±процент) или None."""
    t = " ".join(re.sub(r"[^\w\s%]", " ", phrase.lower().replace("ё", "е")).split())
    if "будильник" in t:
        return None
    t = words_to_digits(re.sub(r"\s*пожалуйста$", "", t))
    if re.search(r"(какая|какой|сколько).*громкост|громкость сейчас", t):
        return ("ask", 0)
    big = 20 if re.search(r"намного|гораздо|сильно", t) else 10
    if VOL_UP.fullmatch(t):
        return ("step", big)
    if VOL_DOWN.fullmatch(t):
        return ("step", -big)
    if not re.search(r"громкост|звук", t) or len(t.split()) > 7:
        return None
    if re.search(r"максим|на полную", t):
        return ("set", 100)
    if re.search(r"миним", t):
        return ("set", 10)
    m = re.search(r"(\d{1,3})\s*(%|процент\w*)?", t)
    if not m:
        return None
    n = int(m.group(1))
    if not m.group(2) and n <= 10:
        n *= 10
    return ("set", max(0, min(100, n)))


def volume_now():
    m = re.search(r"Playback (\d+) \[", speaker_text("amixer -c 0 sget Master"))
    return int(m.group(1)) * 100 // 255 if m else -1


def set_volume(percent):
    percent = max(0, min(100, int(percent)))
    mixer = (percent * 255 + 50) // 100
    speaker_text(
        "amixer -c 0 set Master %d >/dev/null 2>&1; echo 'volume %d' > %s/house.cmd; "
        "kill $(cat %s/volprev.pid 2>/dev/null) 2>/dev/null; "
        "dev=plughw:0,0; grep -q softvol /etc/asound.conf 2>/dev/null && dev=default; "
        "(aplay -D $dev -f S16_LE -r 16000 -c 1 -t raw %s/volprev.pcm >/dev/null 2>&1 & echo $! > %s/volprev.pid); true"
        % (mixer, mixer, ROOT, ROOT, ROOT, ROOT))
    log("volume %d" % percent)
    return percent


def commandd_did_volume():
    """«Громче»/«тише» меняет только штатная модель команд на колонке (commandd), сервер их не трогает.
    «Прибавь/убавь звук» штатная не знает — их делает сервер, если commandd только что не сработала."""
    out = speaker_text("date +%%s; date -r %s/command.log +%%s; tail -n 1 %s/command.log" % (ROOT, ROOT)).split("\n")
    try:
        fresh = int(out[0]) - int(out[1]) < 8
    except (IndexError, ValueError):
        return False
    return fresh and len(out) > 2 and out[2].startswith("volume ")


def volume_answer(phrase):
    """None — фраза не про громкость. "" — сделано молча (как у Алисы), иначе текст ответа."""
    got = volume_command(phrase)
    if not got:
        return None
    kind, value = got
    now = volume_now()
    if kind == "ask":
        return "Громкость %d процентов." % now if now >= 0 else "Не знаю, какая сейчас громкость."
    if kind == "step":
        if re.search(r"громче|тише", phrase.lower()) or commandd_did_volume():
            log("volume by commandd")
            return ""
        set_volume((now if now >= 0 else 30) + value)
        return ""
    set_volume(value)
    return ""


def cut_turn():
    with turn_lock:
        was = turn["busy"] and not turn["cut"]
        if was:
            turn["cut"] = True
            for proc in turn["procs"]:
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass
    if was:
        log("cut")
    return was


FOLLOW_WORDS = (
    "он", "она", "оно", "они", "его", "ее", "её", "ему", "ей", "им", "их", "нем", "ней",
    "это", "этот", "эта", "этого", "этом", "там", "тогда", "туда", "тот", "та", "те",
)
DIALOG_DIR = os.environ.get("YSE_DIALOG_DIR", "/storage/yse/dialog/%d" % BASE)
MEMORY_IDLE = int(os.environ.get("YSE_MEMORY_IDLE", "1800"))
GLUE_IDLE = 300
dialog = {"turns": [], "at": 0.0}
dialog_lock = threading.Lock()


def dialog_load():
    try:
        with open(os.path.join(DIALOG_DIR, "state.json"), encoding="utf-8") as fh:
            dialog.update(json.load(fh))
    except (OSError, ValueError):
        pass


def recent_turns(idle=MEMORY_IDLE):
    """Последний разговор: живёт MEMORY_IDLE секунд после последней реплики, переживает перезапуск."""
    if time.time() - dialog["at"] > idle:
        return []
    return dialog["turns"]


def remember(phrase, asked, answer, route, person, spent):
    now = time.time()
    turn_row = {"t": round(now, 1), "user": phrase[:400], "asked": asked[:400], "assistant": answer[:800],
                "route": route, "person": person[0] if person else "",
                "score": round(person[1], 3) if person else None}
    with dialog_lock:
        dialog["turns"] = (recent_turns() + [turn_row])[-20:]
        dialog["at"] = now
        try:
            os.makedirs(DIALOG_DIR, exist_ok=True)
            with open(os.path.join(DIALOG_DIR, "state.json.tmp"), "w", encoding="utf-8") as fh:
                json.dump(dialog, fh, ensure_ascii=False)
            os.replace(os.path.join(DIALOG_DIR, "state.json.tmp"), os.path.join(DIALOG_DIR, "state.json"))
            row = dict(turn_row, ms=int(spent * 1000),
                       time=datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"))
            with open(os.path.join(DIALOG_DIR, time.strftime("%Y-%m-%d") + ".jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            log("dialog save fail %s" % exc)


def with_context(phrase, follow):
    """Короткий вопрос с местоимением или «а …» после прошлого — для Яндекса склеиваем с ним."""
    words = phrase.lower().replace("ё", "е").replace(",", " ").replace("?", " ").split()
    turns = recent_turns(GLUE_IDLE)
    prev = turns[-1]["asked"] if turns and turns[-1]["route"] in ("yandex", "web") else ""
    if not prev or not words:
        return phrase
    short = len(words) <= 4
    leads = words[0] in ("а", "и", "ну", "еще", "ещё", "тогда")
    points = any(word in FOLLOW_WORDS for word in words)
    if (follow and (short or leads or points)) or leads or (short and points):
        return "%s. %s" % (prev.rstrip(". "), phrase)
    return phrase


HA_VERB = re.compile(r"^(включи|выключи|отключи|зажги|погаси|открой|закрой|запусти|останови|убавь|прибавь|"
                     r"сделай|поставь|подними|опусти|переключи)\w*")
HA_POINT = ("его", "ее", "её", "их", "обратно", "снова", "опять", "назад", "тоже", "там", "это", "ее")
HA_FILL = ("пожалуйста", "а", "и", "ну", "теперь", "давай")
HA_BACK = {"включи": "выключи", "выключи": "включи", "отключи": "включи", "зажги": "погаси", "погаси": "включи",
           "открой": "закрой", "закрой": "открой", "запусти": "останови", "останови": "запусти",
           "убавь": "прибавь", "прибавь": "убавь", "подними": "опусти", "опусти": "подними"}


def home_off(phrase):
    """«Выключи …» — команда дому. Если объекта нет, облако не должно отвечать, будто выключило."""
    words = [w for w in spoken(phrase).split() if w not in HA_FILL]
    return bool(words) and bool(re.match(r"^(выключи|отключи|погаси|закрой|останови)\w*", words[0]))


def ha_rewrite(phrase):
    """«Включи его обратно» после «выключи свет в кабинете» -> «включи свет в кабинете»."""
    words = [w for w in spoken(phrase).split() if w not in HA_FILL]
    back = bool(words) and words[0].startswith("верн")
    if not words or not any(w in HA_POINT for w in words) or not (back or HA_VERB.match(words[0])):
        return ""
    prev = ""
    for row in reversed(recent_turns(900)):
        asked = spoken(row["asked"])
        if row["route"] == "ha" and HA_VERB.match(asked) and not any(w in HA_POINT for w in asked.split()):
            prev = asked
            break
    verb = HA_VERB.match(prev)
    if not verb:
        return ""
    target = prev[verb.end():].strip()
    if not target:
        return ""
    if back:
        stem = verb.group(1)
        words[0] = HA_BACK.get(stem, "")
        if not words[0]:
            return ""
    rest = [w for w in words[1:] if w not in HA_POINT and w not in target.split()]
    return " ".join([words[0], target] + rest)


HA_JOIN = re.compile(r"\s*(?:,|\bа также\b|\bа потом\b|\bпотом\b|\bзатем\b|\bи\b)\s*")


def ha_parts(phrase):
    """«Открой ворота и выключи свет» -> две команды: встроенный агент HA понимает только по одной.
    Кусок без глагола («…и в коридоре») остаётся при предыдущей команде."""
    parts = []
    for piece in HA_JOIN.split(phrase.strip()):
        words = [w for w in piece.lower().replace("ё", "е").split() if w not in HA_FILL]
        if not words:
            continue
        if not parts or HA_VERB.match(words[0]):
            parts.append(" ".join(words))
        else:
            parts[-1] += " и " + " ".join(words)
    return parts if len(parts) > 1 and all(HA_VERB.match(p) for p in parts) else [phrase]


def ha_home(phrase):
    parts = ha_parts(phrase)
    if len(parts) == 1:
        return ha_one(phrase)
    said = []
    missed = []
    for part in parts:
        matched, text, _asked = ha_one(part)
        if matched is None:
            return None, "", phrase
        if matched:
            said.append(text or "Готово.")
        else:
            missed.append(part)
    log("ha split %d, missed %d" % (len(parts), len(missed)))
    if not said:
        return False, "", phrase
    if missed:
        said.append("Не поняла: %s." % ", ".join(missed))
    return True, " ".join(said), phrase


def ha_one(phrase):
    alt = ha_rewrite(phrase)
    for text in ([alt] if alt else []) + [phrase]:
        matched, said = ha_ask(text, "builtin")
        if matched is None:
            return None, "", phrase
        if matched:
            if text != phrase:
                log("ha follow: %s" % text)
            return True, said, text
    return False, "", phrase


FRESH = re.compile(
    r"новост|курс\w* (доллар|евро|юан|валют|рубл|биткоин|акци)|котировк|биткоин|бирж|"
    r"сегодня|вчера|на этой неделе|в этом году|в прошлом году|последн|свеж|недавн|"
    r"кто выиграл|выиграл|счет матча|матч|чемпионат|турнир|олимпиад|"
    r"сколько стоит|цена|цены|подорож|пробк|расписани|афиш|в кино|премьер|вышел|вышла|выйдет|релиз|"
    r"президент|выбор|санкци|войн|202\d|случилось|происходит|"
    r"^(кто так|когда (родил|умер|был|была|началась|закончилась)|где наход|сколько (лет|жител|весит)|"
    r"какая столица|столица|какой высоты|расстояние|сколько километр)")
UNSURE = ("поиск", "не имею доступа", "нет доступа к интернет", "не могу знать", "мои знания",
          "на момент моего", "не располагаю", "у меня нет актуальн", "не могу проверить", "не знаю")


def needs_web(phrase):
    """Маленькая локальная модель не знает свежего и путает факты: такое отвечает Яндекс (у него поиск)."""
    return bool(FRESH.search(phrase.lower().replace("ё", "е")))


NUM_WORDS = dict(
    [(w, i) for i, w in enumerate("ноль один два три четыре пять шесть семь восемь девять".split())] +
    [("одна", 1), ("одну", 1), ("две", 2)] +
    [(w, 10 + i) for i, w in enumerate(_TEENS)] +
    [(w, 20 + 10 * i) for i, w in enumerate(("двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят",
                                              "семьдесят", "восемьдесят", "девяносто"))] +
    [("сто", 100)])


def words_to_digits(text):
    out = []
    for word in text.split():
        value = NUM_WORDS.get(word)
        if value is None:
            out.append(word)
        elif out and out[-1].isdigit() and int(out[-1]) % 10 == 0 and int(out[-1]) >= 20 and value < 10:
            out[-1] = str(int(out[-1]) + value)
        else:
            out.append(str(value))
    return " ".join(out)


def alarm_when(text, now):
    """Время будильника из фразы или None. «через 20 минут», «на 7 30», «на восемь вечера», «завтра в 6»."""
    t = words_to_digits(text.replace("полчаса", "30 минут").replace("полтора часа", "90 минут"))
    if re.search(r"на \d+ минут", t) and not re.search(r"\d+\s*(час|[:.]\s*\d)", t):
        t = t.replace("на ", "через ", 1)
    if "через" in t:
        minutes = 0
        hours = re.search(r"(\d+)?\s*час", t)
        mins = re.search(r"(\d+)\s*мин", t)
        if hours:
            minutes += 60 * int(hours.group(1) or 1)
        if mins:
            minutes += int(mins.group(1))
        if minutes:
            return now + datetime.timedelta(minutes=minutes)
    m = re.search(r"(?:на|в|к)\s+(\d{1,2})(?:\s*[:.]\s*|\s+(?:час\w*\s+)?)?(\d{1,2})?\s*(?:минут\w*)?\s*"
                  r"(утра|вечера|дня|ночи)?", t)
    if not m:
        return None
    hour, minute, part = int(m.group(1)), int(m.group(2) or 0), m.group(3) or ""
    if part in ("вечера", "дня") and hour < 12:
        hour += 12
    if part == "ночи" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if "послезавтра" in t:
        at += datetime.timedelta(days=2)
    elif "завтра" in t or at <= now:
        at += datetime.timedelta(days=1)
    return at


def speaker_alarms():
    raw = speaker_text("cat %s/alarms.txt 2>/dev/null" % ROOT)
    now = time.time()
    return sorted(int(x) for x in raw.split() if x.isdigit() and int(x) > now)


def column_json(method, path, payload=None, timeout=4):
    """Страница Макса. None — это не она или колонка не ответила."""
    if os.environ.get("YSE_PLAY") != "http":
        return None
    ip = os.environ.get("YSE_SPEAKER", "")
    if not ip:
        return None
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request("http://%s:18080%s" % (ip, path), data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw or "{}")
    except (OSError, ValueError):
        return None


def alarm_melody(phrase):
    """Своя мелодия будильника из Яндекс Музыки. Пустая строка — фраза не про мелодию."""
    if re.search(r"стандарт|обычн|встроен|маримб", phrase):
        code, data = music_ask("/v1/alarm-builtin", "")
        if code == 200 and data.get("ok"):
            return "Будильник будет со стандартной мелодией."
        return "Не смогла вернуть стандартную мелодию."
    query = re.sub(
        r"\b(будил\w*|мелоди\w*|постав\w*|поставь|сделай|включи|хочу|давай|на|под|с)\b",
        " ",
        phrase,
    )
    query = re.sub(r"\b\d{1,2}([:.]\d{2})?\b", " ", query)
    query = re.sub(r"\b(утра|вечера|утром|вечером|часа|часов|час|минуты|минут|завтра|сегодня)\b", " ", query)
    query = " ".join(query.split())
    if len(query) < 2:
        return "Назови мелодию для будильника."
    code, data = music_ask("/v1/alarm-search", query)
    tracks = data.get("tracks") if code == 200 else None
    if data.get("error"):
        return data["error"] if isinstance(data["error"], str) else "Поиск не ответил."
    if not tracks:
        return "Не нашла такую мелодию в Яндекс Музыке."
    code, picked = music_ask("/v1/alarm-track", str(tracks[0].get("id") or ""), timeout=90)
    if code != 200 or not picked.get("ok"):
        return picked.get("error") or "Не смогла поставить эту мелодию."
    return "Будильник будет с мелодией %s." % (picked.get("label") or query)


def timer_seconds(text):
    """Срок таймера в секундах. «на 5» без единиц — минуты, как у Яндекса."""
    t = words_to_digits(text.replace("полчаса", "30 минут").replace("полминуты", "30 секунд"))
    seconds = 0
    hours = re.search(r"(\d+)\s*час", t)
    mins = re.search(r"(\d+)\s*мин", t)
    secs = re.search(r"(\d+)\s*сек", t)
    if hours:
        seconds += int(hours.group(1)) * 3600
    if mins:
        seconds += int(mins.group(1)) * 60
    if secs:
        seconds += int(secs.group(1))
    if seconds == 0 and not re.search(r"час|мин|сек", t):
        bare = re.search(r"\b(?:на|через)\s+(\d+)\b", t)
        if bare:
            seconds = int(bare.group(1)) * 60
    if seconds <= 0 or seconds > 24 * 3600:
        return 0
    return seconds


def timer_said(seconds):
    if seconds < 60:
        return "%d %s" % (seconds, _unit(seconds, "секунду", "секунды", "секунд"))
    if seconds % 60 == 0:
        return span_words(seconds)
    minutes, secs = divmod(seconds, 60)
    return "%d %s %d %s" % (
        minutes, _unit(minutes, "минуту", "минуты", "минут"),
        secs, _unit(secs, "секунду", "секунды", "секунд"))


def timer_left_words():
    raw = speaker_text("cat %s/timer.txt 2>/dev/null" % ROOT).strip()
    if not raw.isdigit():
        return ""
    left = int(raw) - int(time.time())
    if left <= 0:
        return ""
    return timer_said(left)


def timer_command(phrase):
    """Таймер на колонке. На Мини 2 циферблат считает назад, в конце — писк, не мелодия будильника."""
    t = " ".join(phrase.lower().replace("ё", "е").replace(",", " ").replace("?", " ").split())
    if "таймер" not in t:
        return ""
    if re.search(r"(отмени|убери|удали|выключи|сбрось|отключи|сними|останови)\w*", t):
        speaker_text("rm -f %s/timer.txt /tmp/timer.ring; touch %s/alarm.stop" % (ROOT, ROOT))
        log("timer clear")
        return "Таймер отменён."
    if re.search(r"(сколько|остал|осталось|какой|когда)\w*", t):
        left = timer_left_words()
        return "На таймере осталось %s." % left if left else "Таймер не стоит."
    seconds = timer_seconds(t)
    if not seconds:
        return "На сколько поставить таймер?"
    stamp = int(time.time()) + seconds
    speaker_text("echo %d > %s/timer.txt" % (stamp, ROOT))
    log("timer set %d" % seconds)
    return "Таймер на %s." % timer_said(seconds)


WEEKDAY_STEMS = ("понедельн", "вторн", "сред", "четверг", "пятниц", "суббот", "воскресен")
WEEKDAY_EVERY = ("по понедельникам", "по вторникам", "по средам", "по четвергам", "по пятницам", "по субботам",
                 "по воскресеньям")
WEEKDAY_ON = ("в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье")


def alarm_days(t):
    """(days, weekday): days — семь 0/1 с понедельника для повтора, weekday — день разового «в пятницу»."""
    if re.search(r"будн|рабоч\w* дн", t):
        return "1111100", None
    if re.search(r"выходн", t):
        return "0000011", None
    if re.search(r"кажд\w* (день|утро|вечер|ночь)|ежедневн", t):
        return "1111111", None
    days = ["0"] * 7
    for i, stem in enumerate(WEEKDAY_STEMS):
        if re.search(r"\b" + stem, t):
            days[i] = "1"
    if "1" not in days:
        return "", None
    if re.search(r"\bпо\b|кажд", t) or days.count("1") > 1:
        return "".join(days), None
    return "", days.index("1")


def repeat_words(days):
    if days == "1111111":
        return "каждый день"
    if days == "1111100":
        return "по будням"
    if days == "0000011":
        return "по выходным"
    picked = [WEEKDAY_EVERY[i] for i, c in enumerate(days) if c == "1"]
    if len(picked) == 1:
        return picked[0]
    return ", ".join(picked[:-1]) + " и " + picked[-1].replace("по ", "")


def clock_words(hour, minute):
    hours = "%s %s" % (_num(hour, False), _unit(hour, "час", "часа", "часов"))
    if minute:
        return "%s %s %s" % (hours, _num(minute, True), _unit(minute, "минута", "минуты", "минут"))
    return hours + " ровно"


def alarm_api(payload=None):
    """Будильники нашей колонки через её страницу. None — колонка не ответила."""
    ip = os.environ.get("YSE_SPEAKER", "")
    for _ in range(2):
        if not page_key["value"]:
            page_key["value"] = speaker_text("cat %s/token" % ROOT)
        if not ip or not page_key["value"]:
            return None
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request("http://%s:18080/api/alarms" % ip, data=data,
                                     method="GET" if data is None else "POST",
                                     headers={"Content-Type": "application/json", "X-Token": page_key["value"]})
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                return json.loads(resp.read().decode("utf-8", "replace") or "{}").get("alarms") or []
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                page_key["value"] = ""
                continue
            log("alarm api %s" % exc.code)
            return None
        except (OSError, ValueError) as exc:
            log("alarm api %s" % type(exc).__name__)
            return None
    return None


def alarm_row_words(row, now):
    """«семь часов тридцать минут по будням» для строки из /api/alarms."""
    hour, minute = (int(x) for x in row["time"].split(":"))
    said = clock_words(hour, minute)
    if row.get("days", "-") != "-":
        return "%s %s" % (said, repeat_words(row["days"]))
    at = datetime.datetime.fromtimestamp(int(row.get("at") or 0), now.tzinfo)
    if at.date() == now.date() + datetime.timedelta(days=1):
        return said + " завтра"
    if at.date() != now.date():
        return "%s %s" % (said, WEEKDAY_ON[at.weekday()])
    return said


def alarm_pick(rows, t, now):
    """Будильники, о которых фраза: по часам и повтору. Если ни то ни другое не названо — пусто."""
    days, weekday = alarm_days(t)
    clock = re.search(r"\d", words_to_digits(t)) and alarm_when(t, now)
    if not clock and not days and weekday is None:
        return []
    picked = []
    for row in rows:
        if clock and row["time"] != clock.strftime("%H:%M"):
            continue
        if days and row.get("days") != days:
            continue
        if weekday is not None and (row.get("days", "-") != "-" or datetime.datetime.fromtimestamp(
                int(row.get("at") or 0), now.tzinfo).weekday() != weekday):
            continue
        picked.append(row)
    return picked


def alarm_schedule(t):
    """Будильники нашей колонки с повтором. Пустая строка — фраза не про будильник."""
    now = speaker_clock()
    rows = alarm_api()
    if rows is None:
        return "Колонка не ответила, будильник не изменён."
    if re.search(r"(отмени|убери|удали|сбрось|сними|выключи|отключи)\w*", t):
        off = bool(re.search(r"(выключи|отключи)\w*", t)) and not re.search(r"\bвсе\b", t)
        ringing = speaker_text("test -f /tmp/alarm.ring && echo yes; touch %s/alarm.stop" % ROOT) == "yes"
        if ringing and not re.search(r"\d|\bвсе\b|будн|выходн|кажд|\bпо\b", words_to_digits(t)):
            log("alarm hush")
            return "Будильник выключен."
        if re.search(r"\bвсе\b", t):
            alarm_api({"op": "clear"})
            log("alarm clear")
            return alarm_sentence("clear")
        live = [r for r in rows if r.get("on")]
        picked = alarm_pick(live if off else rows, t, now)
        if not picked and len(live if off else rows) == 1 and not re.search(r"\d", words_to_digits(t)):
            picked = live if off else rows
        if not picked:
            if not rows:
                return "Будильников нет."
            return "Какой будильник? Есть %s." % ", ".join(alarm_row_words(r, now) for r in rows[:4])
        for row in picked:
            alarm_api({"op": "off" if off and row.get("days", "-") != "-" else "del", "id": row["id"]})
        log("alarm %s %d" % ("off" if off else "del", len(picked)))
        turned_off = off and picked[0].get("days", "-") != "-"
        if len(picked) == 1:
            return "Будильник на %s %s." % (alarm_row_words(picked[0], now), "выключен" if turned_off else "удалён")
        return "Будильники на %s %s." % (", ".join(alarm_row_words(r, now) for r in picked),
                                          "выключены" if turned_off else "удалены")
    if re.search(r"(какие|какой|когда|во сколько|на сколько|на какое время|есть ли|список)\b.*будильник"
                 r"|будильник\w* (стоит|стоят|поставлен)", t):
        live = [r for r in rows if r.get("on")]
        if not live:
            return "Будильников нет." if not rows else "Все будильники выключены."
        return "Будильник на %s." % ", ".join(alarm_row_words(r, now) for r in live[:4])
    if re.search(r"\bвключи\w*", t):
        sleeping = [r for r in rows if not r.get("on")]
        picked = alarm_pick(sleeping, t, now)
        if not picked and len(sleeping) == 1 and not re.search(r"\d", words_to_digits(t)):
            picked = sleeping
        if picked:
            for row in picked:
                alarm_api({"op": "on", "id": row["id"]})
            return "Будильник на %s включён." % alarm_row_words(picked[0], now)
    tone = alarm_melody(t) + " " if "мелоди" in t else ""
    at = alarm_when(t, now)
    if at is None:
        return tone.strip() if tone else alarm_sentence("none")
    days, weekday = alarm_days(t)
    if days:
        got = alarm_api({"when": at.strftime("%H:%M"), "days": days})
        if got is None:
            return "Колонка не ответила, будильник не поставлен."
        log("alarm set %s %s" % (at.strftime("%H:%M"), days))
        return tone + "Будильник поставлен на %s %s." % (clock_words(at.hour, at.minute), repeat_words(days))
    if weekday is not None:
        at = at.replace(year=now.year, month=now.month, day=now.day)
        at += datetime.timedelta(days=(weekday - now.weekday()) % 7)
        if at <= now:
            at += datetime.timedelta(days=7)
    stamp = int(at.timestamp())
    if alarm_api({"stamp": str(stamp)}) is None:
        return "Колонка не ответила, будильник не поставлен."
    log("alarm set %s" % at.strftime("%d.%m %H:%M"))
    return tone + alarm_sentence(str(stamp))


def alarm_volume_phrase(phrase):
    """Громкость звонка, не музыки. ("ask", 0), ("set", процент) или ("step", ±процент)."""
    t = " ".join(re.sub(r"[^\w\s%]", " ", phrase.lower().replace("ё", "е")).split())
    if "будильник" not in t:
        return None
    about = re.search(r"громч|громк|тише|потише", t) or re.search(r"максимальн|минимальн|на полную|на всю", t)
    if not about or (re.search(r"недел", t) and not re.search(r"громч|громк|тише", t)):
        return None
    t = words_to_digits(re.sub(r"\s*пожалуйста$", "", t))
    if re.search(r"(какая|какой|сколько).*громк", t):
        return ("ask", 0)
    big = 20 if re.search(r"намного|гораздо|сильно", t) else 10
    if re.search(r"громче|погромче|прибавь|увеличь", t):
        return ("step", big)
    if re.search(r"тише|потише|убавь|уменьши", t):
        return ("step", -big)
    if re.search(r"максим|на полную|на всю", t):
        return ("set", 100)
    if re.search(r"миним", t):
        return ("set", 5)
    match = re.search(r"(\d{1,3})\s*(%|процент\w*)?", t)
    if not match:
        return ("ask", 0)
    number = int(match.group(1))
    if not match.group(2) and number <= 10:
        number *= 10
    return ("set", max(5, min(100, number)))


def alarm_page(method, path, body=None, content_type="text/plain; charset=utf-8"):
    """Страница колонки. Тот же ключ, что у ползунка громкости будильника."""
    ip = os.environ.get("YSE_SPEAKER", "")
    for _ in range(2):
        if not page_key["value"]:
            page_key["value"] = speaker_text("cat %s/token" % ROOT)
        if not ip or not page_key["value"]:
            return None
        data = None if body is None else body.encode("utf-8")
        headers = {"X-Token": page_key["value"]}
        if data is not None:
            headers["Content-Type"] = content_type
        req = urllib.request.Request("http://%s:18080%s" % (ip, path), data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                return json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                page_key["value"] = ""
                continue
            log("alarm page %s %s" % (path, exc.code))
            return None
        except (OSError, ValueError) as exc:
            log("alarm page %s %s" % (path, type(exc).__name__))
            return None
    return None


def alarm_volume_now():
    data = alarm_page("GET", "/api/alarm-tone")
    if isinstance(data, dict) and isinstance(data.get("volume"), int):
        return max(5, min(100, data["volume"]))
    raw = speaker_text("cat %s/settings/alarm.volume 2>/dev/null" % ROOT).strip()
    try:
        number = int(raw)
    except ValueError:
        number = 40
    return max(5, min(100, number))


def alarm_volume_apply(got):
    kind, value = got
    now = alarm_volume_now()
    if kind == "ask":
        return "Громкость будильника %d процентов." % now
    if kind == "step":
        value = now + value
    value = max(5, min(100, int(value)))
    wrote = alarm_page("POST", "/api/alarm-tone/volume", str(value))
    if not (isinstance(wrote, dict) and wrote.get("ok")):
        speaker_text("printf '%d\\n' > %s/settings/alarm.volume" % (value, ROOT))
    log("alarm volume %d" % value)
    return "Громкость будильника %d процентов." % value


def alarm_command(phrase):
    """Будильник целиком на сервере: колонка с распознаванием на сервере сама фразу не разбирает."""
    t = " ".join(phrase.lower().replace("ё", "е").replace(",", " ").replace("?", " ").split())
    if "будильник" not in t and "разбуди" not in t:
        return ""
    volume = alarm_volume_phrase(phrase)
    if volume:
        return alarm_volume_apply(volume)
    http_room = os.environ.get("YSE_PLAY") == "http"
    if not http_room:
        return alarm_schedule(t)
    if re.search(r"(отмени|убери|удали|выключи|сбрось|отключи|сними)\w*.*будильник", t):
        if http_room:
            column_json("POST", "/api/alarms", {"clear": True})
        else:
            speaker_text("rm -f %s/alarms.txt; touch %s/alarm.stop" % (ROOT, ROOT))
        log("alarm clear")
        return alarm_sentence("clear")
    if re.search(r"(какие|какой|когда|во сколько|на сколько|на какое время|есть ли)\b.*будильник|будильник\w* (стоит|поставлен)", t):
        if http_room:
            data = column_json("GET", "/api/alarms") or {}
            items = []
            for row in data.get("alarms") or []:
                if isinstance(row, dict):
                    at = int(row.get("at") or 0) // 1000
                    if at > time.time():
                        items.append(at)
        else:
            items = speaker_alarms()
        if not items:
            return "Будильников нет."
        zone = speaker_clock().tzinfo
        said = []
        for stamp in items[:3]:
            at = datetime.datetime.fromtimestamp(stamp, zone)
            said.append("%s %s" % (_num(at.hour, False), ("%s" % _num(at.minute, True)) if at.minute else "ровно"))
        return "Будильник на %s." % ", ".join(said)
    if "мелоди" in t:
        said_tone = alarm_melody(t)
        at = alarm_when(t, speaker_clock())
        if at is None:
            return said_tone
        stamp = int(at.timestamp())
        if http_room:
            column_json("POST", "/api/alarms", {"at": stamp * 1000, "label": at.strftime("%H:%M")})
        else:
            speaker_text("echo %d >> %s/alarms.txt" % (stamp, ROOT))
        log("alarm set %s" % at.strftime("%d.%m %H:%M"))
        return said_tone + " " + alarm_sentence(str(stamp))
    at = alarm_when(t, speaker_clock())
    if at is None:
        return alarm_sentence("none")
    stamp = int(at.timestamp())
    if http_room:
        column_json("POST", "/api/alarms", {"at": stamp * 1000, "label": at.strftime("%H:%M")})
    else:
        speaker_text("echo %d >> %s/alarms.txt" % (stamp, ROOT))
    log("alarm set %s" % at.strftime("%d.%m %H:%M"))
    return alarm_sentence(str(stamp))


VOICES_FILE = os.environ.get("YSE_VOICES_FILE", "/storage/yse/people/voices.json")
# Косинус WeSpeaker на колонке: свой голос 0.43–0.64, чужой синтез 0.17. 0.5 отсекал хозяина.
VOICE_MATCH = float(os.environ.get("YSE_VOICE_MATCH", "0.36"))
ENROLL_LINES = ("включи свет на кухне", "какая завтра будет погода", "расскажи мне что-нибудь интересное")
enroll = {"step": "", "name": "", "embs": [], "at": 0.0}


def voice_embed(pcm):
    """Отпечаток голоса фразы из yse-voice (WeSpeaker). None — коротко или сервис недоступен."""
    if len(pcm) < 32000:
        return None
    req = urllib.request.Request(OWN_VOICE_URL + "/v1/speaker/embed", data=pcm[:16000 * 2 * 15],
                                 headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read())["embedding"]
    except (OSError, ValueError, KeyError) as exc:
        log("voice embed fail %s" % type(exc).__name__)
        return None


def voice_profiles():
    try:
        with open(VOICES_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def voice_save(profiles):
    os.makedirs(os.path.dirname(VOICES_FILE), exist_ok=True)
    with open(VOICES_FILE + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(profiles, fh, ensure_ascii=False)
    os.replace(VOICES_FILE + ".tmp", VOICES_FILE)


def unit(vector):
    norm = sum(x * x for x in vector) ** 0.5 or 1.0
    return [x / norm for x in vector]


def voice_bank(item):
    """Отдельные фразы знакомства. Старая запись хранит только их среднее."""
    phrases = item.get("phrases") or []
    if phrases:
        return phrases
    mean = item.get("emb") or []
    return [mean] if mean else []


def voice_score(emb, item):
    """Лучшее совпадение с любой фразой, как три отпечатка у Яндекса, а не одно размытое среднее."""
    best = None
    for vec in voice_bank(item):
        if len(vec) != len(emb):
            continue
        score = sum(a * b for a, b in zip(emb, vec))
        if best is None or score > best:
            best = score
    return best


def identify(emb):
    """(имя, сходство) ближайшего знакомого голоса, даже ниже порога. None — отпечатка нет."""
    if not emb:
        return None
    best = None
    for name, item in voice_profiles().items():
        score = voice_score(emb, item)
        if score is None:
            continue
        if best is None or score > best[1]:
            best = (name, score)
    if best:
        log("voice best %s %.2f%s" % (best[0], best[1], "" if best[1] >= VOICE_MATCH else " below"))
    return best


def spoken_name(t):
    t = re.sub(r"^(меня зовут|зовут|мое имя|моё имя|я|это)\s+", "", t.strip())
    words = [w for w in t.split() if w.isalpha()][:2]
    return " ".join(w.capitalize() for w in words)


def enroll_clear():
    enroll.update(step="", name="", embs=[], nudge=0)


def voice_dialog(phrase, emb, person, follow=False):
    """Знакомство как у Алисы: «запомни мой голос» -> имя -> три фразы за мной -> отпечаток в VOICES_FILE.
    Имя и повторы берутся из окна продолжения. Новое слово снаружи это окно не перехватывает."""
    t = " ".join(phrase.lower().replace("ё", "е").replace(",", " ").replace(".", " ").split())
    if enroll["step"] and time.time() - enroll["at"] > 120:
        enroll_clear()
    if enroll["step"]:
        enroll["at"] = time.time()
        if re.search(r"^(отмена|отмени|хватит|стоп|не надо)", t):
            enroll_clear()
            return "Хорошо, не запоминаю."
        if enroll["step"] == "name":
            if not follow:
                enroll_clear()
                return ""
            name = spoken_name(t)
            if not name or name.lower() in ("погода", "время", "час", "будильник"):
                return "Не расслышала имя. Как тебя зовут?"
            enroll.update(step="0", name=name, nudge=0)
            return "Приятно познакомиться, %s. Повтори за мной: %s." % (name, ENROLL_LINES[0])
        if not enroll_repeat(phrase):
            if not follow:
                enroll_clear()
                return ""
            return "Повтори ещё раз: %s." % enroll_line()
        if emb:
            enroll["embs"].append(emb)
        step = int(enroll["step"]) + 1
        enroll["nudge"] = 0
        if step < len(ENROLL_LINES):
            enroll["step"] = str(step)
            return "Теперь: %s." % ENROLL_LINES[step]
        name, embs = enroll["name"], enroll["embs"]
        enroll_clear()
        fresh = [unit(item) for item in embs if item]
        if len(fresh) < 2:
            return "Не получилось расслышать голос. Давай попробуем ещё раз позже."
        profiles = voice_profiles()
        old = profiles.get(name) or {}
        phrases = [unit(item) for item in voice_bank(old)] + fresh
        phrases = phrases[-6:]
        mean = unit([sum(col) / len(phrases) for col in zip(*phrases)])
        profiles[name] = {
            "emb": [round(x, 5) for x in mean],
            "phrases": [[round(x, 5) for x in item] for item in phrases],
            "n": len(phrases),
            "at": int(time.time()),
            "ha": old.get("ha", ""),
        }
        voice_save(profiles)
        log("voice enrolled %s from %d phrases" % (name, len(phrases)))
        return "Готово, %s. Теперь я узнаю тебя по голосу." % name
    if re.search(r"(запомни|запиши|выучи)\w* (мой голос|меня)|познакомимся|познакомься|знакомься|давай знакомиться", t):
        named = re.search(r"меня зовут (\w+(?: \w+)?)", t)
        if named:
            enroll.update(step="0", name=spoken_name(named.group(1)), embs=[], nudge=0, at=time.time())
            return "Приятно познакомиться, %s. Повтори за мной: %s." % (enroll["name"], ENROLL_LINES[0])
        enroll.update(step="name", name="", embs=[], nudge=0, at=time.time())
        return "Давай познакомимся. Как тебя зовут?"
    if re.search(r"забудь (мой голос|меня)", t):
        if not person:
            return "Я тебя пока не узнаю по голосу."
        profiles = voice_profiles()
        profiles.pop(person[0], None)
        voice_save(profiles)
        return "Хорошо, %s, я забыла твой голос." % person[0]
    if re.search(r"^(как меня зовут|кто я|ты меня узнаешь|узнаешь меня|кто со мной говорит|ты знаешь кто я)", t):
        if person:
            return "Ты %s." % person[0]
        return "Пока не узнаю. Скажи: запомни мой голос, и мы познакомимся."
    return ""


def tell_ha_speaker(name, score, ha_id=""):
    """Кто говорит — в сенсор колонки в HA. Неузнанный голос тоже пишется, иначе сенсор молчит."""
    body = {"speaker": {"name": name, "person": ha_id, "score": round(score, 3)}}

    def work():
        data = ha_post("/api/yse_station/people", json.dumps(body, ensure_ascii=False).encode("utf-8"))
        log("voice ha %s %.2f %s" % (name, score, "ok" if data is not None else "fail"))

    threading.Thread(target=work, daemon=True).start()


def voices_view():
    """Для страницы колонки: знакомые голоса, люди HA, подключён ли HA к этой колонке."""
    data = ha_post("/api/yse_station/people", b"{}")
    voices = [{"name": name, "phrases": int(item.get("n", 0)), "at": int(item.get("at", 0)), "ha": item.get("ha", "")}
              for name, item in sorted(voice_profiles().items())]
    return {"voices": voices, "persons": (data or {}).get("persons", []), "ha": data is not None,
            "enroll": bool(enroll["step"])}


def voices_change(body):
    op, name = str(body.get("op") or ""), str(body.get("name") or "")
    if op == "enroll":
        threading.Thread(target=enroll_from_page, daemon=True).start()
        return voices_view()
    profiles = voice_profiles()
    if name not in profiles:
        return {"error": "name"}
    if op == "forget":
        profiles.pop(name)
        log("voice forget %s" % name)
    elif op == "bind":
        ha = str(body.get("ha") or "")
        if ha and not re.match(r"^person\.[a-z0-9_]+$", ha):
            return {"error": "ha"}
        profiles[name]["ha"] = ha
        log("voice bind %s %s" % (name, ha or "-"))
    elif op == "rename":
        to = spoken_name(str(body.get("to") or "").lower())
        if not to or to in profiles:
            return {"error": "to"}
        profiles[to] = profiles.pop(name)
    else:
        return {"error": "op"}
    voice_save(profiles)
    return voices_view()


def open_follow():
    """После ответа послушать продолжение. Макс — по HTTP, Linux — файлом follow.go."""
    if FOLLOW_SECONDS <= 0:
        return
    if os.environ.get("YSE_PLAY") == "http":
        push_http("/api/follow", b"follow", "text/plain")
        log("follow open http")
        return
    speaker_text("touch %s/follow.go && echo %d > %s/hear.go" % (ROOT, int(time.time() * 1000), ROOT))
    log("follow open")


def enroll_listen():
    open_follow()


def enroll_nudge():
    """Пустое окно продолжения: один повтор вопроса, второй раз знакомство закрываем."""
    step = enroll.get("step") or ""
    if not step:
        return
    if enroll.get("nudge", 0) >= 1:
        enroll_clear()
        text = "Не расслышала. Скажи позже: запомни мой голос."
        again = False
    elif step == "name":
        enroll["nudge"] = 1
        text = "Не расслышала имя. Как тебя зовут?"
        again = True
    else:
        line = enroll_line()
        if not line:
            enroll_clear()
            return
        enroll["nudge"] = 1
        text = "Повтори ещё раз: %s." % line
        again = True
    with talk_lock:
        ok = speak(text, person_token() or DEMO_TOKEN)
    if ok and again:
        enroll_listen()


def enroll_from_page():
    """Кнопка «Познакомиться» на странице: колонка сама спрашивает имя и открывает окно продолжения."""
    enroll.update(step="name", name="", embs=[], nudge=0, at=time.time())
    with talk_lock:
        ok = speak("Давай познакомимся. Как тебя зовут?", person_token() or DEMO_TOKEN)
    if ok:
        enroll_listen()


def spoken(phrase):
    return " ".join(re.sub(r"[^\w\s]", " ", phrase.lower().replace("ё", "е")).split())


def enroll_line():
    step = enroll.get("step") or ""
    if not step.isdigit():
        return ""
    index = int(step)
    if index < 0 or index >= len(ENROLL_LINES):
        return ""
    return ENROLL_LINES[index]


def enroll_repeat(phrase):
    """Строку, которую только что попросили повторить, нельзя считать эхом ответа."""
    want = spoken(enroll_line())
    said = spoken(phrase)
    if len(said) < 2 or len(want) < 2:
        return False
    if said == want or want in said or said in want:
        return True
    need = want.split()
    hit = sum(1 for word in need if word in said.split())
    return hit >= max(2, (len(need) * 3 + 3) // 4)


def echoes_reply(phrase):
    """Своя озвучка, попавшая в окно продолжения: шумодав не вычел хвост, фраза ≈ последний ответ."""
    turns = recent_turns(120)
    if not turns:
        return False
    last = spoken(turns[-1].get("assistant") or "")
    said = spoken(phrase)
    if len(said) < 4 or len(last) < 4:
        return False
    if said in last or last in said:
        return True
    words = said.split()
    if words and words[0] in ("стоп", "хватит", "замолчи", "отбой") and len(words) > 2:
        words = words[1:]
    if len(words) < 2:
        return False
    hit = sum(1 for w in words if w in last)
    return hit >= max(2, (len(words) * 65 + 99) // 100)


def is_stop(phrase):
    """Короткое «стоп» / «хватит» / «замолчи», как Алиса гасит будильник и музыку."""
    return bool(re.fullmatch(r"(стоп|хватит|замолчи|отбой)( пожалуйста)?", spoken(phrase)))


def hush_now(phrase):
    """Гасит звонок сразу и останавливает музыку. True — фраза была командой стоп."""
    if not is_stop(phrase):
        return False
    speaker_text("touch %s/alarm.stop; pid=$(cat /tmp/alarm.play 2>/dev/null); "
                 "[ -n \"$pid\" ] && kill $pid 2>/dev/null; true" % ROOT)
    music_post("/v1/ctrl", "stop")
    log("hush")
    return True


def headset_phrase(phrase):
    """«Включи блютуз» / «выключи гарнитуру». Микрофон в гарнитуру уже берётся из очищенного кольца."""
    t = " ".join(phrase.lower().replace("ё", "е").replace(",", " ").split())
    if not re.search(r"\b(блютуз|bluetooth|гарнитур\w*)\b", t):
        return ""
    on = re.search(r"\b(включи|включить|активируй)\w*", t)
    off = re.search(r"\b(выключи|отключи|выключить)\w*", t)
    if on and off:
        want = on.start() < off.start()
    elif on:
        want = True
    elif off:
        want = False
    else:
        return ""
    flag = "1" if want else "0"
    script = (
        "prev=$(head -c 1 %s/settings/bt.txt 2>/dev/null); "
        "printf '%%s\\n' '%s' > %s/settings/bt.txt; "
        "if %s/bin/bthead apply >/dev/null 2>&1; then echo ok; "
        "else printf '%%s\\n' \"$prev\" > %s/settings/bt.txt; %s/bin/bthead apply >/dev/null 2>&1; echo fail; fi"
        % (ROOT, flag, ROOT, ROOT, ROOT, ROOT)
    )
    try:
        proc = subprocess.run(SPEAKER + [script], timeout=20, check=False, capture_output=True)
        out = proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return "Гарнитуру сейчас не переключить."
    if "ok" not in out.split():
        log("bluetooth fail")
        return "Гарнитуру сейчас не переключить."
    log("bluetooth %s" % ("on" if want else "off"))
    return "Гарнитура включена." if want else "Гарнитура выключена."


def panel_command(phrase):
    """«Открой панель» — страница колонки один раз сама заберёт ключ в ближайшие 2 минуты."""
    t = phrase.lower().replace("ё", "е")
    if not re.search(r"(открой|дай|покажи)\w* (панель|страниц|доступ)|ключ (от |к )?(панел|страниц)|доступ к (панел|страниц)", t):
        return ""
    speaker_text("touch %s/settings/panel.claim" % ROOT)
    log("panel claim open")
    return "Откройте страницу колонки в ближайшие две минуты, ключ подставится сам."


SELF_NAME = os.environ.get("YSE_NAME", "") or "Колонка"
MESSAGE_VERB = re.compile(r"^(передай|передайте|скажи|скажите|сообщи|сообщите|объяви|объявите|крикни)\s+(.+)$")
MESSAGE_FILL = ("в", "во", "на", "для", "колонке", "колонку", "колонка", "комнату", "комнате", "по", "через")
MESSAGE_ALL = ("всем", "везде", "все", "всех", "всему", "дому", "колонкам", "колонки", "комнаты", "комнатам")


def peers():
    """Другие колонки с ответами на этом сервере: id, имя, комната, порт yse_talk."""
    rows = []
    try:
        names = sorted(os.listdir("/storage/yse"))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".env"):
            continue
        try:
            with open(os.path.join("/storage/yse", name), encoding="utf-8") as fh:
                vals = dict(line.split("=", 1) for line in fh.read().splitlines() if "=" in line)
            base = int(vals.get("YSE_BASE", ""))
        except (OSError, ValueError):
            continue
        if base == BASE or not vals.get("YSE_SPEAKER"):
            continue
        rows.append({"id": name[:-4], "name": vals.get("YSE_NAME") or name[:-4],
                     "area": vals.get("YSE_AREA", ""), "port": base + 3})
    return rows


def _stem_hit(word, key):
    same = 0
    for a, b in zip(word, key):
        if a != b:
            break
        same += 1
    return same >= 3 and same >= min(len(key) - 1, 4)


def message_parse(phrase):
    """«передай в спальню, что ужин готов» → ([колонки], текст). ([], "") — фраза не про сообщение."""
    t = " ".join(phrase.lower().replace("ё", "е").replace(",", " ").replace(":", " ").split())
    m = MESSAGE_VERB.match(t)
    if not m:
        return [], ""
    words = m.group(2).split()
    all_peers = peers()
    targets = []
    i = 0
    while i < len(words) and (words[i] in MESSAGE_FILL or words[i] in MESSAGE_ALL):
        if words[i] in MESSAGE_ALL:
            targets = list(all_peers)
        i += 1
    if not targets:
        while i < len(words):
            hit = [p for p in all_peers
                   if any(_stem_hit(words[i], k) for k in (p["name"] + " " + p["area"]).lower().replace("ё", "е").split())]
            if not hit:
                break
            for p in hit:
                if p not in targets:
                    targets.append(p)
            i += 1
            while i < len(words) and words[i] in MESSAGE_FILL + ("и",):
                i += 1
    if not targets:
        return [], ""
    while i < len(words) and words[i] in ("что", "чтобы", "чтоб"):
        i += 1
    return targets, " ".join(words[i:])


def message_send(targets, text):
    """Сообщение на колонки. Возвращает имена тех, кто не принял."""
    body = json.dumps({"text": text, "from": SELF_NAME}, ensure_ascii=False).encode("utf-8")
    failed = []
    for p in targets:
        req = urllib.request.Request("http://127.0.0.1:%d/v1/say" % p["port"], data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            ok = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8")).get("ok")
        except (OSError, ValueError, urllib.error.URLError):
            ok = False
        log("message to %s %s" % (p["id"], "ok" if ok else "fail"))
        if not ok:
            failed.append(p["name"])
    return failed


def message_command(phrase):
    targets, text = message_parse(phrase)
    if not targets:
        return ""
    if not text:
        return "Скажите целиком: передай в спальню, что ужин готов."
    failed = message_send(targets, text)
    if not failed:
        return "Передал."
    if len(failed) == len(targets):
        return "Не получилось, колонка %s не отвечает." % ", ".join(failed)
    return "Передал, кроме колонки %s." % ", ".join(failed)


def message_chime():
    """Два коротких тона перед чужим сообщением, s16le 48 кГц стерео."""
    out = bytearray()
    for freq in (880.0, 1320.0):
        for n in range(int(48000 * 0.18)):
            amp = 6000 * min(1.0, n / 480.0, (int(48000 * 0.18) - n) / 480.0)
            v = int(amp * math.sin(2 * math.pi * freq * n / 48000))
            out += struct.pack("<hh", v, v)
    return bytes(out)


def message_say(text, sender):
    """Сообщение вслух: сигнал, потом «<откуда> передаёт: …», без отправителя — только текст.
    Сразу из обработчика не ждётся."""
    text = " ".join(str(text).split())[:300]
    sender = " ".join(str(sender).split())[:40]
    if not text:
        return False
    said = "%s передаёт: %s" % (sender, text) if sender else text

    def work():
        with talk_lock:
            with turn_lock:
                turn["busy"] = True
                turn["cut"] = False
            try:
                play_raw([message_chime()], 2)
                ok = speak(said, person_token() or DEMO_TOKEN)
            finally:
                with turn_lock:
                    turn["busy"] = False
        log("message from %s %s" % (sender or "-", "ok" if ok else "fail"))

    threading.Thread(target=work, daemon=True).start()
    return True


def reply(phrase, follow=False, nn="yandex", person=None, emb=None):
    started = time.time()
    asked = phrase
    tokens = []
    own = person_token()
    if own:
        tokens.append(own)
    if DEMO_TOKEN not in tokens:
        tokens.append(DEMO_TOKEN)
    text = ""
    used = tokens[0]
    route = ""
    climate = climate_kind(phrase)
    hushed = hush_now(phrase)
    voice_said = voice_dialog(phrase, emb, person, follow) or panel_command(phrase)
    alarm_said = "" if voice_said or hushed else (timer_command(phrase) or alarm_command(phrase))
    if voice_said:
        route = "voice-id"
        text = voice_said
    elif hushed:
        route = "stop"
        text = "Хорошо."
    elif alarm_said:
        route = "alarm"
        text = alarm_said
    elif message_parse(phrase)[0]:
        route = "message"
        text = message_command(phrase)
    elif is_clock_question(phrase):
        route = "clock"
        text = clock_sentence(speaker_clock())
    elif climate:
        route = "climate"
        try:
            text = climate_answer(phrase, climate)
        except (TypeError, ValueError, KeyError) as exc:
            log("climate fail %s" % type(exc).__name__)
            text = "Не получилось узнать погоду."
    elif volume_command(phrase):
        text = volume_answer(phrase)
        if not text:
            log("route volume")
            remember(phrase, phrase, "", "volume", person, time.time() - started)
            return True
        route = "volume"
    elif (headset_text := headset_phrase(phrase)):
        route = "bluetooth"
        text = headset_text
    elif music_kind(phrase)[0] in ("list", "who", "like", "dislike", "mine"):
        text = music_answer(*music_kind(phrase), person)
        if not text:
            log("route music")
            remember(phrase, phrase, "", "music", person, time.time() - started)
            return True
        route = "music"
    elif needs_web(phrase):
        # Новости и свежие факты не отдаём помощнику дома: он принимает их за команду.
        route = "web"
    elif nn == "ha":
        route = "ha-assist"
        matched, text = ha_ask(phrase, "default")
        if matched is None:
            text = "Home Assistant не отвечает."
        elif not text:
            text = "Готово." if matched else "Home Assistant не понял."
    else:
        matched, said, asked = ha_home(phrase)
        if matched is None:
            route = "ha"
            text = "Дом не ответил."
        elif matched:
            route = "ha"
            text = said or "Готово."
        elif music_kind(phrase)[0] == "play" and not music_answer(*music_kind(phrase), person):
            log("route music")
            remember(phrase, phrase, "", "music", person, time.time() - started)
            return True
        elif home_off(phrase):
            route = "ha"
            text = "Такого устройства нет."
    if not text and nn == "google" and route != "web":
        outcome = speak_google(phrase, person, used)
        if outcome[0] == "pending":
            text = outcome[1]
            route = "google"
        elif outcome[0] == "done":
            text, ok = outcome[1], outcome[2]
            route = "google"
            log("route %s%s" % (route, (" person %s %.2f" % person) if person else ""))
            remember(phrase, asked, text, route, person, time.time() - started)
            return ok
    if not text and nn == "openai" and route != "web":
        outcome = speak_llm(phrase, person, used)
        if outcome[0] == "web":
            log("llm unsure -> web: %s" % outcome[1][:80])
            text = ""
            route = "web"
        elif outcome[0] == "pending":
            text = outcome[1]
            route = "llm"
        elif outcome[0] == "done":
            text, ok = outcome[1], outcome[2]
            route = "llm"
            log("route %s%s" % (route, (" person %s %.2f" % person) if person else ""))
            remember(phrase, asked, text, route, person, time.time() - started)
            return ok
    if not text and route == "web":
        said = news_answer(phrase, person)
        if said:
            text = said
            route = "news"
    if not text:
        route = route or "yandex"
        asked = with_context(phrase, follow)
        log("ask %s%s%s" % (route, " follow" if follow else "", " glued" if asked != phrase else ""))
        for token in tokens:
            fresh = not (vins_state["req"] and vins_state["token"] == token
                         and time.time() - vins_state["at"] < VINS_IDLE)
            try:
                # В поиск Яндекса характер не подмешиваем: иначе ищется текст стиля и приходит «ничего не нашлось».
                query = asked if needs_web(phrase) else persona_for_yandex(asked, fresh, person)
                text = asyncio.run(vins_text(token, query))
            except Exception as exc:
                log("ask fail %s" % type(exc).__name__)
                text = ""
            if text:
                used = token
                break
    if not text and re.search(r"новост", phrase.lower().replace("ё", "е")):
        text = news_headlines()
        if text:
            route = "news"
    if not text:
        log("ask empty")
        return False
    log("route %s%s" % (route, (" person %s %.2f" % person) if person else ""))
    ok = speak(text, used)
    remember(phrase, asked, text, route, person, time.time() - started)
    return ok


def own_voice_chunks(text, voice, timeout=15):
    """Свой голос (yse-voice): первое предложение звучит, пока синтезируются следующие."""
    body = json.dumps({"input": text, "voice": voice}).encode("utf-8")
    req = urllib.request.Request(OWN_VOICE_URL + "/v1/audio/stream", data=body,
                                 headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=timeout)
    first = resp.read(9600)
    if not first:
        raise OSError("no audio")

    def rest():
        yield first
        with resp:
            while True:
                chunk = resp.read(9600)
                if not chunk:
                    return
                yield chunk
    return rest()


def speak_own(text, voice):
    http_room = os.environ.get("YSE_PLAY") == "http"
    timeout = 40 if http_room else 15
    tries = 2 if http_room else 1
    last = "?"
    for _attempt in range(tries):
        started = time.time()
        try:
            chunks = own_voice_chunks(text, voice, timeout)
        except (OSError, ValueError) as exc:
            last = type(exc).__name__
            log("own voice fail %s" % last)
            continue
        log("own voice %s first %.2fs" % (voice, time.time() - started))
        try:
            rc = play_raw(chunks, 1)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log("play fail %s" % type(exc).__name__)
            return False
        if turn["cut"]:
            log("spoke cut")
            return False
        log("spoke %s" % ("ok" if rc == 0 else rc))
        return rc == 0
    if http_room:
        log("own voice give up %s" % last)
        return False
    log("own voice fail %s, yandex" % last)
    return None


def speak(text, used=""):
    if turn["cut"]:
        return False
    saved = speaker_settings().get("voice.txt") or ""
    if saved.startswith("own:"):
        done = speak_own(text, saved[4:])
        if done is not None:
            return done
    voice = voice_name()
    ogg = b""
    try:
        ogg = asyncio.run(tts_ogg(used or DEMO_TOKEN, text, voice))
        if not ogg:
            ogg = asyncio.run(tts_ogg(DEMO_TOKEN, text, "shitova"))
    except Exception as exc:
        log("speak fail %s" % type(exc).__name__)
        return False
    if not ogg:
        log("speak empty")
        return False
    if turn["cut"]:
        return False
    try:
        rc = play_ogg(ogg)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log("play fail %s" % type(exc).__name__)
        return False
    if turn["cut"]:
        log("spoke cut")
        return False
    log("spoke %s" % ("ok" if rc == 0 else rc))
    return rc == 0


def span_words(seconds):
    minutes = max(1, int(round(seconds / 60.0)))
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append("%s %s" % (_num(hours, False) if hours < 60 else str(hours), _unit(hours, "час", "часа", "часов")))
    if minutes:
        word = _num(minutes, True)
        if word.endswith("одна"):
            word = word[:-4] + "одну"
        parts.append("%s %s" % (word, _unit(minutes, "минуту", "минуты", "минут")))
    return " ".join(parts)


HEAR_MODEL = "/storage/yse/vosk-model-small-ru-0.22"
_hear_model = None


def hear_text(pcm):
    global _hear_model
    if _hear_model is None:
        from vosk import Model
        _hear_model = Model(HEAR_MODEL)
    from vosk import KaldiRecognizer
    if len(pcm) % 2:
        pcm = pcm[:-1]
    if len(pcm) < 3200:
        return ""
    rec = KaldiRecognizer(_hear_model, 16000)
    rec.SetWords(False)
    step = 8000
    for offset in range(0, len(pcm), step):
        rec.AcceptWaveform(pcm[offset:offset + step])
    return (json.loads(rec.FinalResult()).get("text") or "").strip()


STT_URL = {
    "whisper": os.environ.get("YSE_WHISPER_URL", "http://127.0.0.1:18950"),
    "vosk": os.environ.get("YSE_VOSK_URL", "http://127.0.0.1:18951"),
    "gigaam": os.environ.get("YSE_GIGAAM_URL", "http://127.0.0.1:18953"),
}
STT_SAVE = os.environ.get("YSE_STT_SAVE", "/tmp/yse-hear-%s.raw" % os.environ.get("YSE_BASE", "x"))
STT_COMPARE = [n for n in os.environ.get("YSE_STT_COMPARE", "yandex,whisper").split(",") if n and n != "tone"]
CLIPS = os.environ.get("YSE_CLIPS", "/storage/yse/clips/%s" % os.environ.get("YSE_BASE", "x"))
CLIPS_KEEP = 400


def cut_chime(pcm):
    """Сигнал «слушаю» шумодав не вычитает: в первых 2,5 с он в 15-50 раз громче речи. Режем по его конец."""
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    step = 1600
    levels = []
    for start in range(0, len(samples), step):
        part = samples[start:start + step]
        levels.append((sum(x * x for x in part) / max(1, len(part))) ** 0.5)
    if len(levels) < 5:
        return pcm
    floor = sorted(levels)[len(levels) // 5] or 1.0
    loud = [i for i, level in enumerate(levels[:25]) if level > max(2000.0, floor * 30)]
    if not loud or loud[-1] - loud[0] > 10:
        return pcm
    return samples[(loud[-1] + 2) * step:].tobytes()


def louder(pcm):
    """Тихую запись поднимаем до -6 дБ по пику, не больше чем в 16 раз; по краям тишина для распознавания."""
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    if not samples:
        return pcm
    peak = max(max(samples), -min(samples), 1)
    gain = min(16.0, 16000.0 / peak)
    if gain > 1.05:
        samples = array.array("h", (int(max(-32768, min(32767, s * gain))) for s in samples))
    return b"\0" * 8000 + samples.tobytes() + b"\0" * 16000


def wav_bytes(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return buf.getvalue()


def stt_http(name, pcm):
    boundary = "yse" + uuid.uuid4().hex
    parts = []
    for field, value in (("model", name), ("language", "ru")):
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, field, value)).encode("utf-8"))
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"say.wav\"\r\n"
                  "Content-Type: audio/wav\r\n\r\n" % boundary).encode("utf-8") + wav_bytes(pcm) + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
    req = urllib.request.Request(
        STT_URL[name] + "/v1/audio/transcriptions", data=b"".join(parts), method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return str(json.loads(resp.read().decode("utf-8", "replace")).get("text") or "")


async def asr_yandex(token, pcm):
    """Как штатная прошивка: ASR.Recognize на uniproxy, звук кадрами с номером потока."""
    async with websockets.connect(
        "wss://uniproxy.alice.yandex.net/uni.ws", open_timeout=8, max_size=4_000_000
    ) as ws:
        await ws.send(json.dumps({"event": {"header": {
            "messageId": str(uuid.uuid4()), "name": "SynchronizeState", "namespace": "System"
        }, "payload": {
            "accept_invalid_auth": True, "auth_token": token,
            "speechkitVersion": "3.28.0", "uuid": str(uuid.uuid4())
        }}}))
        await ws.send(json.dumps({"event": {"header": {
            "messageId": str(uuid.uuid4()), "name": "Recognize", "namespace": "ASR", "streamId": 1
        }, "payload": {
            "lang": "ru-RU", "topic": "dialogeneral", "application": "aliced",
            "format": "audio/x-pcm;bit=16;rate=16000",
            "advancedASROptions": {"partial_results": False}
        }}}))
        for offset in range(0, len(pcm), 3200):
            await ws.send(struct.pack(">I", 1) + pcm[offset:offset + 3200])
        await ws.send(json.dumps({"streamcontrol": {
            "streamId": 1, "action": 0, "reason": 0, "messageId": str(uuid.uuid4())}}))
        text = ""
        for _ in range(12):
            try:
                msg = await asyncio.wait_for(ws.recv(), 6)
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            if isinstance(msg, bytes):
                continue
            directive = json.loads(msg).get("directive") or {}
            header = directive.get("header") or {}
            payload = directive.get("payload") or {}
            if header.get("name") == "EventException":
                raise RuntimeError("asr refused")
            if header.get("name") != "Result":
                continue
            hyps = payload.get("recognition") or []
            if hyps:
                text = str(hyps[0].get("normalized") or "")
            if payload.get("endOfUtt"):
                break
        return text


def stt_ha(pcm):
    data = ha_post("/api/yse_station/stt", pcm, "audio/L16; rate=16000; channels=1", timeout=20)
    if data is None:
        raise RuntimeError("ha stt unavailable")
    if not data.get("ok", True):
        raise RuntimeError("ha stt %s" % data.get("error"))
    return str(data.get("text") or "")


STT = {
    "vosk-small": hear_text,
    "vosk": lambda pcm: stt_http("vosk", pcm),
    "whisper": lambda pcm: stt_http("whisper", pcm),
    "gigaam": lambda pcm: stt_http("gigaam", pcm),
    "yandex": lambda pcm: asyncio.run(asr_yandex(person_token() or DEMO_TOKEN, pcm)),
    "ha": stt_ha,
}


def strip_wake(text, wake):
    """Запись начинается с момента слова, в ней бывает хвост «джарвис»."""
    words = text.split()
    wake = (wake or "джарвис").lower().replace("ё", "е")
    if wake.startswith("джарвис"):
        wake = "джарвис"
    for _ in range(2):
        if not words:
            break
        head = re.sub(r"[^\w-]", "", words[0].lower().replace("ё", "е"))
        if len(head) >= 3 and (wake.endswith(head) or difflib.SequenceMatcher(None, head, wake).ratio() >= 0.7):
            words.pop(0)
            continue
        break
    return " ".join(words).strip()


def recognize(pcm):
    settings = speaker_settings()
    engine = stt_engine(settings)
    if engine not in STT:
        engine = "vosk-small"
    pcm = louder(cut_chime(pcm))
    try:
        with open(STT_SAVE, "wb") as fh:
            fh.write(pcm)
    except OSError:
        pass
    for name in dict.fromkeys((engine, "vosk-small")):
        started = time.time()
        try:
            text = clean_text(STT[name](pcm), settings.get("wake.txt"))
        except Exception as exc:
            log("stt %s fail %s" % (name, type(exc).__name__))
            continue
        spent = time.time() - started
        log("stt %s %.1fs %.2fs: %s" % (name, len(pcm) / 32000.0, spent, text or "-"))
        threading.Thread(target=compare_stt, args=(pcm, name, text, spent, settings.get("wake.txt")),
                         daemon=True).start()
        return text
    return ""


def clean_text(text, wake):
    """Буквы и знаки конца фразы остаются: вопрос должен дойти до сети вопросом."""
    text = (text or "").replace("ё", "е").replace("Ё", "Е")
    text = re.sub(r"(\d)[.:](\d)", r"\1 \2", text)
    text = re.sub(r"[^0-9A-Za-zА-Яа-я\s\-.?!…,]", " ", text)
    text = re.sub(r"\s+([.?!…,])", r"\1", " ".join(text.split()))
    return strip_wake(text, wake)


def compare_stt(pcm, used, text, spent, wake):
    """Та же фраза другими распознаваниями: время и текст в журнал и в CLIPS/stt.jsonl."""
    results = {used: {"text": text, "s": round(spent, 2)}}
    for name in STT_COMPARE:
        if name in results or name not in STT:
            continue
        started = time.time()
        try:
            other = clean_text(STT[name](pcm), wake)
        except Exception as exc:
            other = "ERR " + type(exc).__name__
        results[name] = {"text": other, "s": round(time.time() - started, 2)}
        log("cmp %s %.2fs: %s" % (name, results[name]["s"], other or "-"))
    try:
        os.makedirs(CLIPS, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(CLIPS, stamp + ".raw"), "wb") as fh:
            fh.write(pcm)
        with open(os.path.join(CLIPS, "stt.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": stamp, "sec": round(len(pcm) / 32000.0, 2), "used": used,
                                 "stt": results}, ensure_ascii=False) + "\n")
        clips = sorted(n for n in os.listdir(CLIPS) if n.endswith(".raw"))
        for old in clips[:-CLIPS_KEEP]:
            os.remove(os.path.join(CLIPS, old))
    except OSError as exc:
        log("cmp save fail %s" % exc)


SYSTEM_PROMPT = (
    "Ты голосовой помощник в умном доме. Отвечай по-русски, коротко, одним-тремя предложениями. "
    "Ответ читается вслух: без списков, разметки, ссылок и эмодзи. Учитывай прошлые реплики разговора. "
    "Если для ответа нужны свежие новости, курсы, цены, спорт или точный факт, в котором ты не уверен, "
    "ответь одним словом: ПОИСК."
)
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


_SENT_END = re.compile(r"(?<=[.!?…])\s+")


def clean_llm(text):
    text = re.sub(r"<think>.*?</think>", " ", text or "", flags=re.S)
    text = re.sub(r"<think>.*", " ", text, flags=re.S)
    return " ".join(re.sub(r"[*#`_>|]", " ", text).split())


def llm_unsure(text):
    head = text.lower().replace("ё", "е")[:120]
    if re.fullmatch(r"поиск[.!?…]*", head.strip()):
        return True
    return any(mark in head for mark in UNSURE)


GOOGLE_URL = os.environ.get("YSE_GOOGLE_URL", "http://127.0.0.1:18955/v1/chat/completions")


def llm_prepare(phrase, person, stream, endpoint="", mode="openai"):
    """Запрос к своей нейросети. Пустой запрос и текст ошибки, если адреса нет.
    mode=google — чат «Режим ИИ», адрес свой, характер уходит в этот чат."""
    settings = speaker_settings()
    url = (endpoint or settings.get("llm.url") or "").rstrip("/")
    if not url:
        return None, "Нейросеть не настроена. Укажите адрес на странице колонки.", 0
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    key = "" if mode == "google" else speaker_cached("llmkey", "cat %s/settings/llm.key 2>/dev/null" % ROOT, 30)
    now = speaker_clock()
    clock = " Сейчас %s, %s, %02d:%02d." % (
        WEEKDAYS[now.weekday()], now.strftime("%d.%m.%Y"), now.hour, now.minute)
    if mode == "google":
        system = ("Отвечай по-русски, одним или двумя короткими предложениями, "
                  "без списков и ссылок." + clock)
    else:
        system = SYSTEM_PROMPT + clock
    if person:
        system += " С тобой говорит %s." % person[0]
    else:
        system += (" Голос собеседника не опознан. Не называй его по имени и не утверждай, "
                   "что это кто-то из прошлых реплик.")
    persona = persona_text()
    if persona:
        system += (
            " Ниже задан стиль общения — это не вопрос пользователя и не тема для ответа. "
            "Следуй стилю, отвечая только на реплику пользователя. Стиль: "
            + persona
        )
    system += address_clause(person)
    history = []
    for row in recent_turns()[-8:]:
        history += [{"role": "user", "content": row["user"]}, {"role": "assistant", "content": row["assistant"]}]
    messages = [{"role": "system", "content": system}] + history + [{"role": "user", "content": phrase}]
    payload = {
        "model": "google-ai-mode" if mode == "google" else (settings.get("llm.model") or "gpt-4o-mini"),
        "messages": messages, "max_tokens": 300, "temperature": 0.6,
    }
    if stream:
        payload["stream"] = True
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers)
    return req, "", len(history) // 2


def _index_after(pending, parts):
    if not parts:
        return 0
    idx = pending.rfind(parts[-1])
    if idx < 0:
        return None
    end = idx + len(parts[-1])
    while end < len(pending) and pending[end].isspace():
        end += 1
    return end


def pull_sentences(cleaned, committed, final):
    """Готовые фразы из уже пришедшего текста. Короткую фразу держим, пока нет следующей."""
    pending = cleaned[committed:]
    if final:
        text = pending.strip()
        return ([text] if text else []), len(cleaned)
    bits = _SENT_END.split(pending)
    if len(bits) < 2:
        return [], committed
    out = []
    for bit in bits[:-1]:
        bit = bit.strip()
        if not bit:
            continue
        if out and len(out[-1]) < 25:
            out[-1] += " " + bit
        else:
            out.append(bit)
    if out and len(out[-1]) < 25:
        out = out[:-1]
    end = _index_after(pending, out)
    if end is None:
        return [], committed
    return out, committed + end


def _delta_text(obj):
    try:
        choice = obj["choices"][0]
    except (KeyError, IndexError, TypeError):
        return ""
    delta = choice.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
        return delta["content"]
    message = choice.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    return ""


def llm_deltas(resp):
    """Куски ответа: поток data: или один JSON, если сервер поток не умеет."""
    buf = b""
    mode = ""
    while True:
        chunk = resp.read(512)
        if not chunk:
            break
        buf += chunk
        if not mode:
            stripped = buf.lstrip()
            if not stripped:
                continue
            mode = "sse" if stripped.startswith(b"data:") else "json"
        if mode != "sse":
            continue
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith("data:"):
                continue
            payload = text[5:].strip()
            if payload == "[DONE]":
                return
            try:
                piece = _delta_text(json.loads(payload))
            except ValueError:
                continue
            if piece:
                yield piece
    if mode == "json":
        try:
            obj = json.loads(buf.decode("utf-8", "replace"))
        except ValueError:
            return
        if isinstance(obj, dict) and obj.get("error") and "choices" not in obj:
            raise RuntimeError("llm error")
        piece = _delta_text(obj)
        if piece:
            yield piece
    elif mode == "sse" and buf.strip():
        text = buf.decode("utf-8", "replace").strip()
        if text.startswith("data:") and text[5:].strip() not in ("", "[DONE]"):
            try:
                piece = _delta_text(json.loads(text[5:].strip()))
            except ValueError:
                return
            if piece:
                yield piece


def llm_sentences(resp, box, started, turns):
    """Отдаёт фразы по мере записи. Первую законченную фразу проверяет: вдруг это отказ и нужен поиск."""
    raw = ""
    committed = 0
    gated = False
    logged = False
    try:
        for piece in llm_deltas(resp):
            raw += piece
            cleaned = clean_llm(raw)
            box["text"] = cleaned
            ready, new_committed = pull_sentences(cleaned, committed, False)
            if not gated:
                if not ready and len(cleaned) < 120:
                    continue
                gated = True
                if cleaned.startswith("Нейросеть ") or llm_unsure(cleaned):
                    box["web"] = True
                    return
            committed = new_committed
            for sent in ready:
                if not logged:
                    log("llm first %.1fs" % (time.time() - started))
                    logged = True
                yield sent
        cleaned = clean_llm(raw)
        box["text"] = cleaned
        if not gated and (not cleaned or cleaned.startswith("Нейросеть ") or llm_unsure(cleaned)):
            box["web"] = bool(cleaned)
            return
        ready, _committed = pull_sentences(cleaned, committed, True)
        for sent in ready:
            if not logged:
                log("llm first %.1fs" % (time.time() - started))
                logged = True
            yield sent
        log("llm %.1fs %d turns" % (time.time() - started, turns))
    finally:
        try:
            resp.close()
        except OSError:
            pass


def llm_answer(phrase, person=None):
    req, err, turns = llm_prepare(phrase, person, False)
    if req is None:
        return err
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        log("llm fail %s" % exc.code)
        return "Нейросеть ответила ошибкой %s." % exc.code
    except (OSError, ValueError) as exc:
        log("llm fail %s" % type(exc).__name__)
        return "Нейросеть не отвечает."
    try:
        text = str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return "Нейросеть прислала пустой ответ."
    text = clean_llm(text)
    log("llm %.1fs %d turns" % (time.time() - started, turns))
    return text


def _offer(bucket, item):
    while not turn["cut"]:
        try:
            bucket.put(item, timeout=0.3)
            return True
        except queue.Full:
            continue
    return False


def _take(bucket):
    while True:
        if turn["cut"]:
            raise BrokenPipeError
        try:
            return bucket.get(timeout=0.3)
        except queue.Empty:
            continue


def ogg_to_pcm(ogg):
    env = os.environ.copy()
    if os.path.exists(LIBVA):
        env["LD_PRELOAD"] = LIBVA
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"],
        input=ogg, capture_output=True, timeout=30, env=env,
    )
    return proc.stdout


def yandex_pcm(text, used):
    voice = voice_name()
    try:
        ogg = asyncio.run(tts_ogg(used or DEMO_TOKEN, text, voice))
        if not ogg:
            ogg = asyncio.run(tts_ogg(DEMO_TOKEN, text, "shitova"))
    except Exception as exc:
        log("speak fail %s" % type(exc).__name__)
        return b""
    if not ogg:
        log("speak empty")
        return b""
    return ogg_to_pcm(ogg)


def _play_queued(produce, channels, stop=None):
    bucket = queue.Queue(maxsize=2)
    def run():
        try:
            produce(bucket)
        finally:
            _offer(bucket, None)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    def chunks():
        while True:
            item = _take(bucket)
            if item is None:
                return
            if isinstance(item, Exception):
                raise OSError(type(item).__name__)
            yield item
    try:
        rc = play_raw(chunks(), channels)
    finally:
        if stop and turn["cut"]:
            try:
                stop()
            except OSError:
                pass
        worker.join(timeout=5)
    return rc == 0


def speak_yandex_sentences(parts, used, stop=None):
    def produce(bucket):
        try:
            for part in parts:
                if turn["cut"]:
                    break
                part = " ".join(part.split())
                if not part:
                    continue
                pcm = yandex_pcm(part, used)
                if not pcm:
                    _offer(bucket, OSError("empty"))
                    return
                if not _offer(bucket, pcm):
                    return
        except (OSError, ValueError) as exc:
            log("speak fail %s" % type(exc).__name__)
            _offer(bucket, exc)
    return _play_queued(produce, 2, stop)


def speak_own_sentences(first_audio, parts, voice, timeout, stop=None):
    """Пока звучит фраза, следующая уже считается."""
    bucket = queue.Queue(maxsize=2)
    def run():
        try:
            if not _offer(bucket, first_audio):
                return
            for part in parts:
                if turn["cut"]:
                    break
                part = " ".join(part.split())
                if not part:
                    continue
                audio = own_voice_chunks(part, voice, timeout)
                if not _offer(bucket, audio):
                    return
        except (OSError, ValueError) as exc:
            log("own voice fail %s" % type(exc).__name__)
            _offer(bucket, exc)
        finally:
            _offer(bucket, None)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    def chunks():
        while True:
            item = _take(bucket)
            if item is None:
                return
            if isinstance(item, Exception):
                raise OSError(type(item).__name__)
            yield from item
    try:
        rc = play_raw(chunks(), 1)
    finally:
        if stop and turn["cut"]:
            try:
                stop()
            except OSError:
                pass
        worker.join(timeout=5)
    return False if turn["cut"] else rc == 0


def _chain(first, rest):
    yield first
    yield from rest


def speak_sentences(first, rest, used, stop=None):
    """Первая фраза уже готова, остальные приходят, пока она звучит."""
    saved = speaker_settings().get("voice.txt") or ""
    if saved.startswith("own:"):
        voice = saved[4:]
        http_room = os.environ.get("YSE_PLAY") == "http"
        timeout = 40 if http_room else 15
        tries = 2 if http_room else 1
        chunks = None
        for _attempt in range(tries):
            try:
                started = time.time()
                chunks = own_voice_chunks(first, voice, timeout)
                log("own voice %s first %.2fs" % (voice, time.time() - started))
                break
            except (OSError, ValueError) as exc:
                log("own voice fail %s" % type(exc).__name__)
                chunks = None
        if chunks is not None:
            return speak_own_sentences(chunks, rest, voice, timeout, stop)
        if http_room:
            log("own voice give up")
            rest.close()
            return False
        log("own voice fail, yandex")
        return speak_yandex_sentences(_chain(first, rest), used, stop)
    return speak_yandex_sentences(_chain(first, rest), used, stop)


def speak_google(phrase, person, used):
    """Чат Google «Режим ИИ». Это не адрес OpenAI и не запасной Яндекс."""
    req, err, turns = llm_prepare(phrase, person, True, GOOGLE_URL, "google")
    if req is None:
        return ("pending", err)
    outcome = speak_llm_request(req, turns, used, 50)
    if outcome[0] == "web":
        return ("pending", outcome[1])
    return outcome


def speak_llm(phrase, person, used):
    """Говорит с первой фразы, не дожидаясь конца ответа. ('done', текст, ok) уже сказано."""
    req, err, turns = llm_prepare(phrase, person, True)
    if req is None:
        return ("web", err)
    return speak_llm_request(req, turns, used, 40)


def speak_llm_request(req, turns, used, timeout):
    started = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        log("llm fail %s" % exc.code)
        return ("web", "Нейросеть ответила ошибкой %s." % exc.code)
    except (OSError, ValueError) as exc:
        log("llm fail %s" % type(exc).__name__)
        return ("web", "Нейросеть не отвечает.")
    else:
        box = {"text": "", "web": False}
        gen = llm_sentences(resp, box, started, turns)
        try:
            first = next(gen, None)
        except (OSError, ValueError, RuntimeError) as exc:
            log("llm fail %s" % type(exc).__name__)
            gen.close()
            return ("web", "Нейросеть не отвечает.")
        if box["web"]:
            gen.close()
            return ("web", box["text"])
        if first is None:
            gen.close()
            text = box["text"]
        else:
            try:
                ok = speak_sentences(first, gen, used, resp.close)
            except Exception:
                try:
                    gen.close()
                except ValueError:
                    pass
                raise
            return ("done", box["text"] or first, ok)
    if not text:
        return ("empty",)
    if text.startswith("Нейросеть ") or llm_unsure(text):
        return ("web", text)
    return ("pending", text)


def news_plain(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = re.sub(r"https?://\S+", " ", text)
    return " ".join(re.sub(r"[*#`_>|]", " ", text).split())


def news_from_response(data):
    if isinstance(data.get("output_text"), str):
        found = news_plain(data["output_text"])
        if found:
            return found
    parts = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "output_text":
            parts.append(item.get("text") or "")
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                parts.append(block.get("text") or "")
    return news_plain(" ".join(parts))


def news_post(url, body, key):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def news_headlines(limit=3):
    """Когда Яндекс новости не отдал: короткие заголовки с публичной ленты."""
    req = urllib.request.Request(
        "https://lenta.ru/rss",
        headers={"User-Agent": "yse-station"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read(200000).decode("utf-8", "replace")
    except (OSError, ValueError):
        log("news feed fail")
        return ""
    titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", raw, flags=re.S)
    out = []
    for title in titles[1:]:
        title = " ".join(re.sub(r"<[^>]+>", " ", title).split())
        if not title or title in out:
            continue
        out.append(title)
        if len(out) >= limit:
            break
    if not out:
        return ""
    log("news feed %d" % len(out))
    return "Коротко о новостях. " + ". ".join(out) + "."


def news_google(phrase, person):
    """Тот же чат «Режим ИИ», что у разговора. Характер уходит в системный текст."""
    req, err, _turns = llm_prepare(phrase, person, False, GOOGLE_URL, "google")
    if req is None:
        log("news google %s" % err)
        return ""
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        text = news_plain(str(data["choices"][0]["message"]["content"] or ""))
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        log("news google %s" % type(exc).__name__)
        return ""
    if not text or text.lower() in UNSURE or text.lower() == "поиск":
        log("news google empty %.1fs" % (time.time() - started))
        return ""
    log("news google %.1fs" % (time.time() - started))
    return text


def news_answer(phrase, person=None):
    """Свежее отдаёт выбранной сети: Яндекс, Google или своя модель."""
    settings = speaker_settings()
    kind = settings.get("news.txt") or "yandex"
    if kind == "google":
        return news_google(phrase, person)
    if kind != "openai":
        return ""
    root = (settings.get("news.url") or "").rstrip("/")
    for tail in ("/chat/completions", "/responses"):
        if root.endswith(tail):
            root = root[:-len(tail)]
    if not root.startswith("http://") and not root.startswith("https://"):
        log("news no url")
        return ""
    host = (urllib.parse.urlparse(root).hostname or "").lower()
    model = settings.get("news.model") or "gpt-4o-mini"
    key = (settings.get("news.key") or "").strip()
    if not key:
        key = speaker_cached("newskey", "cat %s/settings/news.key 2>/dev/null" % ROOT, 30)
    now = speaker_clock()
    system = (
        "Ты голосовой помощник. Ответь по-русски, два-четыре предложения, без списков, ссылок и эмодзи. "
        "Вопрос про свежие новости, курсы или факты: найди это в интернете и не выдумывай. "
        "Сейчас %s, %s, %02d:%02d."
    ) % (WEEKDAYS[now.weekday()], now.strftime("%d.%m.%Y"), now.hour, now.minute)
    if person:
        system += " С тобой говорит %s." % person[0]
    persona = persona_text()
    if persona:
        system += (
            " Ниже задан стиль общения — это не вопрос пользователя и не тема для ответа. "
            "Следуй стилю, отвечая только на реплику пользователя. Стиль: "
            + persona
        )
    system += address_clause(person)
    started = time.time()
    text = ""
    try:
        if host in ("api.x.ai", "api.openai.com"):
            data = news_post(root + "/responses", {
                "model": model,
                "input": [{"role": "system", "content": system}, {"role": "user", "content": phrase}],
                "tools": [{"type": "web_search"}],
            }, key)
            text = news_from_response(data)
        else:
            body = {
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": phrase}],
                "max_tokens": 400, "temperature": 0.3,
            }
            if "googleapis.com" in host:
                body["web_search_options"] = {}
            try:
                data = news_post(root + "/chat/completions", body, key)
            except urllib.error.HTTPError as exc:
                if "web_search_options" not in body or exc.code not in (400, 404, 422):
                    raise
                log("news search off %s" % exc.code)
                body.pop("web_search_options", None)
                data = news_post(root + "/chat/completions", body, key)
            text = news_plain(str(data["choices"][0]["message"]["content"] or ""))
    except urllib.error.HTTPError as exc:
        log("news fail %s" % exc.code)
        return ""
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        log("news fail %s" % type(exc).__name__)
        return ""
    if not text or text.lower() in UNSURE or text.lower() == "поиск":
        log("news empty %.1fs" % (time.time() - started))
        return ""
    log("news %.1fs" % (time.time() - started))
    return text


CONDITIONS = {
    "clear-night": "ясно", "cloudy": "облачно", "exceptional": "необычная погода",
    "fog": "туман", "hail": "град", "lightning": "гроза", "lightning-rainy": "гроза с дождём",
    "partlycloudy": "переменная облачность", "pouring": "ливень", "rainy": "дождь",
    "snowy": "снег", "snowy-rainy": "мокрый снег", "sunny": "солнечно",
    "windy": "ветрено", "windy-variant": "ветрено и облачно",
}


def climate_kind(phrase):
    text = " ".join(phrase.lower().replace("ё", "е").replace("?", " ").split())
    outside = any(w in text for w in ("на улице", "за окном", "снаружи", "на дворе"))
    if "погод" in text or (outside and any(w in text for w in ("температур", "градус", "холодно", "тепло", "жарко"))):
        return "weather"
    if "температур" in text or "сколько градусов" in text or "сколько тепла" in text:
        return "temp"
    return ""


def degrees(value):
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return ""
    word = _unit(n, "градус", "градуса", "градусов")
    return ("минус %d %s" % (-n, word)) if n < 0 else ("%d %s" % (n, word))


def span_degrees(low, high):
    low, high = int(round(float(low))), int(round(float(high)))
    if low == high:
        return degrees(high)
    first = ("минус %d" % -low) if low < 0 else str(low)
    return "от %s до %s" % (first, degrees(high))


def show_temp(value, name="temp.show"):
    try:
        text = "%d" % int(round(float(value)))
    except (TypeError, ValueError):
        return
    try:
        subprocess.run(SPEAKER + ["echo %s > %s/%s" % (text, ROOT, name)], timeout=8, check=False,
                       capture_output=True)
    except (OSError, subprocess.TimeoutExpired):
        pass


def climate_answer(phrase, kind):
    data = ha_post("/api/yse_station/climate", json.dumps(
        {"text": phrase, "kind": kind}).encode("utf-8"), timeout=8)
    if data is None:
        return "Home Assistant не отвечает."
    if kind == "temp":
        if not data.get("ok"):
            if data.get("room"):
                return "В комнате %s нет датчика температуры." % data["room"]
            return "Не знаю, в какой комнате стоит колонка. Укажите ей комнату в Home Assistant."
        threading.Thread(target=show_temp, args=(data["temperature"],), daemon=True).start()
        if data.get("own"):
            return "Здесь %s." % degrees(data["temperature"])
        return "%s: %s." % (data.get("room") or "Там", degrees(data["temperature"]))
    if not data.get("ok"):
        return "Нет данных о погоде."
    low = phrase.lower()
    day = data.get("tomorrow") if "завтра" in low else None
    if day:
        parts = ["Завтра"]
        if day.get("templow") is not None and day.get("temperature") is not None:
            parts.append(span_degrees(day["templow"], day["temperature"]))
        elif day.get("temperature") is not None:
            parts.append(degrees(day["temperature"]))
        sentence = " ".join(parts)
        cond = CONDITIONS.get(day.get("condition") or "")
        if cond:
            sentence += ", " + cond
        if day.get("temperature") is not None:
            threading.Thread(target=show_temp, args=(day["temperature"],), daemon=True).start()
        return sentence + "."
    temp = data.get("temperature")
    today = data.get("today") or {}
    said_today = False
    if temp is None and today.get("temperature") is not None:
        if today.get("templow") is not None:
            sentence = "Сегодня на улице %s" % span_degrees(today["templow"], today["temperature"])
        else:
            got = degrees(today["temperature"])
            sentence = "Сегодня на улице %s" % got if got else ""
        said_today = bool(sentence)
    elif temp is not None:
        sentence = "Сейчас на улице %s" % degrees(temp)
    else:
        sentence = ""
    if not sentence:
        cond = CONDITIONS.get(data.get("condition") or "")
        return ("Сейчас на улице %s." % cond) if cond else "Нет данных о погоде."
    cond = CONDITIONS.get(data.get("condition") or "")
    if cond:
        sentence += ", " + cond
    sentence += "."
    wind_raw = data.get("wind_speed")
    if wind_raw is not None:
        try:
            wind = float(wind_raw)
        except (TypeError, ValueError):
            wind = None
        if wind is not None:
            if (data.get("wind_unit") or "").startswith("km"):
                wind = wind / 3.6
            w = int(round(wind))
            sentence += " Ветер %d %s в секунду." % (w, _unit(w, "метр", "метра", "метров"))
    if not said_today and today.get("temperature") is not None and today.get("templow") is not None:
        sentence += " Сегодня %s." % span_degrees(today["templow"], today["temperature"])
    show = temp if temp is not None else today.get("temperature")
    if show is not None:
        threading.Thread(target=show_temp, args=(show,), daemon=True).start()
    return sentence


def room_temp_loop():
    """Температура комнаты для экрана Мини 2: раз в 5 минут в temp.now."""
    while True:
        data = ha_post("/api/yse_station/climate", b'{"text":"","kind":"temp"}', timeout=8)
        if data and data.get("ok"):
            show_temp(data["temperature"], "temp.now")
        time.sleep(300)


def stt_warm_loop():
    """Whisper после простоя отвечает секунды вместо десятых: держим его тёплым тишиной."""
    while True:
        chosen = stt_engine()
        for name in ("whisper",):
            if name in STT_COMPARE or chosen == name:
                try:
                    stt_http(name, b"\0" * 16000)
                except Exception:
                    pass
        time.sleep(120)


def alarm_sentence(body):
    if body == "clear":
        return "Будильники отменены."
    if body == "none":
        return "Не расслышала время будильника. Скажите ещё раз, например: поставь будильник на семь тридцать."
    try:
        when = int(body)
    except ValueError:
        return ""
    now = speaker_clock()
    zone = now.tzinfo
    at = datetime.datetime.fromtimestamp(when, zone)
    hours = "%s %s" % (_num(at.hour, False), _unit(at.hour, "час", "часа", "часов"))
    if at.minute:
        clock = "%s %s %s" % (hours, _num(at.minute, True), _unit(at.minute, "минута", "минуты", "минут"))
    else:
        clock = hours + " ровно"
    left = when - now.timestamp()
    if at.date() == now.date() + datetime.timedelta(days=1):
        clock = "завтра " + clock
    elif now.date() < at.date() < now.date() + datetime.timedelta(days=7):
        clock += " " + WEEKDAY_ON[at.weekday()]
    elif at.date() != now.date():
        clock = "%d.%02d %s" % (at.day, at.month, clock)
    if left > 24 * 3600:
        return "Будильник поставлен на %s." % clock
    return "Будильник поставлен на %s. Прозвенит через %s." % (clock, span_words(left))


def answer_heard(phrase, audio=b"", follow=False, stream=False):
    """audio — звук фразы (для узнавания голоса), stream — колонка умеет окно продолжения."""
    quiet = pcm_rms(audio)
    log("hear%s %s rms %.0f" % (" follow" if follow else "", phrase or "empty", quiet))
    if follow and phrase and echoes_reply(phrase) and not enroll_repeat(phrase):
        log("hear follow echo dropped %s" % phrase)
        return False
    if not phrase and quiet < float(os.environ.get("YSE_QUIET_RMS", "220") or "220"):
        log("hear empty quiet")
        if follow and enroll.get("step"):
            enroll_nudge()
        return False
    if not phrase:
        if not follow:
            line = missed_sentence()
            publish_heard(line)
            with talk_lock:
                with turn_lock:
                    turn["busy"] = True
                    turn["cut"] = False
                try:
                    speak(line, person_token() or DEMO_TOKEN)
                finally:
                    with turn_lock:
                        turn["busy"] = False
        return False
    publish_heard(phrase)
    emb = voice_embed(cut_chime(audio)) if audio else None
    best = identify(emb)
    person = best if best and best[1] >= VOICE_MATCH else None
    if best:
        item = voice_profiles().get(person[0]) if person else None
        tell_ha_speaker(person[0] if person else "незнакомый", best[1], (item or {}).get("ha", ""))
    with talk_lock:
        with turn_lock:
            turn["busy"] = True
            turn["cut"] = False
        try:
            ok = reply(phrase, follow, brain_name(speaker_settings().get("nn.txt")), person, emb)
        finally:
            with turn_lock:
                turn["busy"] = False
    if ok and stream and not turn["cut"]:
        open_follow()
    return ok


LISTEN_PORT = BASE + 5
LISTEN_WALL = 13.0
FOLLOW_SECONDS = float(os.environ.get("YSE_FOLLOW", "5"))
VAD_MODEL = os.environ.get("YSE_VAD_MODEL", "/storage/yse/models/silero_vad.onnx")
FRAME = 640
_vad_session = None


def vad_session():
    """Silero VAD (onnx). Нет модели или onnxruntime — None, тогда конец фразы по громкости."""
    global _vad_session
    if _vad_session is None:
        try:
            import onnxruntime
            opts = onnxruntime.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            _vad_session = onnxruntime.InferenceSession(VAD_MODEL, sess_options=opts,
                                                        providers=["CPUExecutionProvider"])
        except Exception as exc:
            log("vad off %s" % type(exc).__name__)
            _vad_session = False
    return _vad_session or None


class SpeechEnd:
    """Где кончилась фраза: Silero VAD по 512 отсчётов, сигнал «слушаю» не речь."""

    def __init__(self, chime, wait_ms=5000, settle_ms=0):
        import numpy
        self.np = numpy
        self.wait_ms = wait_ms
        self.settle_ms = settle_ms
        self.session = vad_session()
        self.state = numpy.zeros((2, 1, 128), dtype=numpy.float32)
        self.context = numpy.zeros((1, 64), dtype=numpy.float32)
        self.rest = numpy.zeros(0, dtype=numpy.float32)
        self.chime_done = not chime
        self.loud_run = 0
        self.floor = 0.0
        self.samples = 0
        self.speech_ms = 0
        self.last_speech = -1
        self.started = False

    def level(self, pcm):
        samples = self.np.frombuffer(pcm, dtype=self.np.int16).astype(self.np.float32)
        return samples, float(self.np.sqrt(self.np.mean(samples * samples))) if len(samples) else 0.0

    def feed(self, pcm):
        """Кадр 20 мс. Возвращает кадр для облака (сигнал заглушён) и признак конца фразы."""
        samples, level = self.level(pcm)
        at = self.samples / 16.0
        self.samples += len(samples)
        # После озвучки в clean ещё хвост своего голоса — в окне продолжения первые сотни мс глушим.
        if at < self.settle_ms:
            return b"\0" * len(pcm), False
        if self.floor == 0.0 or level < self.floor:
            self.floor = max(level, 1.0)
        if not self.chime_done and at < 2500 and level > max(2000.0, self.floor * 30):
            self.loud_run += 1
            return b"\0" * len(pcm), False
        if self.loud_run >= 8 or at >= 2500:
            self.chime_done = True
        self.loud_run = 0
        # Тихую речь с двух метров фиксированные ×8 не дотягивают до Silero.
        # Пик кадра поднимаем к 0.25, но не больше ×12 — без жёсткой обрезки громкой речи.
        peak = float(self.np.max(self.np.abs(samples))) if len(samples) else 1.0
        gain = min(12.0, 8000.0 / max(peak, 1.0))
        speech = self.speech(self.np.clip(samples * gain, -32767, 32767) / 32768.0, level)
        if speech:
            self.speech_ms += 20
            if self.speech_ms >= 120:
                self.started = True
            self.last_speech = at
        elif not self.started:
            self.speech_ms = max(0, self.speech_ms - 20)
        return pcm, self.ended(at)

    def speech(self, samples, level):
        if self.session is None:
            return level > max(60.0, self.floor * 2.5)
        np = self.np
        self.rest = np.concatenate((self.rest, samples))
        hit = False
        while len(self.rest) >= 512:
            chunk = self.rest[:512].reshape(1, 512)
            self.rest = self.rest[512:]
            out, self.state = self.session.run(None, {
                "input": np.concatenate((self.context, chunk), axis=1),
                "state": self.state, "sr": np.array(16000, dtype=np.int64)})
            self.context = chunk[:, -64:]
            hit = hit or float(out[0][0]) >= 0.35
        return hit

    def ended(self, at):
        if at >= 10000:
            return True
        if not self.started:
            return at >= self.wait_ms
        return at - self.last_speech >= 700


async def listen_yandex(sock, head, token, audio, result):
    """Как штатная прошивка: звук сразу в ASR.Recognize (тема quasar-general), конец фразы решает Яндекс."""
    loop = asyncio.get_running_loop()
    ends = SpeechEnd(head["chime"], head["wait"], head.get("settle", 0))
    async with websockets.connect("wss://uniproxy.alice.yandex.net/uni.ws", open_timeout=5,
                                  max_size=4_000_000) as ws:
        await ws.send(json.dumps({"event": {"header": {
            "messageId": str(uuid.uuid4()), "name": "SynchronizeState", "namespace": "System"
        }, "payload": {"accept_invalid_auth": True, "auth_token": token,
                       "speechkitVersion": "3.28.0", "uuid": VINS_DEVICE}}}))
        await ws.send(json.dumps({"event": {"header": {
            "messageId": str(uuid.uuid4()), "name": "Recognize", "namespace": "ASR", "streamId": 1
        }, "payload": {"lang": "ru-RU", "topic": "quasar-general", "application": "aliced",
                       "format": "audio/x-pcm;bit=16;rate=16000",
                       "advancedASROptions": {"partial_results": True}}}}))

        async def results():
            async for msg in ws:
                if isinstance(msg, bytes):
                    continue
                directive = json.loads(msg).get("directive") or {}
                header = directive.get("header") or {}
                payload = directive.get("payload") or {}
                if header.get("name") == "EventException":
                    raise RuntimeError("asr refused")
                if header.get("name") != "Result":
                    continue
                hyps = payload.get("recognition") or []
                if hyps:
                    result["text"] = str(hyps[0].get("normalized") or "")
                if payload.get("endOfUtt"):
                    result["eou"] = time.time()
                    return

        reader = asyncio.create_task(results())
        pending = b""
        began = time.time()
        while not reader.done() and time.time() - began < LISTEN_WALL:
            try:
                data = await asyncio.wait_for(loop.sock_recv(sock, 8192), 1.0)
            except asyncio.TimeoutError:
                data = None
            if data == b"":
                break
            if data:
                pending += data
            while len(pending) >= FRAME:
                frame, pending = pending[:FRAME], pending[FRAME:]
                audio += frame
                frame, over = ends.feed(frame)
                await ws.send(struct.pack(">I", 1) + frame)
                if over and result["vad_end"] is None:
                    result["vad_end"] = time.time()
            if result["vad_end"] and (not ends.started or time.time() - result["vad_end"] > 1.2):
                break
        if not reader.done():
            await ws.send(json.dumps({"streamcontrol": {
                "streamId": 1, "action": 0, "reason": 0, "messageId": str(uuid.uuid4())}}))
            try:
                await asyncio.wait_for(reader, 1.5)
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                reader.cancel()
        elif reader.exception():
            raise reader.exception()
    result["spoke"] = ends.started


def early_follow_phrase(audio):
    """Имя часто говорят сразу, а первые 700 мс окна глушатся как хвост озвучки.
    Если речь была только там, распознаём эту секунду, а не ждём тишину до конца."""
    head = audio[:32000]
    if len(head) < 8000 or pcm_rms(head) < 80:
        return ""
    return recognize(head)


def listen_local(sock, head):
    """Конец фразы по Silero VAD, текст — выбранным распознаванием по всей записи."""
    ends = SpeechEnd(head["chime"], head["wait"], head.get("settle", 0))
    audio = bytearray()
    pending = b""
    sock.settimeout(1.0)
    began = time.time()
    while time.time() - began < LISTEN_WALL:
        try:
            data = sock.recv(8192)
        except socket.timeout:
            data = None
        if data == b"":
            break
        if data:
            pending += data
        over = False
        while len(pending) >= FRAME:
            frame, pending = pending[:FRAME], pending[FRAME:]
            audio += frame
            _frame, over = ends.feed(frame)
            if over:
                break
        if over:
            break
    return ends.started, bytes(audio)


class ListenHandler(socketserver.BaseRequestHandler):
    """Поток с колонки после слова: строка «YSE1 lag=.. chime=..», дальше s16le 16 кГц моно.
    Сервер отвечает «stop», когда фраза кончилась, и сам отвечает голосом."""

    def handle(self):
        sock = self.request
        sock.settimeout(3.0)
        try:
            line = b""
            while not line.endswith(b"\n") and len(line) < 200:
                part = sock.recv(1)
                if not part:
                    return
                line += part
        except OSError:
            return
        fields = dict(item.split("=", 1) for item in line.decode("ascii", "replace").split()[1:] if "=" in item)
        follow = fields.get("follow") == "1"
        head = {"lag": int(fields.get("lag", "0") or 0), "chime": fields.get("chime", "1") == "1",
                "wait": int(FOLLOW_SECONDS * 1000) if follow else 5000,
                "settle": 700 if follow else 0}
        settings = speaker_settings()
        engine = stt_engine(settings)
        started = time.time()
        phrase = ""
        audio = bytearray()
        how = engine
        tail = ""
        result = {"text": "", "eou": 0.0, "vad_end": None, "spoke": False}
        if engine == "yandex":
            sock.setblocking(False)
            try:
                asyncio.run(listen_yandex(sock, head, person_token() or DEMO_TOKEN, audio, result))
                phrase = clean_text(result["text"], settings.get("wake.txt"))
                how = "yandex-stream %s" % ("eou" if result["eou"] else "vad")
                if result["eou"] and result["vad_end"]:
                    tail = " eou-vad %+.2fs" % (result["eou"] - result["vad_end"])
            except Exception as exc:
                log("listen yandex fail %s" % type(exc).__name__)
                how = "yandex"
            sock.setblocking(True)
        if how == engine:
            spoke, more = listen_local(sock, head)
            audio += more
            phrase = recognize(bytes(audio)) if spoke else ""
            if follow and not phrase:
                phrase = early_follow_phrase(bytes(audio))
                if phrase:
                    tail += " early"
        heard_at = time.time()
        if follow and phrase:
            # На тишине окна продолжения Яндекс выдумывает английские слова («please», «jacket»).
            silent = how.startswith("yandex-stream") and not result["spoke"]
            if silent or not re.search(r"[а-яё]", phrase.lower()) or (echoes_reply(phrase) and not enroll_repeat(phrase)):
                tail += " dropped %s" % phrase
                phrase = ""
        try:
            sock.sendall(b"stop\n")
        except OSError:
            pass
        log("listen%s lag %d %s %.1fs audio, %.2fs%s: %s" % (" follow" if follow else "", head["lag"], how,
                                                              len(audio) / 32000.0, heard_at - started, tail,
                                                              phrase or "-"))
        audio = bytes(audio)
        if how.startswith("yandex-stream") and audio and phrase:
            pcm = louder(cut_chime(audio))
            threading.Thread(target=compare_stt, args=(pcm, "yandex", phrase, 0.0, settings.get("wake.txt")),
                             daemon=True).start()
        answer_heard(phrase, audio, follow, True)


class ListenServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.partition("?")[0] == "/v1/voices":
            self.send_json(voices_view())
            return
        if self.path.partition("?")[0] == "/v1/message":
            self.send_json({"self": SELF_NAME,
                            "peers": [{"id": p["id"], "name": p["name"], "area": p["area"]} for p in peers()]})
            return
        self.send_error(404)

    def read_json(self, limit):
        length = int(self.headers.get("Content-Length") or "0")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if 0 < length <= limit else {}
        except ValueError:
            body = {}
        return body if isinstance(body, dict) else {}

    def do_POST(self):
        path, _, query = self.path.partition("?")
        client = self.client_address[0]
        if path == "/v1/say":
            body = self.read_json(1200)
            if client != "127.0.0.1":
                self.send_error(403)
                return
            self.send_json({"ok": message_say(body.get("text") or "", body.get("from") or "")})
            return
        if path == "/v1/message":
            body = self.read_json(1200)
            if client not in ("127.0.0.1", SPEAKER[1]):
                self.send_error(403)
                return
            text = " ".join(str(body.get("text") or "").split())[:300]
            to = str(body.get("to") or "")
            if to == "here":
                self.send_json({"ok": message_say(text, body.get("from") or "")})
                return
            targets = [p for p in peers() if to in ("all", p["id"])]
            if not text or not targets:
                self.send_json({"ok": False, "error": "to" if text else "text"})
                return
            failed = message_send(targets, text)
            self.send_json({"ok": not failed, "failed": failed})
            return
        if path == "/v1/voices":
            length = int(self.headers.get("Content-Length") or "0")
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8")) if 0 < length <= 600 else {}
            except ValueError:
                body = {}
            self.send_json(voices_change(body if isinstance(body, dict) else {}))
            return
        if path == "/v1/alarm-hear":
            length = int(self.headers.get("Content-Length") or "0")
            pcm = self.rfile.read(length) if 0 < length <= 16000 * 2 * 4 else b""
            hit = False
            said = ""
            if pcm:
                try:
                    said = hear_text(pcm)
                except Exception as exc:
                    log("alarm hear fail %s" % type(exc).__name__)
                hit = is_stop(said)
                log("alarm hear %s" % (said or "-"))
            data = b"stop\n" if hit else b"no\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/v1/stop":
            length = int(self.headers.get("Content-Length") or "0")
            if 0 < length <= 64:
                self.rfile.read(length)
            if os.environ.get("YSE_PLAY") == "http":
                body = b'{"was":true}' if cut_turn() else b'{"was":false}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            threading.Thread(target=speaker_text, daemon=True, args=(
                "[ -f /tmp/alarm.ring ] && { touch %s/alarm.stop; "
                "pid=$(cat /tmp/alarm.play 2>/dev/null); [ -n \"$pid\" ] && kill $pid 2>/dev/null; true; }"
                % ROOT,)).start()
            with turn_lock:
                speaking = bool(turn["busy"])
            if speaking:
                body = b'{"was":false,"busy":true}'
            else:
                body = b'{"was":true,"busy":false}' if cut_turn() else b'{"was":false,"busy":false}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/v1/alarm":
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length).decode("utf-8", "replace").strip() if 0 < length <= 32 else ""
            text = alarm_sentence(body)
            ok = False
            if text:
                log("alarm say")
                with talk_lock:
                    with turn_lock:
                        turn["busy"] = True
                        turn["cut"] = False
                    try:
                        ok = speak(text, person_token() or DEMO_TOKEN)
                    finally:
                        with turn_lock:
                            turn["busy"] = False
            data = b'{"ok":true}' if ok else b'{"ok":false}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/v1/hear":
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > 16000 * 2 * 12:
                self.send_response(400)
                self.end_headers()
                return
            pcm = self.rfile.read(length)
            phrase = ""
            try:
                phrase = recognize(pcm)
            except Exception as exc:
                log("hear fail %s" % type(exc).__name__)
            ok = answer_heard(phrase, pcm)
            data = b'{"ok":true}' if ok else b'{"ok":false}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/v1/settings":
            folder = os.environ.get("YSE_SETTINGS", "").strip()
            if not folder or client not in ("127.0.0.1", SPEAKER[1]):
                self.send_error(403)
                return
            body = self.read_json(4000)
            allowed = {
                "nn": "nn.txt", "news": "news.txt", "stt": "stt.txt",
                "voice": "voice.txt", "wake": "wake.txt", "persona": "persona.txt",
                "news_url": "news.url", "news_model": "news.model", "news_key": "news.key",
            }
            os.makedirs(folder, exist_ok=True)
            for key, name in allowed.items():
                if key not in body:
                    continue
                limit = 3000 if key == "persona" else 300
                value = " ".join(str(body.get(key) or "").split())[:limit]
                if key == "news_key" and not value:
                    continue
                dest = os.path.join(folder, name)
                tmp = dest + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(value)
                os.replace(tmp, dest)
            self.send_json({"ok": True})
            return
        if path == "/v1/speak":
            body = self.read_json(2000)
            if client not in ("127.0.0.1", SPEAKER[1]):
                self.send_error(403)
                return
            said = " ".join(str(body.get("text") or "").split())[:400]
            if not said:
                self.send_json({"ok": False, "error": "text"})
                return

            def work():
                with talk_lock:
                    with turn_lock:
                        turn["busy"] = True
                        turn["cut"] = False
                    try:
                        ok = speak(said, person_token() or DEMO_TOKEN)
                    finally:
                        with turn_lock:
                            turn["busy"] = False
                log("speak page %s" % ("ok" if ok else "fail"))

            threading.Thread(target=work, daemon=True).start()
            self.send_json({"ok": True})
            return
        if path != "/v1/ask":
            self.send_error(404)
            return
        follow = "follow=1" in query
        nn = brain_name((re.search(r"(?:^|&)nn=(\w+)", query) or [None, ""])[1])
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > 800:
            self.send_response(400)
            self.end_headers()
            return
        phrase = self.rfile.read(length).decode("utf-8", "replace").strip()
        if not phrase:
            self.send_response(400)
            self.end_headers()
            return
        publish_heard(phrase)
        with talk_lock:
            with turn_lock:
                turn["busy"] = True
                turn["cut"] = False
            try:
                ok = reply(phrase, follow, nn)
            finally:
                with turn_lock:
                    turn["busy"] = False
        body = b'{"ok":true}' if ok else b'{"ok":false}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def brain_name(value):
    return value if value in ("ha", "openai", "google") else "yandex"


def ensure_room_settings():
    folder = os.environ.get("YSE_SETTINGS", "").strip()
    if not folder:
        return
    os.makedirs(folder, exist_ok=True)
    defaults = {
        "nn.txt": "openai",
        "news.txt": "openai",
        "stt.txt": "whisper",
        "voice.txt": "own:jarvis",
        "wake.txt": "джарвис",
        "persona.txt": "",
    }
    for name, value in defaults.items():
        dest = os.path.join(folder, name)
        if os.path.exists(dest):
            continue
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(value)


if __name__ == "__main__":
    ensure_room_settings()
    log("talk listening %s" % PORT)
    dialog_load()
    threading.Thread(target=lambda: hear_text(b"\0" * 4000), daemon=True).start()
    threading.Thread(target=room_temp_loop, daemon=True).start()
    threading.Thread(target=stt_warm_loop, daemon=True).start()
    listen = ListenServer((HOST, LISTEN_PORT), ListenHandler)
    threading.Thread(target=listen.serve_forever, daemon=True).start()
    log("listen stream %s" % LISTEN_PORT)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
