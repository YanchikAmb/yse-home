# -*- coding: utf-8 -*-
"""ADB tinycap с Макса → UDP wake + TCP фраза (без reader4 на устройстве)."""
from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import threading
import time

SPEAKER = os.environ.get("YSE_SPEAKER", "192.168.209.114")
BASE = int(os.environ.get("YSE_PORT_WAKE", "18805"))
RATE = 48000
CH = 16
FRAME = 960
BYTES = FRAME * CH * 2
CHUNK = 320
PREV = 0.0


def log(msg: str) -> None:
    print(msg, flush=True)


def adb(*args: str, timeout=12):
    return subprocess.run(
        ["adb", "-s", f"{SPEAKER}:5555", *args],
        capture_output=True,
        timeout=timeout,
    )


def prep_device():
    adb("connect", SPEAKER)
    adb("shell", "am force-stop com.yse.station")
    adb("shell", "am force-stop com.yandex.io.sdk")
    adb("shell", "tinymix2 -D 0 set 94 PDMIN")


def mono_from_block(block: bytes) -> int:
    """7 mics [0..6] минус ref [7,8], downsample 48k→16k (каждый 3-й кадр)."""
    global PREV
    acc = 0
    acc_n = 0
    spk = 0
    for i in range(0, len(block), CH * 2):
        if i + CH * 2 > len(block):
            break
        samples = struct.unpack_from("<" + "h" * CH, block, i)
        if (i // (CH * 2)) % 3 != 0:
            continue
        mic = sum(samples[0:7]) // 7
        ref = (samples[7] + samples[8]) // 2
        x = mic - ref // 2
        acc += x * x
        acc_n += 1
        y = x - 0.97 * PREV
        PREV = float(x)
        yield int(y)


def send_wake(sock: socket.socket, host: str, seq: int, samples: list[int]):
    if len(samples) < CHUNK:
        return
    body = struct.pack("<i", seq)
    for s in samples[:CHUNK]:
        body += struct.pack("<h", max(-32768, min(32767, s)))
    if len(body) < 4 + CHUNK * 2:
        return
    rms = 0
    for s in samples[:CHUNK]:
        rms += s * s
    rms = int((rms / CHUNK) ** 0.5)
    if rms < 60:
        return
    sock.sendto(body, (host, BASE))


def phrase_loop(host: str, phrase_sock: list):
    """Ждёт YSEGO на :18081 от wake — упрощённо TCP 18810 только по сигналу UDP."""
    del host, phrase_sock
    time.sleep(10**6)


def main() -> int:
    host = os.environ.get("YSE_HOST", "192.168.209.50")
    prep_device()
    path = "/sdcard/yse_host_ear.wav"
    subprocess.run(
        ["adb", "-s", f"{SPEAKER}:5555", "shell", f"rm -f {path}"],
        check=False,
        timeout=10,
    )
    cmd = (
        f"tinymix2 -D 0 set 94 PDMIN; "
        f"tinycap2 {path} -D 0 -d 1 -r {RATE} -c {CH} -p {FRAME} -b 16 -t 30"
    )
    subprocess.Popen(
        ["adb", "-s", f"{SPEAKER}:5555", "shell", cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    pull = subprocess.Popen(
        ["adb", "-s", f"{SPEAKER}:5555", "exec-out", f"cat {path}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq = 0
    buf = b""
    fifo: list[int] = []
    while True:
        chunk = pull.stdout.read(BYTES)  # type: ignore
        if not chunk:
            if pull.poll() is not None:
                log("pull end rc=%s" % pull.returncode)
                break
            time.sleep(0.05)
            continue
        buf += chunk
        while len(buf) >= BYTES:
            block = buf[:BYTES]
            buf = buf[BYTES:]
            for sample in mono_from_block(block):
                fifo.append(sample)
                if len(fifo) >= CHUNK:
                    send_wake(udp, host, seq, fifo)
                    seq += 1
                    fifo = fifo[CHUNK:]
        if seq and seq % 500 == 0:
            log("packets %d" % seq)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())