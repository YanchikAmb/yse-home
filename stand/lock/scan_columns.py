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
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/opt/yse-lock")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import columns

EVERY = int(os.environ.get("COLUMN_SCAN_SEC", str(30 * 60)))
PORT = 18080
_BUSY = threading.Lock()


def _file():
    return columns.RUN / "scan-net.txt"


def suggested_net() -> str:
    """Сеть адреса самого сервиса. Это предложение для поля, не поиск."""
    local = (os.environ.get("LOCAL_IP") or os.environ.get("BIND_IP") or "").strip()
    if not local:
        return ""
    try:
        return str(ipaddress.ip_network(local + "/24", strict=False))
    except ValueError:
        return ""


def saved_net() -> str:
    try:
        return _file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def parse_net(text: str):
    raw = (text or "").strip()
    if not raw:
        raise ValueError("напишите подсеть, например 192.168.1.0/24")
    if "/" not in raw:
        raw += "/24"
    try:
        net = ipaddress.ip_network(raw, strict=False)
    except ValueError:
        raise ValueError("это не подсеть")
    if net.version != 4 or net.prefixlen < 22:
        raise ValueError("нужна сеть не шире /22")
    return net


def save_net(text: str):
    net = parse_net(text)
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(net) + "\n", encoding="utf-8")
    return net


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


def scan_once(net=None) -> int:
    if net is None:
        text = saved_net()
        if not text:
            return 0
        net = parse_net(text)
    if not _BUSY.acquire(blocking=False):
        raise RuntimeError("поиск уже идёт")
    try:
        skip = columns.skip_ips()
        hosts = [str(addr) for addr in net.hosts() if str(addr) not in skip]
        with ThreadPoolExecutor(max_workers=64) as pool:
            opened = [ip for ip in pool.map(_port_open, hosts) if ip]
        found = [ip for ip in opened if _ours(ip)]
        for ip in found:
            _ask(ip)
        print("scan %s found %s" % (net, len(found)), flush=True)
        return len(found)
    finally:
        _BUSY.release()


def main() -> int:
    print("scan columns :%s every %ss, только сохранённая подсеть" % (PORT, EVERY), flush=True)
    while True:
        time.sleep(EVERY)
        if not saved_net():
            continue
        try:
            scan_once()
        except Exception as exc:
            print("scan fail %s" % type(exc).__name__, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
