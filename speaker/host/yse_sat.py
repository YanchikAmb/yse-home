# Голосовой спутник Home Assistant для колонки. Протокол Wyoming, HA подключается сам.
# Слово ловит mww_wake_listen.py. Если на колонке выбрана сеть «assist», звук после слова
# идёт в Assist (в HA нужно распознавание речи), а ответ HA играет колонка. При «ha» звук
# в HA не идёт: фразу разбирает колонка, текст в HA отправляет yse_talk.py.
# Фразы и ответы в журнал не пишутся.
import asyncio
import audioop
import json
import os
import subprocess
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wyoming.asr import Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, Satellite
from wyoming.pipeline import PipelineStage, RunPipeline
from wyoming.ping import Ping, Pong
from wyoming.satellite import PauseSatellite, RunSatellite
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.snd import Played
from wyoming.vad import VoiceStopped

BASE = int(os.environ.get("YSE_BASE", "18765"))
SPEAKER_IP = os.environ.get("YSE_SPEAKER", "192.168.209.105")
NAME = os.environ.get("YSE_NAME", "Колонка")
AREA = os.environ.get("YSE_AREA", "") or None
SAT_PORT = int(os.environ.get("YSE_SAT_PORT", "10700"))
HTTP_PORT = BASE + 4
MUSIC_URL = "http://127.0.0.1:%d" % (BASE + 1)
ROOT = "/data/opt/yandex-station"
SSH = ["/storage/yse/bin/yse-ssh", SPEAKER_IP]
LISTEN_MAX = 15
RATE = 16000

state = {"nn": "", "handler": None, "loop": None}


def log(text):
    print(text, flush=True)


def watch_nn():
    while True:
        try:
            proc = subprocess.run(
                SSH + ["cat %s/settings/nn.txt 2>/dev/null" % ROOT],
                timeout=8, check=False, capture_output=True,
            )
            if proc.returncode == 0:
                state["nn"] = proc.stdout.decode("utf-8", "replace").strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(10)


def music(path, body):
    import urllib.request
    try:
        req = urllib.request.Request(MUSIC_URL + path, data=body.encode(), method=("POST" if body else "GET"))
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except (OSError, ValueError):
        return {}


class Speaker(AsyncEventHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.running = False
        self.mic = None
        self.mic_task = None
        self.play = None
        self.play_rate = 22050
        self.play_width = 2
        self.play_channels = 1
        self.ratecv = None
        self.ducked = False
        self.cut = False

    async def handle_event(self, event: Event) -> bool:
        if Ping.is_type(event.type):
            await self.write_event(Pong(text=Ping.from_event(event).text).event())
            return True
        if Describe.is_type(event.type):
            await self.write_event(self.info().event())
            return True
        if RunSatellite.is_type(event.type):
            self.running = True
            state["handler"] = self
            log("ha connected")
            return True
        if PauseSatellite.is_type(event.type):
            self.running = False
            log("ha paused")
            return True
        if Transcript.is_type(event.type) or VoiceStopped.is_type(event.type):
            await self.stop_mic()
            return True
        if Error.is_type(event.type):
            err = Error.from_event(event)
            log("ha error %s %s" % (err.code, (err.text or "")[:120]))
            await self.stop_mic()
            self.unduck()
            return True
        if AudioStart.is_type(event.type):
            start = AudioStart.from_event(event)
            self.open_play(start.rate, start.width, start.channels)
            return True
        if AudioChunk.is_type(event.type):
            self.feed_play(AudioChunk.from_event(event).audio)
            return True
        if AudioStop.is_type(event.type):
            await self.close_play()
            await self.write_event(Played().event())
            self.unduck()
            return True
        return True

    async def disconnect(self) -> None:
        if state["handler"] is self:
            state["handler"] = None
        await self.stop_mic()
        self.kill_play()
        log("ha gone")

    def info(self):
        return Info(satellite=Satellite(
            name=NAME,
            attribution=Attribution(name="hassio.by", url="https://hassio.by"),
            installed=True,
            description="Яндекс Станция со своей прошивкой",
            version="1.0",
            area=AREA,
        ))

    async def wake(self, heard_at):
        await self.stop_mic()
        self.kill_play()
        self.cut = False
        self.ducked = (music("/v1/now", "").get("state") == "play")
        back = int(max(0.0, time.time() - heard_at) * 1000) + 150
        back = min(back, 3000)
        await self.write_event(RunPipeline(
            start_stage=PipelineStage.ASR, end_stage=PipelineStage.TTS, restart_on_end=False,
        ).event())
        await self.write_event(AudioStart(rate=RATE, width=2, channels=1).event())
        self.mic = await asyncio.create_subprocess_exec(
            *(SSH + ["%s/bin/ringcat %d %d" % (ROOT, back, LISTEN_MAX)]),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        self.mic_task = asyncio.create_task(self.pump_mic(self.mic))
        log("assist start")

    async def pump_mic(self, proc):
        sent = 0.0
        last_duck = 0.0
        try:
            while True:
                data = await proc.stdout.read(1024)
                if not data:
                    break
                if len(data) % 2:
                    data = data[:-1]
                await self.write_event(AudioChunk(rate=RATE, width=2, channels=1, audio=data).event())
                sent += len(data) / (RATE * 2)
                if self.ducked and time.time() - last_duck > 3:
                    music("/v1/duck", "down")
                    last_duck = time.time()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if proc.returncode is None:
                proc.kill()
            try:
                await self.write_event(AudioStop().event())
            except ConnectionError:
                pass
            log("assist mic %.1fs" % sent)

    async def stop_mic(self):
        proc, task = self.mic, self.mic_task
        self.mic = None
        self.mic_task = None
        if proc is not None and proc.returncode is None:
            proc.kill()
        if task is not None:
            try:
                await asyncio.wait_for(task, 3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

    def open_play(self, rate, width, channels):
        self.kill_play()
        if self.cut:
            return
        self.play_rate, self.play_width, self.play_channels = rate, width, channels
        self.ratecv = None
        self.play = subprocess.Popen(
            SSH + ["dev=plughw:0,0; grep -q softvol /etc/asound.conf 2>/dev/null && dev=default; "
                   "aplay -D \"$dev\" -f S16_LE -r 48000 -c 2 -t raw -"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if self.ducked:
            music("/v1/duck", "down")

    def feed_play(self, audio):
        if self.play is None or self.play.poll() is not None:
            return
        if self.play_width != 2:
            audio = audioop.lin2lin(audio, self.play_width, 2)
        if self.play_channels == 2:
            audio = audioop.tomono(audio, 2, 0.5, 0.5)
        if self.play_rate != 48000:
            audio, self.ratecv = audioop.ratecv(audio, 2, 1, self.play_rate, 48000, self.ratecv)
        audio = audioop.tostereo(audio, 2, 1.0, 1.0)
        try:
            self.play.stdin.write(audio)
        except (BrokenPipeError, OSError):
            self.kill_play()

    async def close_play(self):
        proc = self.play
        self.play = None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        await asyncio.get_running_loop().run_in_executor(None, wait_quiet, proc)
        log("assist spoke")

    def kill_play(self):
        proc = self.play
        self.play = None
        if proc is not None and proc.poll() is None:
            proc.kill()
            wait_quiet(proc)

    def cut_now(self):
        was = self.mic is not None or self.play is not None
        self.cut = True
        self.kill_play()
        return was

    def unduck(self):
        if self.ducked:
            self.ducked = False
            music("/v1/duck", "up")


def wait_quiet(proc):
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()


class Http(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length).decode("utf-8", "replace") if 0 < length <= 64 else ""
        handler = state["handler"]
        loop = state["loop"]
        result = {}
        if self.path == "/v1/wake":
            taken = state["nn"] == "assist" and handler is not None and handler.running
            if taken:
                try:
                    heard_at = float(body)
                except ValueError:
                    heard_at = time.time()
                asyncio.run_coroutine_threadsafe(handler.wake(heard_at), loop)
            result = {"taken": taken}
        elif self.path == "/v1/stop":
            was = False
            if handler is not None:
                was = asyncio.run_coroutine_threadsafe(
                    asyncio.to_thread(handler.cut_now), loop).result(timeout=5)
            result = {"was": was}
        elif self.path == "/v1/state":
            result = {"nn": state["nn"], "connected": handler is not None and handler.running}
        else:
            self.send_error(404)
            return
        data = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST

    def log_message(self, fmt, *args):
        return


async def main():
    state["loop"] = asyncio.get_running_loop()
    threading.Thread(target=watch_nn, daemon=True).start()
    http = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Http)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    log("sat listening %d http %d" % (SAT_PORT, HTTP_PORT))
    server = AsyncServer.from_uri("tcp://0.0.0.0:%d" % SAT_PORT)
    await server.run(partial(Speaker))


if __name__ == "__main__":
    asyncio.run(main())
