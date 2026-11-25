# -*- coding: utf-8 -*-
"""Живые примеры для дообучения слова. Берутся из того же потока, что слушает yse-wake.

Страница колонки ходит сюда через свой /api/train/* (ключ проверяет колонка) на порт base+2.
Пускаются только колонка этой комнаты и сам сервер. Всё лежит в /storage/yse-mww/real:

  words.json                                   слова, которые можно дообучать: модель, порог
  words/<slug>/say/<комната>/<человек>/*.wav   слово по подсказке: 1.5 с до подсказки и 3 с после
  words/<slug>/hard/<комната>/*.wav + .json    модель почти сработала или сработала; метка со страницы
  other/<комната>/<человек>/*.wav              обычные фразы по подсказке — «не слово»
  bg/<комната>/*.wav + .json                   фон по выключателю, по минуте; удаляется после обучения
  bg.until                                     до какого времени пишется фон (во всех комнатах)
  job.request, job.json                        очередь и состояние обучения (служба yse-train)

Комната — последний байт адреса колонки.
"""
import http.server
import json
import os
import pathlib
import queue
import random
import re
import threading
import time
import urllib.parse
import wave

import numpy as np

ROOT = pathlib.Path(os.environ.get("YSE_REAL", "/storage/yse-mww/real"))
RATE = 16000
KEEP = RATE * 40
PRE = int(RATE * 1.5)
TAKE_SEC = {"say": 3.0, "other": 5.0}
HARD_FROM = 0.5
HARD_END = 0.3
HARD_GAP = 8.0
HARD_OPEN_MAX = 400
FLAT_EVERY = 120.0
BG_CHUNK = RATE * 60
BG_ROOM_MAX = 12 * 60
BG_HOURS_MAX = 12
SAY_MIN = 30
DEFAULT_WORDS = {
    "джарвис": {
        "slug": "ru_jarvis",
        "model": "/storage/yse-mww/trainer/ru_jarvis_mww.tflite",
        "cutoff": 0.85,
        "similar": ["джаз", "Ярвис", "Харви", "сервис", "Джек", "жарко", "пожалуйста", "Денис", "завис"],
    },
}
OTHER_PROMPTS = [
    "Включи свет на кухне",
    "Который час",
    "Какая завтра погода",
    "Поставь будильник на семь утра",
    "Сделай погромче",
    "Давай ужинать",
    "Где мой телефон",
    "Сегодня жарко",
    "Выключи музыку",
    "Расскажи анекдот",
    "Алиса, включи музыку",
    "Яндекс, что нового",
    "Позвони маме",
    "Я скоро приду",
    "Закрой окно, пожалуйста",
    "Посмотрим фильм вечером",
    "Купи хлеба и молока",
    "Доброе утро",
    "Спокойной ночи",
    "Привет, как дела",
]


def log(line):
    print(line, flush=True)


def slug_ok(text):
    return bool(re.fullmatch(r"[a-z0-9_]{1,40}", text or ""))


def clean_person(text):
    text = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "_", (text or "").strip())[:24].strip("_")
    return text or "гость"


def load_words():
    path = ROOT / "words.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data:
            return data
    except (OSError, ValueError):
        pass
    ROOT.mkdir(parents=True, exist_ok=True)
    write_json(path, DEFAULT_WORDS)
    return json.loads(json.dumps(DEFAULT_WORDS))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_wav(path, audio):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with wave.open(str(tmp), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(np.asarray(audio, dtype="<i2").tobytes())
    tmp.replace(path)


def stamp_name():
    now = time.time()
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-%03d" % int((now % 1) * 1000)


def count_wavs(folder):
    if not folder.is_dir():
        return 0
    return sum(1 for _ in folder.rglob("*.wav"))


def frame_rms(audio, size=1600):
    n = len(audio) // size
    if n == 0:
        return np.zeros(1)
    block = audio[: n * size].astype(np.float64).reshape(n, size)
    return np.sqrt((block * block).mean(axis=1))


def speech_check(audio):
    """(годится, почему). Поток после preemph: тишина — единицы, голос с метра — десятки."""
    head = frame_rms(audio[:PRE])
    tail = frame_rms(audio[PRE:])
    floor = float(np.percentile(np.concatenate([head, tail]), 20))
    peak = float(tail.max())
    if int((np.abs(audio[PRE:].astype(np.int32)) >= 32000).sum()) > 20:
        return False, "слишком громко, отойдите подальше", peak
    if peak < 6.0 or peak < 1.8 * floor + 2.0:
        return False, "не слышно, скажите громче или ближе", peak
    return True, "", peak


class Collector:
    def __init__(self, speaker, port, model, cutoff_fn, ear_cls):
        self.speaker = speaker
        self.room = speaker.rsplit(".", 1)[-1]
        self.port = port
        self.model = model
        self.cutoff_fn = cutoff_fn
        self.ear_cls = ear_cls
        self.mode = "server"
        self.lock = threading.Lock()
        self.buf = np.zeros(KEEP, dtype=np.int16)
        self.pos = 0
        self.takes = {}
        self.work = queue.Queue()
        self.event = None
        self.last_hard = -HARD_GAP * RATE
        self.bg_on = False
        self.bg_checked = 0.0
        self.bg_parts = []
        self.bg_n = 0
        self.bg_hot = []
        self.scorers = {}
        self.word = None
        self.open_hard = 0
        self.last_flat = 0.0
        self.reload()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._serve, daemon=True).start()

    def reload(self):
        """Какое слово слушает модель сервера — для него и копим трудные случаи."""
        words = load_words()
        self.word = None
        for word, info in words.items():
            if os.path.realpath(info.get("model", "")) == os.path.realpath(self.model):
                self.word = word
                break
        self.scorers = {}
        if self.word:
            folder = ROOT / "words" / words[self.word]["slug"] / "hard" / self.room
            self.open_hard = sum(
                1 for p in folder.glob("*.json") if (read_json(p, {}) or {}).get("label") is None
            ) if folder.is_dir() else 0

    # --- поток ---

    def push(self, samples):
        samples = np.asarray(samples, dtype=np.int16)
        n = len(samples)
        with self.lock:
            at = self.pos % KEEP
            first = min(n, KEEP - at)
            self.buf[at:at + first] = samples[:first]
            if first < n:
                self.buf[: n - first] = samples[first:]
            self.pos += n
            pos = self.pos
            for tid, take in list(self.takes.items()):
                if not take["queued"] and pos >= take["end"]:
                    take["queued"] = True
                    self.work.put(("take", tid, self._cut(take["begin"], take["end"])))
        self._background(samples)
        event = self.event
        if event is not None and event.get("closing") and pos >= event["cut_end"]:
            self.event = None
            self.work.put(("hard", event, self._cut(event["cut_begin"], event["cut_end"])))

    def _cut(self, begin, end):
        """Отсчёты [begin, end) по счёту с начала потока; что уже затёрто — нулями."""
        out = np.zeros(end - begin, dtype=np.int16)
        lo = max(begin, self.pos - KEEP)
        for i in range(lo, end, 4096):
            j = min(end, i + 4096)
            a, b = i % KEEP, j % KEEP
            if a < b:
                out[i - begin:j - begin] = self.buf[a:b]
            else:
                head = KEEP - a
                out[i - begin:i - begin + head] = self.buf[a:]
                out[i - begin + head:j - begin] = self.buf[:b]
        return out

    def score(self, avg, _rms):
        """Каждый шаг модели сервера (30 мс). Всплеск от 0.5 — трудный случай: и сработки, и почти-сработки."""
        if not self.word:
            return
        pos = self.pos
        event = self.event
        if event is None:
            if avg >= HARD_FROM and pos - self.last_hard >= HARD_GAP * RATE:
                self.event = {"peak": avg, "at": pos, "quiet": None, "mode": self.mode}
            return
        if event.get("closing"):
            return
        if avg > event["peak"]:
            event["peak"] = avg
            event["at"] = pos
        if avg < HARD_END:
            if event["quiet"] is None:
                event["quiet"] = pos
        else:
            event["quiet"] = None
        done = event["quiet"] is not None and pos - event["quiet"] >= RATE // 2
        if done or pos - event["at"] > 3 * RATE:
            event["closing"] = True
            event["cut_begin"] = event["at"] - int(2.5 * RATE)
            event["cut_end"] = max(pos, event["at"] + RATE)
            self.last_hard = pos
            if self.bg_on:
                self.bg_hot.append([round((self.bg_n - (pos - event["at"])) / RATE, 2), round(event["peak"], 3)])

    def _background(self, samples):
        now = time.time()
        if now - self.bg_checked > 5.0:
            self.bg_checked = now
            until = bg_until()
            on = until > now and count_wavs(ROOT / "bg" / self.room) < BG_ROOM_MAX
            if on != self.bg_on:
                log("train bg %s" % ("on" if on else "off"))
                if not on:
                    self.bg_parts, self.bg_n, self.bg_hot = [], 0, []
            self.bg_on = on
        if not self.bg_on:
            return
        self.bg_parts.append(samples.copy())
        self.bg_n += len(samples)
        if self.bg_n >= BG_CHUNK:
            audio = np.concatenate(self.bg_parts)
            self.work.put(("bg", {"hot": self.bg_hot, "mode": self.mode}, audio))
            self.bg_parts, self.bg_n, self.bg_hot = [], 0, []

    # --- запись по подсказке ---

    def start_take(self, kind, word, person):
        sec = TAKE_SEC[kind]
        with self.lock:
            now_pos = self.pos
            for tid in [t for t, take in self.takes.items() if take["expires"] < time.time()]:
                self.takes.pop(tid, None)
            tid = "%s-%s-%s" % (kind, self.room, stamp_name())
            self.takes[tid] = {
                "kind": kind, "word": word, "person": person, "queued": False,
                "begin": now_pos - PRE, "end": now_pos + int(sec * RATE),
                "deadline": time.time() + sec + 4.0, "expires": time.time() + 120.0,
                "result": None,
            }
        return tid, sec

    def take_result(self, tid):
        with self.lock:
            take = self.takes.get(tid)
            if take is None:
                return {"done": True, "ok": False, "why": "запись потерялась"}
            if take["result"] is not None:
                return take["result"]
            if time.time() > take["deadline"] and not take["queued"]:
                take["result"] = {"done": True, "ok": False, "why": "нет звука с колонки"}
                return take["result"]
        return {"done": False}

    def _scorer(self, word):
        info = load_words().get(word) or {}
        model = info.get("model", "")
        if not model or not os.path.isfile(model):
            return None
        ear = self.scorers.get(model)
        if ear is None:
            ear = self.ear_cls(model)
            self.scorers[model] = ear
        return ear

    def _peak_score(self, word, audio):
        ear = self._scorer(word)
        if ear is None:
            return None
        best = [0.0]

        def keep(avg, _rms):
            if avg > best[0]:
                best[0] = avg

        ear.reset()
        ear.on_score = keep
        ear.feed(audio)
        ear.on_score = None
        return round(best[0], 3)

    def _finish_take(self, tid, audio):
        with self.lock:
            take = self.takes.get(tid)
        if take is None:
            return
        words = load_words()
        ok, why, peak = speech_check(audio)
        result = {"done": True, "ok": ok, "why": why, "level": round(peak, 1)}
        if take["kind"] == "say":
            result["score"] = self._peak_score(take["word"], audio)
            folder = ROOT / "words" / words[take["word"]]["slug"] / "say"
        else:
            result["score"] = self._peak_score(self.word, audio) if self.word else None
            folder = ROOT / "other"
        if ok:
            path = folder / self.room / take["person"] / (tid + ".wav")
            write_wav(path, audio)
            result["id"] = tid
            log("train %s %s %s level %.0f score %s" % (take["kind"], take["person"], tid, peak, result["score"]))
        with self.lock:
            take["result"] = result

    def _worker(self):
        while True:
            kind, meta, audio = self.work.get()
            try:
                if kind == "take":
                    self._finish_take(meta, audio)
                elif kind == "hard":
                    self._save_hard(meta, audio)
                elif kind == "bg":
                    name = stamp_name()
                    write_wav(ROOT / "bg" / self.room / (name + ".wav"), audio)
                    write_json(ROOT / "bg" / self.room / (name + ".json"), meta)
            except Exception as exc:  # noqa: BLE001 — сбор не должен ронять ухо
                log("train %s fail %s: %s" % (kind, type(exc).__name__, exc))

    def _save_hard(self, event, audio):
        if not self.word:
            return
        # Ровный шум без всплеска громкости: слова там быть не могло — сразу «не слово», без вопроса человеку.
        levels = frame_rms(audio)
        flat = float(levels.max()) < 2.0 * float(np.median(levels)) + 2.0
        now = time.time()
        if flat:
            if now - self.last_flat < FLAT_EVERY:
                return
            self.last_flat = now
        elif self.open_hard >= HARD_OPEN_MAX:
            return
        words = load_words()
        info = words[self.word]
        kind = "hit" if event["peak"] >= float(info.get("cutoff", self.cutoff_fn())) else "near"
        name = "hard-%s-%s" % (self.room, stamp_name())
        folder = ROOT / "words" / info["slug"] / "hard" / self.room
        write_wav(folder / (name + ".wav"), audio)
        meta = {"peak": round(event["peak"], 3), "kind": kind, "mode": event["mode"], "at": now, "label": None}
        if flat:
            meta.update(label="no", auto="ровный шум")
        write_json(folder / (name + ".json"), meta)
        if not flat:
            self.open_hard += 1
        log("train hard %s %.3f %s%s" % (kind, event["peak"], event["mode"], " flat" if flat else ""))

    # --- HTTP ---

    def _serve(self):
        collector = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _allowed(self):
                return self.client_address[0] in (collector.speaker, "127.0.0.1")

            def _send(self, code, data, ctype="application/json; charset=utf-8"):
                body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def _route(self, method):
                if not self._allowed():
                    self._send(403, {"error": "чужой адрес"})
                    return
                url = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(url.query))
                body = {}
                if method == "POST":
                    size = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(min(size, 65536)) if size > 0 else b""
                    try:
                        body = json.loads(raw.decode("utf-8") or "{}")
                    except ValueError:
                        body = {}
                    if not isinstance(body, dict):
                        body = {}
                try:
                    code, data, ctype = collector.handle(method, url.path, query, body)
                except Exception as exc:  # noqa: BLE001
                    log("train http %s fail %s: %s" % (url.path, type(exc).__name__, exc))
                    code, data, ctype = 500, {"error": "сбой сервера"}, None
                self._send(code, data, ctype or "application/json; charset=utf-8")

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

        try:
            server = http.server.ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        except OSError as exc:
            log("train http bind %d fail %s" % (self.port, exc))
            return
        server.daemon_threads = True
        log("train http %d" % self.port)
        server.serve_forever()

    def handle(self, method, path, query, body):
        words = load_words()
        word = body.get("word") or query.get("word") or next(iter(words))
        if word not in words:
            return 400, {"error": "такого слова нет"}, None
        info = words[word]
        if method == "GET" and path == "/v1/train/state":
            return 200, self.state(word, info, words), None
        if method == "POST" and path in ("/v1/train/say", "/v1/train/other"):
            kind = path.rsplit("/", 1)[-1]
            person = clean_person(body.get("person"))
            tid, sec = self.start_take(kind, word, person)
            data = {"id": tid, "sec": sec}
            if kind == "other":
                pool = OTHER_PROMPTS + list(info.get("similar") or [])
                data["prompt"] = random.choice(pool)
            return 200, data, None
        if method == "GET" and path == "/v1/train/take":
            return 200, self.take_result(query.get("id", "")), None
        if method == "POST" and path == "/v1/train/drop":
            return self.drop_take(body.get("id", ""), info)
        if method == "POST" and path == "/v1/train/bg":
            on = bool(body.get("on"))
            hours = min(BG_HOURS_MAX, max(1, int(body.get("hours") or 4)))
            path_until = ROOT / "bg.until"
            ROOT.mkdir(parents=True, exist_ok=True)
            path_until.write_text("%d\n" % (time.time() + hours * 3600 if on else 0), encoding="utf-8")
            self.bg_checked = 0.0
            return 200, {"on": on, "until": bg_until()}, None
        if method == "GET" and path == "/v1/train/hard":
            return 200, {"items": hard_items(info["slug"])}, None
        if method == "GET" and path == "/v1/train/clip":
            target = hard_path(info["slug"], query.get("id", ""))
            if target is None or not target.with_suffix(".wav").is_file():
                return 404, {"error": "нет записи"}, None
            return 200, target.with_suffix(".wav").read_bytes(), "audio/wav"
        if method == "POST" and path == "/v1/train/label":
            target = hard_path(info["slug"], body.get("id", ""))
            label = body.get("label")
            if target is None or label not in ("yes", "no", "skip"):
                return 400, {"error": "метка"}, None
            meta = read_json(target, None)
            if meta is None:
                return 404, {"error": "нет записи"}, None
            meta["label"] = label
            write_json(target, meta)
            if target.parent.name == self.room:
                self.open_hard = max(0, self.open_hard - 1)
            return 200, {"ok": True}, None
        if method == "POST" and path in ("/v1/train/start", "/v1/train/apply", "/v1/train/rollback"):
            action = path.rsplit("/", 1)[-1]
            job = read_json(ROOT / "job.json", {}) or {}
            if job.get("state") == "running" or (ROOT / "job.request").exists():
                return 409, {"error": "обучение уже идёт"}, None
            if action == "start":
                said = count_wavs(ROOT / "words" / info["slug"] / "say")
                if said < SAY_MIN:
                    return 400, {"error": "мало записей слова: %d из %d" % (said, SAY_MIN)}, None
            if action == "apply" and not (job.get("result") or {}).get("candidate"):
                return 400, {"error": "нечего применять"}, None
            write_json(ROOT / "job.request", {"action": action, "word": word, "room": self.room, "at": time.time()})
            return 200, {"ok": True}, None
        return 404, {"error": "нет такого"}, None

    def drop_take(self, tid, info):
        if not re.fullmatch(r"(say|other)-\d+-[0-9-]+", tid or ""):
            return 400, {"error": "id"}, None
        base = ROOT / "words" / info["slug"] / "say" if tid.startswith("say") else ROOT / "other"
        for path in base.glob("*/*/%s.wav" % tid):
            path.unlink()
            return 200, {"ok": True}, None
        return 404, {"error": "нет записи"}, None

    def state(self, word, info, words):
        slug = info["slug"]
        say = ROOT / "words" / slug / "say"
        people = {}
        if say.is_dir():
            for path in say.glob("*/*/*.wav"):
                people[path.parent.name] = people.get(path.parent.name, 0) + 1
        other = {}
        if (ROOT / "other").is_dir():
            for path in (ROOT / "other").glob("*/*/*.wav"):
                other[path.parent.name] = other.get(path.parent.name, 0) + 1
        bg_rooms = {}
        if (ROOT / "bg").is_dir():
            for folder in (ROOT / "bg").iterdir():
                if folder.is_dir():
                    bg_rooms[folder.name] = count_wavs(folder)
        hard = {"open": 0, "yes": 0, "no": 0}
        for meta_path in (ROOT / "words" / slug / "hard").glob("*/*.json"):
            label = (read_json(meta_path, {}) or {}).get("label")
            if label is None:
                hard["open"] += 1
            elif label in ("yes", "no"):
                hard[label] += 1
        job = read_json(ROOT / "job.json", {}) or {}
        if job.get("word") not in (None, word):
            job = {"state": "idle", "other_word": job.get("word"), "other_state": job.get("state")}
        if (ROOT / "job.request").exists() and job.get("state") != "running":
            job = dict(job, state="queued")
        return {
            "room": self.room, "word": word, "words": list(words), "say_min": SAY_MIN,
            "people": people, "other": other,
            "bg": {"until": bg_until(), "minutes": bg_rooms, "max_hours": BG_HOURS_MAX},
            "hard": hard, "job": job,
            "model": {"cutoff": info.get("cutoff"), "history": (info.get("history") or [])[-5:]},
        }


def bg_until():
    try:
        return float((ROOT / "bg.until").read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0.0


def hard_path(slug, item_id):
    if not re.fullmatch(r"hard-\d+-[0-9-]+", item_id or ""):
        return None
    room = item_id.split("-")[1]
    return ROOT / "words" / slug / "hard" / room / (item_id + ".json")


def hard_items(slug, limit=40):
    items = []
    for meta_path in sorted((ROOT / "words" / slug / "hard").glob("*/*.json"), reverse=True):
        meta = read_json(meta_path, {}) or {}
        if meta.get("label") is not None:
            continue
        items.append({
            "id": meta_path.stem, "room": meta_path.parent.name, "peak": meta.get("peak"),
            "kind": meta.get("kind"), "mode": meta.get("mode"), "at": meta.get("at"),
        })
        if len(items) >= limit:
            break
    return items
