# Распознавание речи для колонок. Один код на три контейнера: STT_ENGINE=whisper, vosk или gigaam.
# Протокол как у OpenAI: POST /v1/audio/transcriptions, поле file с WAV 16 кГц моно.
import io
import json
import os
import tempfile
import threading
import time
import wave
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

ENGINE = os.environ.get("STT_ENGINE", "whisper")
PORT = int(os.environ.get("STT_PORT", "8000"))
RATE = 16000
JUNK = (
    "продолжение следует", "субтитры", "спасибо за просмотр", "подписывайтесь",
    "редактор субтитров", "корректор", "dimatorzok",
)
lock = threading.Lock()
state = {"model": None, "name": ""}


def log(text):
    print(text, flush=True)


def load():
    if ENGINE == "vosk":
        from vosk import Model, SetLogLevel
        SetLogLevel(-1)
        path = os.environ.get("VOSK_MODEL", "/models/vosk/vosk-model-ru-0.42")
        state["model"] = Model(path)
        state["name"] = os.path.basename(path)
    elif ENGINE == "gigaam":
        import gigaam
        name = os.environ.get("GIGAAM_MODEL", "v3_e2e_rnnt")
        state["model"] = gigaam.load_model(
            name,
            download_root=os.environ.get("GIGAAM_ROOT", "/models/gigaam"),
            device=os.environ.get("GIGAAM_DEVICE", "cuda"),
            use_flash=False,
        )
        state["name"] = name
    else:
        from faster_whisper import WhisperModel
        name = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
        state["model"] = WhisperModel(
            name,
            device=os.environ.get("WHISPER_DEVICE", "cuda"),
            compute_type=os.environ.get("WHISPER_COMPUTE", "int8_float16"),
            download_root="/models/whisper",
        )
        state["name"] = name
    log("loaded %s %s" % (ENGINE, state["name"]))


def pcm_from_wav(blob):
    with wave.open(io.BytesIO(blob)) as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError("need 16-bit wav")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != RATE and len(samples):
        span = np.arange(0, len(samples), rate / RATE)
        samples = np.interp(span, np.arange(len(samples)), samples)
    return samples.astype(np.float32) / 32768.0


def clean_text(text):
    text = " ".join((text or "").split())
    low = text.lower()
    if any(word in low for word in JUNK):
        return ""
    return text


def transcribe(audio, language, prompt):
    model = state["model"]
    if ENGINE == "gigaam":
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            with wave.open(path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(RATE)
                wav.writeframes(pcm.tobytes())
            with lock:
                text = model.transcribe(path)
        finally:
            os.remove(path)
        if not isinstance(text, str):
            text = getattr(text, "text", "") or ""
        return text.strip()
    if ENGINE == "vosk":
        from vosk import KaldiRecognizer
        rec = KaldiRecognizer(model, RATE)
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        for offset in range(0, len(pcm), 8000):
            rec.AcceptWaveform(pcm[offset:offset + 8000])
        return json.loads(rec.FinalResult()).get("text") or ""
    with lock:
        segments, _ = model.transcribe(
            audio, language=language or "ru", beam_size=5,
            condition_on_previous_text=False, without_timestamps=True,
            vad_filter=False, initial_prompt=prompt or None,
        )
        parts = [seg.text for seg in segments if seg.no_speech_prob < 0.6]
    return clean_text(" ".join(parts))


def form_fields(ctype, body):
    msg = BytesParser(policy=email_policy).parsebytes(
        b"Content-Type: " + ctype.encode("latin-1") + b"\r\n\r\n" + body)
    fields = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            fields[name] = part.get_payload(decode=True) or b""
    return fields


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            self.reply(200, {"ok": state["model"] is not None, "engine": ENGINE, "model": state["name"]})
            return
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/v1/audio/transcriptions"):
            self.reply(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > 8_000_000:
            self.reply(400, {"error": "bad length"})
            return
        body = self.rfile.read(length)
        started = time.time()
        try:
            fields = form_fields(self.headers.get("Content-Type", ""), body)
            audio = pcm_from_wav(fields.get("file", b""))
            language = fields.get("language", b"ru").decode("utf-8", "replace")
            prompt = fields.get("prompt", b"").decode("utf-8", "replace")
            text = transcribe(audio, language, prompt)
        except Exception as exc:
            log("fail %s %s" % (type(exc).__name__, exc))
            self.reply(400, {"error": type(exc).__name__})
            return
        log("heard %.1fs in %.2fs: %s" % (len(audio) / RATE, time.time() - started, text or "-"))
        self.reply(200, {"text": text})

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    load()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
