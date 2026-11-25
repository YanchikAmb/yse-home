# -*- coding: utf-8 -*-
"""Слушает logcat Макса; при штатном spotter — YSEGO на панель."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time

SPEAKER = os.environ.get("YSE_SPEAKER", "192.168.209.114")
UDP = (SPEAKER, 18081)
PAT = re.compile(r"(spotterConfidence=\s*[\d.]+|SpotterPhrase|wakeword|Activation blocked)", re.I)
REFRACTORY = float(os.environ.get("YSE_WAKE_REFRACTORY", "2.5"))


def log(msg: str) -> None:
    print(msg, flush=True)


def push_wake() -> None:
    import urllib.request

    try:
        req = urllib.request.Request(
            "http://%s:18080/api/wake" % SPEAKER, data=b"", method="POST"
        )
        urllib.request.urlopen(req, timeout=1.5).read()
    except OSError as exc:
        log("wake http %s" % exc)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(b"YSEGO", UDP)
        s.close()
    except OSError as exc:
        log("push %s" % exc)


def main() -> int:
    adb = ["adb", "-s", f"{SPEAKER}:5555"]
    subprocess.run(adb + ["connect", f"{SPEAKER}:5555"], check=False, timeout=10)
    log("log wake %s" % SPEAKER)
    proc = subprocess.Popen(
        adb + ["logcat", "-v", "brief", "quark-native:I", "YandexIO:I", "*:S"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    last = 0.0
    assert proc.stdout is not None
    for line in proc.stdout:
        if not PAT.search(line):
            continue
        now = time.time()
        if now - last < REFRACTORY:
            continue
        last = now
        log("hit %s" % line.strip()[:120])
        push_wake()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())