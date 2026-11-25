# -*- coding: utf-8 -*-
"""Текущий адрес колонки.

Колонка сама шлёт микрофон на порт комнаты. Кабель и Wi-Fi дают разные адреса,
порт не меняется, поэтому новый адрес виден из этих пакетов.
Файл: /storage/yse/run/<base>.peer. YSE_SPEAKER остаётся только первой догадкой.
"""
from __future__ import annotations

import os
from pathlib import Path

_SEEN: dict[str, str] = {}


def _private(ip: str) -> bool:
    parts = (ip or "").split(".")
    if len(parts) != 4:
        return False
    try:
        nums = [int(part) for part in parts]
    except ValueError:
        return False
    if any(num < 0 or num > 255 for num in nums):
        return False
    first, second = nums[0], nums[1]
    if first == 10:
        return True
    if first == 192 and second == 168:
        return True
    return first == 172 and 16 <= second <= 31


def path_for(base: str) -> Path:
    root = os.environ.get("YSE_RUN", "/storage/yse/run")
    return Path(root) / ("%s.peer" % (base or "18765"))


def _base() -> str:
    return (os.environ.get("YSE_BASE", "18765") or "18765").strip()


def note(ip: str, base: str = "") -> str:
    """Запоминает адрес, если это частный IPv4. Повтор того же адреса ничего не пишет."""
    ip = (ip or "").strip()
    if not _private(ip):
        return ""
    base = (base or _base()).strip() or "18765"
    if _SEEN.get(base) == ip:
        return ip
    path = path_for(base)
    try:
        old = path.read_text(encoding="utf-8").strip()
    except OSError:
        old = ""
    _SEEN[base] = ip
    if old == ip:
        return ip
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(ip + "\n", encoding="utf-8")
    tmp.replace(path)
    if old:
        print("peer %s -> %s" % (old, ip), flush=True)
    else:
        print("peer %s" % ip, flush=True)
    return ip


def current_for(base: str, fallback: str = "") -> str:
    fallback = (fallback or "").strip()
    base = (base or "").strip()
    if not base:
        return fallback
    try:
        ip = path_for(base).read_text(encoding="utf-8").strip()
    except OSError:
        ip = ""
    if _private(ip):
        return ip
    return fallback


def current() -> str:
    return current_for(_base(), os.environ.get("YSE_SPEAKER", ""))
