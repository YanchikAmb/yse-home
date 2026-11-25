# Свой голос колонок. Текст -> цифры словами -> ударения -> Silero 48 кГц -> RVC, если голос обученный.
# Все модели грузятся при старте и держатся в памяти. Ответ можно брать потоком по предложениям.
import io
import json
import os
import re
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import torch

ROOT = os.environ.get("VOICE_ROOT", "/voice")
DEVICE = os.environ.get("VOICE_DEVICE", "cuda:0")
SILERO_FILE = os.path.join(ROOT, "models", os.environ.get("SILERO_FILE", "v5_6_ru.pt"))
SPEAKER_MODEL = os.path.join(ROOT, "models", "voxceleb_resnet34_LM.onnx")
RVC_DIR = os.path.join(ROOT, "rvc")
BASE_SPEAKER = os.environ.get("RVC_BASE_SPEAKER", "eugene")
PITCH = int(os.environ.get("RVC_PITCH", "0"))
SR = 48000
SILERO_SPEAKERS = ("aidar", "baya", "eugene", "kseniya", "xenia")
gpu = threading.Lock()
ear = threading.Lock()
state = {"silero": None, "silero_args": {}, "accent": None, "rvc": {}, "engine": None, "speaker": None,
         "ready": False}


def log(text):
    print(text, flush=True)


def load_silero():
    torch.set_num_threads(4)
    model = torch.package.PackageImporter(SILERO_FILE).load_pickle("tts_models", "model")
    model.to(torch.device("cpu"))
    state["silero"] = model
    import inspect
    known = inspect.signature(model.apply_tts).parameters
    state["silero_args"] = {key: True for key in ("put_accent", "put_yo", "put_stress_homo", "put_yo_homo")
                            if key in known}
    log("silero %s %s" % (os.path.basename(SILERO_FILE), " ".join(sorted(state["silero_args"]))))


def load_accent():
    """silero-stress нужен только v4: v5 сам ставит ударения и в омографах (put_stress_homo)."""
    if "put_stress_homo" in state["silero_args"] and os.environ.get("SILERO_ACCENT") != "1":
        log("accent built-in v5")
        return
    try:
        from silero_stress import load_accentor
        state["accent"] = load_accentor()
        log("accent silero-stress")
    except Exception as exc:
        log("accent built-in (%s)" % type(exc).__name__)


VC_PARTS = ("net_g", "tgt_sr", "if_f0", "version", "pipeline")


def load_rvc():
    """Один движок RVC: hubert и rmvpe в памяти один раз, у голоса только своя сеть (~55 МБ)."""
    if not os.path.isdir(RVC_DIR):
        return
    from rvc_python.infer import RVCInference
    from rvc_python.modules.vc import modules

    plain = modules.load_audio
    modules.load_audio = lambda src, sr: src if isinstance(src, np.ndarray) else plain(src, sr)
    engine = None
    pipelines = {}
    for name in sorted(os.listdir(RVC_DIR)):
        pth = os.path.join(RVC_DIR, name, name + ".pth")
        if not os.path.isfile(pth):
            continue
        index = ""
        for item in os.listdir(os.path.join(RVC_DIR, name)):
            if item.endswith(".index"):
                index = os.path.join(RVC_DIR, name, item)
        if engine is None:
            engine = RVCInference(device=DEVICE)
        engine.load_model(pth, index_path=index)
        vc = engine.vc
        vc.pipeline = pipelines.setdefault(vc.tgt_sr, vc.pipeline)
        vc.cpt = None
        parts = {key: getattr(vc, key) for key in VC_PARTS}
        parts["index"] = index
        state["rvc"][name] = parts
        log("rvc %s %d Hz%s" % (name, vc.tgt_sr, " +index" if index else ""))
    state["engine"] = engine
    if engine is not None:
        load_rvc_base(engine, pipelines.values())


def load_rvc_base(engine, pipelines):
    """hubert и rmvpe сразу на видеокарту; внутри rvc-python их копии оставались в RAM (+0,7 ГБ)."""
    import ctypes
    import gc
    from fairseq import checkpoint_utils
    from rvc_python.lib.rmvpe import RMVPE

    vc = engine.vc
    base = os.path.join(engine.lib_dir, "base_model")
    models, _cfg, _task = checkpoint_utils.load_model_ensemble_and_task([os.path.join(base, "hubert_base.pt")],
                                                                        suffix="")
    hubert = models[0].to(vc.config.device)
    vc.hubert_model = (hubert.half() if vc.config.is_half else hubert.float()).eval()
    del models, _cfg, _task, hubert
    rmvpe = RMVPE(os.path.join(base, "rmvpe.pt"), is_half=vc.config.is_half, device=vc.config.device)
    for pipeline in pipelines:
        pipeline.model_rmvpe = rmvpe
    gc.collect()
    ctypes.CDLL("libc.so.6").malloc_trim(0)


UNITS = ((r"\s*%", " процентов"), (r"\s*°C?", " градусов"), (r"\bм/с\b", "метров в секунду"),
         (r"\bкм/ч\b", "километров в час"), (r"\bмм\b", "миллиметров"))


def words_for_numbers(text):
    from num2words import num2words

    def one(match):
        sign, whole, frac = match.group(1), match.group(2), match.group(4)
        try:
            said = num2words(int(whole), lang="ru")
            if frac:
                said += " " + num2words(int(frac), lang="ru")
        except (ValueError, OverflowError):
            return match.group(0)
        return ("минус " if sign else "") + said

    for pattern, word in UNITS:
        text = re.sub(pattern, word, text)
    text = re.sub(r"(?<![\w])(-)?(\d+)([.:,](\d+))?", one, text)
    return text.replace("—", ", ").replace("–", ", ")


def sentences(text):
    text = " ".join(words_for_numbers(text).split())
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]
    out = []
    for part in parts:
        while len(part) > 280:
            cut = part.rfind(",", 0, 280)
            cut = cut if cut > 40 else part.rfind(" ", 0, 280)
            out.append(part[:cut + 1].strip())
            part = part[cut + 1:].strip()
        if out and len(out[-1]) < 25:
            out[-1] = out[-1] + " " + part
        else:
            out.append(part)
    return out


def silero_marks(text):
    """Характер ставит + после ударной гласной. Silero обучен на плюсе перед ней:
    «дома+шние» он читает как ударение на следующей гласной."""
    moved = {"n": 0}

    def shift(match):
        moved["n"] += 1
        return "+" + match.group(1)

    text = re.sub(r"([аеёиоуыэюяАЕЁИОУЫЭЮЯ])\+", shift, text)
    if moved["n"]:
        log("stress moved %d" % moved["n"])
    return text


def silero(text, speaker):
    text = silero_marks(text)
    if state["accent"] is not None:
        try:
            text = state["accent"](text)
        except Exception:
            pass
    audio = state["silero"].apply_tts(text=text, speaker=speaker, sample_rate=SR, **state["silero_args"])
    return audio.numpy().astype(np.float32)


def load_speaker():
    """WeSpeaker ResNet34 (onnx, CPU): отпечаток голоса 256 чисел, сходство — скалярное произведение."""
    if not os.path.isfile(SPEAKER_MODEL):
        log("speaker id off: нет %s" % SPEAKER_MODEL)
        return
    import onnxruntime
    opts = onnxruntime.SessionOptions()
    opts.intra_op_num_threads = 2
    state["speaker"] = onnxruntime.InferenceSession(SPEAKER_MODEL, sess_options=opts,
                                                    providers=["CPUExecutionProvider"])
    log("speaker id %s" % os.path.basename(SPEAKER_MODEL))


def embed(pcm):
    import torchaudio
    wave16 = torch.from_numpy(np.frombuffer(pcm, dtype=np.int16).astype(np.float32)).unsqueeze(0)
    feats = torchaudio.compliance.kaldi.fbank(wave16, num_mel_bins=80, frame_length=25, frame_shift=10,
                                              dither=0.0, sample_frequency=16000, window_type="hamming",
                                              use_energy=False)
    feats = feats - feats.mean(dim=0)
    with ear:
        out = state["speaker"].run(None, {"feats": feats.unsqueeze(0).numpy()})[0][0]
    return out / (np.linalg.norm(out) + 1e-9)


def resample(audio, src, dst):
    import torchaudio
    return torchaudio.functional.resample(torch.from_numpy(audio), src, dst).numpy()


def rvc(audio, name):
    parts = state["rvc"][name]
    vc = state["engine"].vc
    for key in VC_PARTS:
        setattr(vc, key, parts[key])
    out = vc.vc_single(sid=0, input_audio_path=resample(audio, SR, 16000), f0_up_key=PITCH,
                       f0_file="", f0_method="rmvpe", file_index=parts["index"], file_index2="",
                       index_rate=0.5 if parts["index"] else 0.0, filter_radius=3, resample_sr=0,
                       rms_mix_rate=1, protect=0.33)
    if not isinstance(out, np.ndarray):
        raise RuntimeError("rvc: %s" % str(out)[-300:])
    out = out.astype(np.float32) / 32768.0
    return out if parts["tgt_sr"] == SR else resample(out, parts["tgt_sr"], SR)


def say(sentence, voice):
    """Одно предложение -> float32 48 кГц."""
    with gpu:
        if voice in SILERO_SPEAKERS:
            return silero(sentence, voice)
        if voice not in state["rvc"]:
            raise KeyError(voice)
        return rvc(silero(sentence, BASE_SPEAKER), voice)


def voices():
    return list(SILERO_SPEAKERS) + sorted(state["rvc"])


def pcm16(audio):
    return (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def warm():
    for voice in voices():
        started = time.time()
        say("Проверка голоса.", voice)
        log("warm %s %.2fs" % (voice, time.time() - started))


def keep_warm():
    while True:
        time.sleep(600)
        try:
            for voice in sorted(state["rvc"]):
                say("Да.", voice)
        except Exception as exc:
            log("keep warm fail %s" % exc)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200 if state["ready"] else 503, {"ok": state["ready"], "voices": voices(),
                                                            "device": DEVICE, "base": BASE_SPEAKER,
                                                            "silero": os.path.basename(SILERO_FILE),
                                                            "speaker_id": state["speaker"] is not None})
        elif self.path == "/v1/voices":
            self.send_json(200, {"voices": voices()})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or "0")
        if self.path == "/v1/speaker/embed":
            if state["speaker"] is None or not 16000 <= length <= 16000 * 2 * 20:
                self.send_json(400, {"error": "speaker id off or audio not 0.5-20 s s16le 16 kHz"})
                return
            started = time.time()
            vector = embed(self.rfile.read(length))
            self.send_json(200, {"embedding": [round(float(x), 5) for x in vector],
                                 "seconds": length / 32000.0, "ms": int((time.time() - started) * 1000)})
            return
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8")) if 0 < length < 20000 else {}
        except ValueError:
            req = {}
        text = str(req.get("input") or "").strip()
        voice = str(req.get("voice") or BASE_SPEAKER)
        if not state["ready"] or not text or voice not in voices():
            self.send_json(400, {"error": "not ready" if not state["ready"] else "no text or voice",
                                 "voices": voices()})
            return
        started = time.time()
        parts = sentences(text)
        if self.path == "/v1/audio/stream":
            self.send_response(200)
            self.send_header("Content-Type", "audio/L16; rate=48000; channels=1")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            first = None
            try:
                for part in parts:
                    chunk = pcm16(say(part, voice))
                    if first is None:
                        first = time.time() - started
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                log("stream cut")
                return
            log("stream %s %d parts first %.2fs all %.2fs" % (voice, len(parts), first or 0, time.time() - started))
            return
        if self.path != "/v1/audio/speech":
            self.send_json(404, {"error": "not found"})
            return
        audio = np.concatenate([say(part, voice) for part in parts])
        if req.get("response_format") == "pcm":
            body = pcm16(audio)
            kind = "audio/L16; rate=48000; channels=1"
        else:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as fh:
                fh.setnchannels(1)
                fh.setsampwidth(2)
                fh.setframerate(SR)
                fh.writeframes(pcm16(audio))
            body = buf.getvalue()
            kind = "audio/wav"
        log("speech %s %d parts %.2fs for %.1fs audio" % (voice, len(parts), time.time() - started,
                                                         len(audio) / SR))
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def main():
    load_silero()
    load_accent()
    load_speaker()
    load_rvc()
    warm()
    import ctypes
    import gc
    gc.collect()
    ctypes.CDLL("libc.so.6").malloc_trim(0)
    state["ready"] = True
    threading.Thread(target=keep_warm, daemon=True).start()
    rss = [line.split()[1] for line in open("/proc/self/status") if line.startswith("VmRSS")]
    log("voice ready %s, RAM %d MB" % (", ".join(voices()), int(rss[0]) // 1024 if rss else 0))


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    threading.Thread(target=main, daemon=True).start()
    server.serve_forever()
