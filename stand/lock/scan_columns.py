# -*- coding: utf-8 -*-
"""Ищет уже прошитые колонки в сети сервиса. Списка адресов нет.

Оболочка слушает :18080 и без ключа отвечает на /api/clock.
Сервис опрашивает адреса своей подсети и записывает тех, кто ответил.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/opt/yse-lock")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import columns

EVERY = int(os.environ.get("COLUMN_SCAN_SEC", "60"))
PORT = 18080


def _net():
    local = (os.environ.get("LOCAL_IP") or os.environ.get("BIND_IP") or "").strip()
    if not local:
        return None
    try:
        return ipaddress.ip_network(local + "/24", strict=False)
    except ValueError:
        return None


def _port_open(ip: str) -> str:
    try:
        with socket.create_connection((ip, PORT), 0.4):
            return ip
    except OSError:
        return ""


def _ours(ip: str) -> bool:
    try:
        with urllib.request.urlopen("http://%s:%s/api/clock" % (ip, PORT), timeout=2) as resp:
            body = resp.read(200).decode("utf-8", "replace")
    except Exception:
        return False
    return '"text"' in body


def _ask(ip: str) -> None:
    row = columns.get(ip)
    if row and row.get("job") in ("armed", "work"):
        return
    columns.seen(ip)
    try:
        import lock_speaker
        lock_speaker.probe(ip)
        print("scan shell %s model" % ip, flush=True)
    except Exception:
        current = columns.get(ip) or {}
        if current.get("job") in (None, "", "idle"):
            columns.set_job(ip, "idle", "наша оболочка на порту 18080")
        print("scan shell %s" % ip, flush=True)


def scan_once() -> int:
    net = _net()
    if net is None:
        return 0
    skip = columns.skip_ips()
    hosts = [str(addr) for addr in net.hosts() if str(addr) not in skip]
    with ThreadPoolExecutor(max_workers=64) as pool:
        opened = [ip for ip in pool.map(_port_open, hosts) if ip]
    found = [ip for ip in opened if _ours(ip)]
    for ip in found:
        _ask(ip)
    return len(found)


def main() -> int:
    print("scan columns :%s every %ss" % (PORT, EVERY), flush=True)
    while True:
        try:
            n = scan_once()
            print("scan found %s" % n, flush=True)
        except Exception as exc:
            print("scan fail %s" % type(exc).__name__, flush=True)
        time.sleep(EVERY)


if __name__ == "__main__":
    raise SystemExit(main())
