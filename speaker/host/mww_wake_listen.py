# -*- coding: utf-8 -*-
"""Ухо «джарвис»: сырой микрофон колонки, уже обученная сеть, кольцо.

Колонка шлёт на UDP 18765: в тишине канал 2 до шумодава, пока сама играет — выход штатного эхоподавления.
Срабатывание — средняя вероятность не ниже 0.9 и окно не тишина. Оценка от 0.97 проходит и на тихом хвосте слова.
`exam` прогоняет запись и ничего не включает.
"""
import collections
import hashlib
import json
import math
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

import numpy as np
import tensorflow as tf
from pymicro_features import MicroFrontend

import wake_collect

MODEL = os.environ.get("YSE_MODEL", "/storage/yse-mww/trainer/ru_jarvis_mww.tflite")
RAW = "/storage/yse-mww/exam/pre_vqe.raw"
BASE = int(os.environ.get("YSE_BASE", "18765"))
PORT = BASE
SPEAKER = os.environ.get("YSE_SPEAKER", "192.168.209.105")
MUSIC_URL = "http://127.0.0.1:%d" % (BASE + 1)
TALK_URL = "http://127.0.0.1:%d" % (BASE + 3)
SAT_URL = "http://127.0.0.1:%d" % (BASE + 4)
CHUNK = 160
STRIDE = 3
CUTOFF = 0.9
RMS_MIN = 3.0
REFRACTORY = 2.0
LIGHT_SEC = 8.0
HIST = 6400

SSH = ["/storage/yse/bin/yse-ssh", SPEAKER]


def log(line):
    print(line, flush=True)


def load_cutoff():
    global CUTOFF
    raw = os.environ.get("YSE_CUTOFF", "")
    try:
        value = float(raw)
    except ValueError:
        return
    if 0.5 <= value <= 0.99:
        CUTOFF = value


class Ear:
    def __init__(self, model=None):
        self.interp = tf.lite.Interpreter(model or MODEL)
        self.interp.allocate_tensors()
        self.on_score = None
        self.inp = self.interp.get_input_details()[0]
        self.out = self.interp.get_output_details()[0]
        self.scale, self.zp = self.inp["quantization"]
        self.oscale, self.ozp = self.out["quantization"]
        self.reset()

    def reset(self):
        self.interp.allocate_tensors()
        self.frontend = MicroFrontend()
        self.buf = []
        self.probs = collections.deque(maxlen=10)
        self.hist = collections.deque()
        self.hot = False
        self.near = False
        self.energy = 0
        self.frame = 0
        self.last_fire = -10.0
        self.last_note = -10.0
        self.last_duck = -10.0
        self.music = False
        self.max_avg = 0.0
        self.max_rms = 0.0
        self.trace = []
        self.keep_trace = False

    def feed(self, samples):
        fires = []
        audio = np.asarray(samples, dtype=np.int16)
        for i in range(0, len(audio) - CHUNK + 1, CHUNK):
            piece = audio[i : i + CHUNK]
            for sample in piece:
                sample = int(sample)
                if len(self.hist) == HIST:
                    old = self.hist.popleft()
                    self.energy -= old * old
                self.hist.append(sample)
                self.energy += sample * sample
            result = self.frontend.process_samples(piece.tobytes())
            if result is None or len(result.features) == 0:
                continue
            self.buf.append(list(result.features))
            self.frame += 1
            if len(self.buf) < STRIDE:
                continue
            window = np.asarray(self.buf, dtype=np.float32)
            self.buf = []
            quant = np.clip(np.round(window / self.scale + self.zp), -128, 127).astype(np.int8)
            self.interp.set_tensor(self.inp["index"], quant[np.newaxis])
            self.interp.invoke()
            raw = self.interp.get_tensor(self.out["index"])
            prob = float((raw.flat[0] - self.ozp) * self.oscale)
            if prob < 0.0:
                prob = 0.0
            elif prob > 1.0:
                prob = 1.0
            self.probs.append(prob)
            avg = sum(self.probs) / len(self.probs)
            rms = math.sqrt(self.energy / len(self.hist)) if self.hist else 0.0
            t = self.frame * 0.01
            if avg > self.max_avg:
                self.max_avg = avg
            if rms > self.max_rms:
                self.max_rms = rms
            if self.keep_trace:
                self.trace.append((t, avg, rms))
            if self.on_score is not None and t >= 1.5:
                self.on_score(avg, rms)
            floor = 1.5 if self.music else RMS_MIN
            loud = rms >= floor or (avg >= 0.95 and rms >= 2.5) or (avg >= 0.97 and rms >= 1.5)
            # Порог тот же. Пока оценка просто висит выше него, колонка не пищит снова.
            # Новый зов — это переход через порог снизу.
            was_hot = self.hot
            if avg < CUTOFF - 0.04:
                self.hot = False
            elif avg >= CUTOFF:
                self.hot = True
            crossed = self.hot and not was_hot
            was_near = self.near
            self.near = avg >= max(0.72, CUTOFF - 0.1)
            if (self.music and self.near and not was_near and rms >= 1.5
                    and t - self.last_fire >= 1.0):
                self.last_duck = t
                fires.append(("duck", t, avg, rms))
            if crossed and loud and t >= 1.5 and t - self.last_fire >= REFRACTORY:
                self.last_fire = t
                fires.append((t, avg, rms))
            elif avg >= CUTOFF and not loud and t - self.last_note >= REFRACTORY:
                self.last_note = t
                fires.append(("quiet", t, avg, rms))
        return fires


def prepare(path):
    raw = np.fromfile(path, dtype=np.int16)
    frames = raw.size // 16
    block = raw[: frames * 16].reshape(frames, 16).astype(np.float32)
    ch = block[:, 2]
    n = (len(ch) // 3) * 3
    x = ch[:n].reshape(-1, 3).mean(axis=1)
    y = np.empty_like(x)
    y[0] = 0.0
    y[1:] = x[1:] - 0.97 * x[:-1]
    return np.clip(np.round(y), -32768, 32767).astype(np.int16)


def region_best(scores, a, b):
    rows = [row for row in scores if a <= row[0] <= b]
    if not rows:
        return None
    return max(rows, key=lambda row: row[1])


def exam():
    audio = prepare(RAW)
    ear = Ear()
    ear.keep_trace = True
    fires = []
    step = 320
    for i in range(0, len(audio), step):
        for item in ear.feed(audio[i : i + step]):
            if item[0] == "quiet":
                log("QUIET t=%.2f avg=%.3f rms=%.0f" % (item[1], item[2], item[3]))
            else:
                fires.append(item)
                log("ACCEPT t=%.2f avg=%.3f rms=%.0f" % item)
    scores = ear.trace
    for a, b in ((0, 8), (10, 18), (18, 26), (26, 36), (64, 75)):
        best = region_best(scores, a, b)
        if best is None:
            log("region %.0f-%.0f empty" % (a, b))
        else:
            log("region %.0f-%.0f best=%.3f at %.2f rms=%.0f" % (a, b, best[1], best[0], best[2]))
    ok = (
        len(fires) == 1
        and 10.0 <= fires[0][0] <= 18.0
        and region_best(scores, 18, 26)[1] < CUTOFF
        and region_best(scores, 26, 36)[1] < CUTOFF
        and region_best(scores, 64, 75)[1] < CUTOFF
        and region_best(scores, 10, 18)[1] >= 0.95
    )
    log("PASS" if ok else "FAIL")
    return 0 if ok else 2


def chime_pcm():
    rate = 48000
    n = int(rate * 0.70)
    notes = (
        (0.00, 0.34, 523.25, 1.00),
        (0.12, 0.40, 659.25, 0.85),
        (0.24, 0.42, 783.99, 0.70),
    )
    buf = bytearray()
    for i in range(n):
        t = i / rate
        sample = 0.0
        for start, length, freq, gain in notes:
            u = (t - start) / length
            if u < 0.0 or u > 1.0:
                continue
            env = u / 0.08 if u < 0.08 else (1.0 - u) ** 1.6
            tone = math.sin(2.0 * math.pi * freq * t)
            tone += 0.18 * math.sin(2.0 * math.pi * freq * 2.0 * t)
            sample += tone * env * gain
        sample *= 0.42
        if sample > 1.0:
            sample = 1.0
        if sample < -1.0:
            sample = -1.0
        buf += struct.pack("<h", int(sample * 12000))
    return bytes(buf)


def push_speaker(text):
    if os.environ.get("YSE_PLAY") != "http":
        return False
    if text.startswith("YSEGO") and os.environ.get("YSE_MAX_STT") == "1":
        try:
            req = urllib.request.Request(
                "http://%s:18080/api/wake" % SPEAKER,
                data=b"",
                method="POST",
            )
            urllib.request.urlopen(req, timeout=1.2).read()
        except OSError as exc:
            log("wake http %s" % exc)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(text.encode("ascii"), (SPEAKER, 18081))
        sock.close()
        return True
    except OSError as exc:
        log("push fail %s" % exc)
        return False


def restart_capture():
    if os.environ.get("YSE_PLAY") == "http":
        log("mic silent, speaker keeps its own capture")
        return
    log("mic silent, restart capture")
    try:
        proc = subprocess.run(
            SSH + ["/etc/init.d/S99house restart"],
            timeout=20,
            check=False,
            capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log("restart capture fail %s" % exc)
        return
    log("restart capture rc=%s" % proc.returncode)


def music_is_playing():
    try:
        with urllib.request.urlopen(MUSIC_URL + "/v1/now", timeout=0.4) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return False
    return data.get("state") == "play"


def music_duck():
    try:
        req = urllib.request.Request(
            MUSIC_URL + "/v1/duck", data=b"down", method="POST"
        )
        with urllib.request.urlopen(req, timeout=0.4) as resp:
            resp.read()
    except OSError:
        log("duck fail")


def post_json(url, body, timeout=0.8):
    try:
        req = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return {}


def talk_stop():
    """(оборвали ответ, ответ сейчас звучит или пишется). Пока ответ идёт, слово модели его не гасит."""
    post_json(SAT_URL + "/v1/stop", b"wake")
    data = post_json(TALK_URL + "/v1/stop", b"wake")
    return bool(data.get("was")), bool(data.get("busy"))


wake_seen = {"at": 0.0, "sat": 0.0}


def sat_take():
    if post_json(SAT_URL + "/v1/wake", ("%.3f" % wake_seen["at"]).encode()).get("taken"):
        wake_seen["sat"] = time.time()
        log("assist")
        if music_is_playing():
            music_duck()


def barge():
    was, busy = talk_stop()
    wake_seen["busy"] = busy
    if busy or not was:
        return
    log("barge")
    if music_is_playing():
        music_duck()
    try:
        subprocess.run(
            SSH + ["echo %d > /data/opt/yandex-station/hear.go" % int(wake_seen["at"] * 1000)],
            timeout=5, check=False, capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log("barge fail %s" % exc)


def led(name):
    if os.environ.get("YSE_PLAY") == "http":
        if name == "barge":
            was, busy = talk_stop()
            wake_seen["busy"] = busy
            if was and not busy:
                log("barge")
            if music_is_playing():
                music_duck()
            push_speaker("YSEGO")
            return
        if name == "take":
            sat_take()
            return
        if name == "listening":
            if wake_seen.get("busy"):
                wake_seen["busy"] = False
                log("led listening held")
                return
            if music_is_playing():
                music_duck()
            push_speaker("YSEGO")
            log("led listening http")
            return
        if name == "idle":
            push_speaker("YSEIDLE")
            log("led idle http")
            return
    if name == "barge":
        barge()
        return
    if name == "take":
        sat_take()
        return
    if name == "listening":
        if wake_seen.get("busy"):
            wake_seen["busy"] = False
            log("led listening held")
            return
        assist = time.time() - wake_seen["sat"] < 3.0
        if not assist and music_is_playing():
            music_duck()
        hear = (
            "rm -f /data/opt/yandex-station/hear.go; "
            if assist
            else "echo %d > /data/opt/yandex-station/hear.go; " % int(wake_seen["at"] * 1000)
        )
        remote = (
            "S=/data/opt/yandex-station/settings; "
            "ring=$(cat $S/ring.txt 2>/dev/null || echo 1); "
            "color=$(cat $S/ring.color 2>/dev/null || echo 3aa0ff); "
            "bright=$(cat $S/ring.bright 2>/dev/null || echo 160); "
            "sound=$(cat $S/wake.sound 2>/dev/null || echo 1); "
            "case $color in "
            "[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]) ;; "
            "*) color=3aa0ff ;; esac; "
            "case $bright in ''|*[!0-9]*) bright=160 ;; esac; "
            "if [ \"$bright\" -gt 255 ]; then bright=160; fi; "
            "effect=$(cat $S/ring.effect 2>/dev/null || echo wave); "
            "case $effect in wave|breathe|spin|fill|spark|shift|comet|aurora|twinkle) ;; *) effect=wave ;; esac; "
            ": > /data/opt/yandex-station/heard.txt; "
            + hear +
            "if [ \"$ring\" != 0 ]; then "
            "printf '%s\\n' \"light $bright\" > /data/opt/yandex-station/house.cmd; "
            "sleep 0.35; "
            "/data/opt/yandex-station/bin/glow $color 7400 $effect $bright >/dev/null 2>&1 & "
            "fi; "
            "if [ \"$sound\" != 0 ]; then "
            "dev=plughw:0,0; grep -q softvol /etc/asound.conf 2>/dev/null && dev=default; "
            "aplay -D \"$dev\" -f S16_LE -r 48000 -c 1 -t raw - >/dev/null 2>&1; "
            "else cat >/dev/null; fi; "
            "echo face:$ring:$color:$bright:$sound:$effect"
        )
        data = chime_pcm()
    else:
        remote = (
            "if [ -f /data/opt/yandex-station/glow.pid ]; then "
            "kill $(cat /data/opt/yandex-station/glow.pid) 2>/dev/null; sleep 0.05; fi; "
            "printf '%s\\n' 'led idle' > /data/opt/yandex-station/house.cmd; "
            "sleep 0.35; "
            "printf '%s\\n' 'light 0' > /data/opt/yandex-station/house.cmd"
        )
        data = None
    try:
        proc = subprocess.run(
            SSH + [remote], input=data, timeout=8, check=False, capture_output=True
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log("led %s fail %s" % (name, exc))
        return
    err = proc.stderr.decode("utf-8", "replace").strip().replace("\n", " ")
    out = proc.stdout.decode("utf-8", "replace").strip().replace("\n", " ")
    log("led %s rc=%s %s %s" % (name, proc.returncode, out, err))


SPEAKER_MODEL = "/data/opt/yandex-station/jarvis/ru_jarvis_mww.tflite"


def model_stamp():
    try:
        st = os.stat(MODEL)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def sync_speaker_model():
    """Своя колонка слушает ту же модель, что сервер. Отличается — кладём и перезапускаем только jarvisd."""
    if os.environ.get("YSE_PLAY") == "http":
        return
    try:
        with open(MODEL, "rb") as src:
            data = src.read()
    except OSError as exc:
        log("model read fail %s" % exc)
        return
    want = hashlib.md5(data).hexdigest()
    probe = (
        "D=/data/opt/yandex-station; [ -x $D/bin/jarvisd ] || { echo nojarvis; exit 0; }; "
        "md5sum %s 2>/dev/null || echo none" % SPEAKER_MODEL
    )
    try:
        proc = subprocess.run(SSH + [probe], timeout=15, check=False, capture_output=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log("model probe fail %s" % exc)
        return
    out = proc.stdout.decode("utf-8", "replace")
    if proc.returncode or "nojarvis" in out or want in out:
        return
    push = (
        "D=/data/opt/yandex-station; mkdir -p $D/jarvis && cat > %s.new && mv %s.new %s && "
        "{ [ -f $D/jarvis.pid ] && kill $(cat $D/jarvis.pid) 2>/dev/null; sleep 1; "
        "/etc/init.d/S99house start; echo yse-done; }" % (SPEAKER_MODEL, SPEAKER_MODEL, SPEAKER_MODEL)
    )
    try:
        proc = subprocess.run(SSH + [push], input=data, timeout=40, check=False, capture_output=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log("model push fail %s" % exc)
        return
    ok = proc.returncode == 0 and b"yse-done" in proc.stdout
    log("model push %s %s" % ("ok" if ok else "fail", want[:8]))


def serve():
    ear = Ear()
    stamp = model_stamp()
    collector = wake_collect.Collector(SPEAKER, BASE + 2, MODEL, lambda: CUTOFF, Ear)
    ear.on_score = collector.score
    threading.Thread(target=sync_speaker_model, daemon=True).start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
    sock.bind(("0.0.0.0", PORT))
    sock.settimeout(1.0)
    load_cutoff()
    log("listening %d cutoff %.2f" % (PORT, CUTOFF))
    expect = None
    last_report = time.time()
    packets = 0
    silent_windows = 0
    last_restart = 0.0
    light_until = 0.0
    light_on = False
    music_at = 0.0
    outside = {"word": "", "at": 0.0}
    lights = queue.Queue()

    def paint():
        while True:
            led(lights.get())

    threading.Thread(target=paint, daemon=True).start()

    def paint_later(name):
        lights.put(name)

    while True:
        try:
            pkt, _addr = sock.recvfrom(2048)
        except socket.timeout:
            pkt = None
        now = time.time()
        if now - music_at > 1.0:
            music_at = now
            ear.music = music_is_playing()
        if pkt is not None and pkt.startswith(b"YSE") and len(pkt) < 100:
            parts = pkt.decode("ascii", "replace").split()
            if parts[0] == "YSEMODE" and len(parts) > 1:
                if outside["word"] != parts[1]:
                    log("wake word %s on speaker" % parts[1])
                outside.update(word=parts[1], at=now)
            elif parts[0] == "YSEWAKE" and len(parts) > 1 and parts[1].isdigit():
                log("WAKE speaker %s" % " ".join(parts[2:]))
                outside.update(word=parts[2] if len(parts) > 2 else outside["word"], at=now)
                wake_seen["at"] = int(parts[1]) / 1000.0
                paint_later("barge")
                paint_later("take")
                light_until = time.time() + LIGHT_SEC
                if not light_on:
                    light_on = True
                    paint_later("listening")
            pkt = None
        if outside["word"] and now - outside["at"] > 10:
            log("wake word back to server")
            outside["word"] = ""
            ear.reset()
        collector.mode = outside["word"] or "server"
        if pkt is not None and len(pkt) == 4 + 320 * 2:
            seq = struct.unpack_from("<I", pkt, 0)[0]
            if expect is None or seq != expect:
                if expect is not None:
                    log("gap %s -> %s" % (expect, seq))
                    ear.reset()
            expect = (seq + 1) & 0xFFFFFFFF
            samples = np.frombuffer(pkt, dtype="<i2", count=320, offset=4)
            packets += 1
            collector.push(samples)
            # Слово выбрано на колонке: модель считает только ради сбора трудных случаев, сама ничего не будит.
            fires = ear.feed(samples)
            if outside["word"]:
                fires = []
            for item in fires:
                if item[0] == "duck":
                    log("duck near avg=%.3f rms=%.0f" % (item[2], item[3]))
                    music_duck()
                    continue
                if item[0] == "quiet":
                    log("quiet t=%.2f avg=%.3f rms=%.0f" % (item[1], item[2], item[3]))
                    continue
                log("WAKE t=%.2f avg=%.3f rms=%.0f" % item)
                wake_seen["at"] = time.time()
                paint_later("barge")
                paint_later("take")
                light_until = time.time() + LIGHT_SEC
                if not light_on:
                    light_on = True
                    paint_later("listening")
        if light_on and time.time() >= light_until:
            light_on = False
            paint_later("idle")
        if now - last_report >= 10.0:
            log(
                "alive packets=%d max_avg=%.3f max_rms=%.0f"
                % (packets, ear.max_avg, ear.max_rms)
            )
            if packets > 100 and ear.max_rms == 0.0 and not outside["word"]:
                silent_windows += 1
            else:
                silent_windows = 0
            if silent_windows >= 3 and now - last_restart > 120:
                last_restart = now
                silent_windows = 0
                threading.Thread(target=restart_capture, daemon=True).start()
            packets = 0
            ear.max_avg = 0.0
            ear.max_rms = 0.0
            last_report = now
            fresh = model_stamp()
            if fresh is not None and fresh != stamp:
                stamp = fresh
                log("model changed, reload")
                music = ear.music
                ear = Ear()
                ear.music = music
                ear.on_score = collector.score
                collector.reload()
                threading.Thread(target=sync_speaker_model, daemon=True).start()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "exam":
        return exam()
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
