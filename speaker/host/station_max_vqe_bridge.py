# -*- coding: utf-8 -*-
"""Ухо Джарвис для Station Max: поток vqe из quasar LogStreamer/WebSocket → wake UDP + TCP фраза.

На колонке без root третье приложение не открывает PDM/VQE — штатный audiosender в io.sdk
держит канал «vqe» (см. station_max_audiosender_re.md). Мост принимает WS на YSE_VQE_WS
(по умолчанию :18820), пуляет пакеты как housed (UDP BASE, 320×s16 + seq), при wake на
хосте — YSEGO на панель и TCP BASE+5 с PCM.

Вход: websocket (бинарные кадры), сырой TCP или файл s16le mono 16kHz (YSE_VQE_FILE).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import sys
import threading
import time
import numpy as np

SPEAKER = os.environ.get("YSE_SPEAKER", "192.168.209.114")
HOST = os.environ.get("YSE_HOST", "127.0.0.1")
BASE = int(os.environ.get("YSE_BASE", "18805"))
WS_HOST = os.environ.get("YSE_VQE_WS_BIND", "0.0.0.0")
WS_PORT = int(os.environ.get("YSE_VQE_WS_PORT", "18820"))
VQE_CHANNEL = os.environ.get("YSE_VQE_CHANNEL", "vqe")
VQE_INDEX = int(os.environ.get("YSE_VQE_INDEX", "9"))  # index in audiosender.availableChannels
CHANNELS = int(os.environ.get("YSE_VQE_NCH", "14"))
PREV = 0.0
SEQ = 0


def log(msg: str) -> None:
    print(msg, flush=True)


def push_speaker(text: str) -> None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(text.encode("ascii"), (SPEAKER, 18081))
        sock.close()
    except OSError as exc:
        log("YSEGO fail %s" % exc)


def phrase_stream(pcm_ring: bytearray, gen_at: float) -> None:
    """После wake открыть TCP на yse_talk и слить кольцо + live tail."""
    port = BASE + 5
    end = time.time() + 28.0
    try:
        sock = socket.create_connection((HOST, port), timeout=4.0)
    except OSError as exc:
        log("phrase tcp %s" % exc)
        return
    try:
        sock.sendall(b"YSE1 lag=0 chime=0\n")
        sock.settimeout(0.25)
        with _PHRASE_LOCK:
            _PHRASE_SOCK[0] = sock
        while time.time() < end:
            chunk = None
            with _PHRASE_LOCK:
                if pcm_ring:
                    chunk = bytes(pcm_ring[:6400])
                    del pcm_ring[: len(chunk)]
            if chunk:
                sock.sendall(chunk)
            else:
                time.sleep(0.02)
            try:
                data = sock.recv(64)
                if data and b"stop" in data:
                    break
            except OSError:
                pass
    except OSError as exc:
        log("phrase send %s" % exc)
    finally:
        with _PHRASE_LOCK:
            if _PHRASE_SOCK[0] is sock:
                _PHRASE_SOCK[0] = None
        sock.close()


_PHRASE_LOCK = threading.Lock()
_PHRASE_SOCK: list = [None]
_PCM_RING = bytearray()
_WAKE_GEN = 0.0


def emit_wake() -> None:
    """Wake/phrase ведёт yse-wake@ (UDP 18805); здесь только UI на панели и опционально TCP."""
    global _WAKE_GEN
    _WAKE_GEN = time.time()
    push_speaker("YSEGO")
    if os.environ.get("YSE_VQE_HOST_PHRASE", "1") == "1":
        threading.Thread(target=phrase_stream, args=(_PCM_RING, _WAKE_GEN), daemon=True).start()


def mono_from_multichannel(raw: bytes, nch: int, pick: int) -> np.ndarray:
    samples = np.frombuffer(raw, dtype="<i2")
    if samples.size % nch != 0:
        samples = samples[: samples.size // nch * nch]
    frames = samples.reshape(-1, nch)
    if pick < nch:
        return frames[:, pick]
    return frames[:, 0]


def push_frame(mono: np.ndarray) -> np.ndarray:
    global PREV, SEQ
    if mono.size < 320:
        return mono
    mono = mono[:320]
    wake = np.empty(320, dtype=np.int16)
    prev = PREV
    for i in range(320):
        x = float(mono[i])
        y = x - 0.97 * prev
        prev = x
        wake[i] = int(np.clip(y, -32768, 32767))
    PREV = prev
    pkt = struct.pack("<I", SEQ) + wake.astype("<i2").tobytes()
    SEQ = (SEQ + 1) & 0xFFFFFFFF
    pcm = mono.astype("<i2").tobytes()
    _PCM_RING.extend(pcm)
    if len(_PCM_RING) > 32000 * 12:
        del _PCM_RING[: len(_PCM_RING) - 32000 * 12]
    with _PHRASE_LOCK:
        ps = _PHRASE_SOCK[0]
    if ps is not None:
        try:
            ps.sendall(pcm)
        except OSError:
            pass
    # wake model consumes preemphasis in UDP packet tail (same as Web.pushSamples)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(pkt, (HOST, BASE))
        s.close()
    except OSError:
        pass
    return wake


def serve_file(path: str) -> None:
    data = open(path, "rb").read()
    step = 640
    for off in range(0, len(data) - step, step):
        mono = np.frombuffer(data[off : off + step], dtype="<i2")
        push_frame(mono)
        time.sleep(0.018)


async def ws_handler(websocket):  # noqa: ANN001
    log("ws client")
    nch = CHANNELS
    async for message in websocket:
        if isinstance(message, str):
            try:
                obj = json.loads(message)
                ch = obj.get("channel") or obj.get("channel_name")
                b64 = obj.get("data") or obj.get("pcm")
                if b64 and ch in (VQE_CHANNEL, "vqe_0", "vqe"):
                    import base64

                    raw = base64.b64decode(b64)
                    mono = np.frombuffer(raw, dtype="<i2")
                    if mono.size >= 320:
                        push_frame(mono[:320])
                continue
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            continue
        # binary: try interleaved multichannel
        if len(message) >= nch * 2 * 80:
            mono = mono_from_multichannel(message, nch, VQE_INDEX)
            for i in range(0, mono.size - 320, 320):
                chunk = mono[i : i + 320]
                push_frame(chunk)
        elif len(message) >= 640:
            mono = np.frombuffer(message[:640], dtype="<i2")
            push_frame(mono)


async def ws_main() -> None:
    try:
        import websockets
    except ImportError:
        log("pip install websockets on frigate")
        return
    async with websockets.serve(ws_handler, WS_HOST, WS_PORT, max_size=2**20, ping_interval=20):
        log("vqe ws %s:%d nch=%d idx=%d -> udp %s:%d" % (WS_HOST, WS_PORT, CHANNELS, VQE_INDEX, HOST, BASE))
        await asyncio.Future()


def main() -> int:
    path = os.environ.get("YSE_VQE_FILE", "")
    if path:
        serve_file(path)
        return 0
    threading.Thread(target=lambda: asyncio.run(ws_main()), daemon=True).start()
    # keepalive
    while True:
        time.sleep(60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())