"""Служба слова вызова.

Колонка шлёт сюда куски 16 кГц моно int16 и фразу выбранной модели.
Ответ уходит на каждый пакет: hit=1, когда фраза узнана.
Обучения здесь нет: фраза задаётся грамматикой уже готовой русской модели Vosk.
"""

import array
import json
import math
import socket
import struct
import subprocess
import sys
import time
import wave
from collections import deque
from pathlib import Path

from vosk import KaldiRecognizer, Model

MAGIC = 0x314B5759
MODEL_DIR = "/opt/vosk-model-small-ru-0.22"
PORT = 18181
PEAK_MIN = 200.0


def load_wav(path):
    with wave.open(path) as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise SystemExit("wav width")
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    if channels == 2:
        samples = array.array("h", (samples[i] for i in range(0, len(samples), 2)))
    if rate == 16000:
        return samples
    out_len = int(len(samples) * 16000 / rate)
    out = array.array("h", [0]) * out_len
    last = len(samples) - 1
    for i in range(out_len):
        src = i * (rate / 16000.0)
        left = int(src)
        if left >= last:
            out[i] = samples[last]
        else:
            frac = src - left
            out[i] = int(samples[left] * (1.0 - frac) + samples[left + 1] * frac)
    return out


class Ear:
    def __init__(self, model):
        self.model = model
        self.phrase = ""
        self.rec = None
        self.recent = deque(maxlen=40)
        self.cool_until = 0.0
        self.last_partial = ""

    def set_phrase(self, phrase):
        phrase = phrase.strip().lower().replace("ё", "е")
        if phrase == self.phrase and self.rec is not None:
            return
        self.phrase = phrase
        self.last_partial = ""
        self.recent.clear()
        if not phrase:
            self.rec = None
            return
        words = [phrase, "алиса", "погода", "включи", "выключи", "привет", "музыка", "свет", "тихо", "стоп"]
        grammar = json.dumps(words, ensure_ascii=False)
        self.rec = KaldiRecognizer(self.model, 16000, grammar)
        self.rec.SetWords(False)

    def feed(self, pcm):
        if self.rec is None or not pcm:
            return False
        count = len(pcm) // 2
        if count <= 0:
            return False
        samples = array.array("h")
        samples.frombytes(pcm[: count * 2])
        if sys.byteorder != "little":
            samples.byteswap()
        energy = 0
        for sample in samples:
            energy += sample * sample
        rms = math.sqrt(energy / count)
        self.recent.append(rms)
        peak = max(self.recent) if self.recent else 0.0
        now = time.monotonic()
        heard = False
        final = self.rec.AcceptWaveform(pcm[: count * 2])
        if final:
            text = json.loads(self.rec.Result()).get("text", "")
        else:
            text = json.loads(self.rec.PartialResult()).get("partial", "")
        text = text.strip().lower().replace("ё", "е")
        if text and text != self.last_partial:
            print("hear text=%s final=%s peak=%.0f" % (text, final, peak), flush=True)
            self.last_partial = text
        words = text.split()
        if self.phrase in words and peak >= PEAK_MIN and now >= self.cool_until:
            heard = True
            self.cool_until = now + 2.0
            self.last_partial = ""
            self.recent.clear()
            self.rec.Reset()
            print("hit phrase=%s peak=%.0f" % (text, peak), flush=True)
        return heard


def synth(phrase, path):
    subprocess.check_call(
        ["espeak-ng", "-v", "ru", "-s", "120", "-w", path, phrase],
        stdout=subprocess.DEVNULL,
    )
    samples = load_wav(path)
    silence = array.array("h", [0]) * 8000
    return samples + silence


def self_test():
    model = Model(MODEL_DIR)
    ear = Ear(model)
    ear.set_phrase("джарвис")
    yes = synth("джарвис", "/tmp/jarvis.wav")
    no = synth("погода на завтра", "/tmp/other.wav")
    hit = False
    step = 1600
    raw = yes.tobytes()
    for offset in range(0, len(raw), step * 2):
        hit = ear.feed(raw[offset : offset + step * 2]) or hit
    ear.set_phrase("джарвис")
    false = False
    raw = no.tobytes()
    for offset in range(0, len(raw), step * 2):
        false = ear.feed(raw[offset : offset + step * 2]) or false
    print("self-test hit=%s false=%s" % (hit, false), flush=True)
    if not hit or false:
        raise SystemExit(2)


def serve():
    model = Model(MODEL_DIR)
    ear = Ear(model)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PORT))
    print("yse-wake udp %d" % PORT, flush=True)
    while True:
        packet, addr = sock.recvfrom(4096)
        if len(packet) < 88:
            continue
        magic, seq = struct.unpack_from("<II", packet, 0)
        if magic != MAGIC:
            continue
        phrase = packet[8:88].split(b"\x00", 1)[0].decode("utf-8", "replace")
        ear.set_phrase(phrase)
        hit = 1 if ear.feed(packet[88:]) else 0
        sock.sendto(struct.pack("<III", MAGIC, seq, hit), addr)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
    elif len(sys.argv) > 1 and sys.argv[1] == "--dump-raw":
        synth("джарвис", "/tmp/jarvis.wav").tofile(Path("/tmp/jarvis.raw").open("wb"))
    else:
        serve()
