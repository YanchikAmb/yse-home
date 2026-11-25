# -*- coding: utf-8 -*-
"""Колонки, которые сами пришли на DNS сервиса. Файл run/columns.json."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

RUN = Path(os.environ.get("LOCK_RUN", os.environ.get("STAND_RUN", "/opt/lite-kit/run")))
PATH = RUN / "columns.json"
_LOCK = threading.Lock()

# Имена с https://hackalice.me/ — как колонка называет себя в запросе.
TITLES = {
    "yandexstation_2": "Макс",
    "yandexstation": "Станция",
    "yandexmidi": "Станция 2",
    "yandexmini_2": "Мини 2",
    "yandexmini": "Мини",
    "yandexmicro": "Лайт",
    "cucumber": "Миди",
    "plum": "Лайт 2",
    "bergamot": "Мини 3",
    "pickle": "Мини 3 Про",
    "orion": "Станция 3",
    "mango": "Стрит",
    "saturn": "Умный хаб",
    "fiero": "Fiero",
}
# Android: оболочка ставится обычным приложением, root не нужен.
ANDROID = {"yandexstation_2", "yandexstation"}
# Станция 2 и Мини 2: прошивка, SSH, запрет обновлений.
BUILDROOT = {"yandexmidi", "yandexmini_2", "yandexmini", "yandexmicro", "cucumber"}


def _load() -> list:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save(rows: list) -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(PATH)


def _own_ips() -> set:
    ips = {"127.0.0.1"}
    for key in ("LOCAL_IP", "BIND_IP", "COLUMN_IGNORE"):
        for value in (os.environ.get(key) or "").split(","):
            value = value.strip()
            if value:
                ips.add(value)
    return ips


def read_columns() -> list:
    with _LOCK:
        return _load()


def get(ip: str) -> dict | None:
    for row in read_columns():
        if row.get("ip") == ip:
            return row
    return None


def _gateway_ips() -> set:
    ips = set()
    for key in ("COLUMN_GATEWAY", "YSE_GW"):
        value = (os.environ.get(key) or "").strip()
        if value:
            ips.add(value)
    return ips


def skip_ips() -> set:
    return _own_ips() | _gateway_ips()


def _column_platform(platform: str) -> str:
    if platform in TITLES or platform.startswith("yandex"):
        return platform
    return ""


def seen(ip: str, platform: str = "", dns_name: str = "", device: str = "") -> None:
    """Свой адрес — из DNS. Модель — из запроса колонки. Адрес роутера колонкой не считается."""
    ip = (ip or "").strip()
    platform = _column_platform((platform or "").strip())
    dns_name = (dns_name or "").strip()
    device = (device or "").strip()
    hidden = (not ip) or ip in _own_ips() or ip in _gateway_ips() or ip.startswith("127.")
    real = "" if hidden else ip
    if not real and not platform:
        return
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        rows = _load()
        row = None
        if device:
            row = next((item for item in rows if item.get("device") == device), None)
        if row is None and real:
            row = next((item for item in rows if item.get("ip") == real), None)
        if row is None and hidden and platform:
            row = next((item for item in rows if item.get("platform") == platform and not item.get("ip")), None)
        if row is None:
            row = {"ip": real, "platform": "", "title": "колонка", "seen": now, "job": "idle", "detail": ""}
            rows.append(row)
        elif row.get("seen") == now and (not platform or row.get("platform") == platform) and (
            not dns_name or row.get("dns") == dns_name
        ) and (not real or row.get("ip") == real):
            return
        row["seen"] = now
        if real:
            row["ip"] = real
        if device:
            row["device"] = device
        if dns_name:
            row["dns"] = dns_name
            if not row.get("detail") or str(row.get("detail", "")).startswith("DNS "):
                row["detail"] = "DNS %s" % dns_name
        if platform and platform != row.get("platform"):
            row["platform"] = platform
            row["title"] = TITLES.get(platform, platform)
            if not row.get("detail") or str(row.get("detail", "")).startswith("DNS "):
                row["detail"] = "модель в запросе"
        if hidden and not row.get("ip") and row.get("detail") in ("", "модель в запросе"):
            row["detail"] = "запрос пришёл с роутера, адрес колонки не виден"
        _save(rows)


def apply_probe(ip: str, platform: str, page_key: str, shell: bool) -> dict:
    """Проверка колонки. «Прошита» здесь не ставится: это только после подтверждения."""
    with _LOCK:
        rows = _load()
        row = next((item for item in rows if item.get("ip") == ip), None)
        if row is None:
            raise ValueError("колонка ещё не появлялась")
        if platform:
            row["platform"] = platform
            row["title"] = TITLES.get(platform, platform)
        row["shell"] = bool(shell)
        if page_key:
            row["page_key"] = page_key
        row["detail"] = "на колонке наша оболочка" if shell else "нашей оболочки нет"
        _save(rows)
        return dict(row)


def confirm(ip: str) -> dict:
    with _LOCK:
        rows = _load()
        row = next((item for item in rows if item.get("ip") == ip), None)
        if row is None:
            raise ValueError("колонка ещё не появлялась")
        if not row.get("shell"):
            raise ValueError("сначала проверка должна увидеть нашу оболочку")
        row["flashed"] = True
        row["detail"] = "прошита"
        _save(rows)
        return dict(row)


def for_page(rows: list) -> list:
    out = []
    for row in rows:
        item = dict(row)
        if not item.get("flashed"):
            item.pop("page_key", None)
        item.pop("device", None)
        out.append(item)
    return out


def set_job(ip: str, job: str, detail: str = "") -> None:
    with _LOCK:
        rows = _load()
        row = next((item for item in rows if item.get("ip") == ip), None)
        if row is None:
            return
        row["job"] = job
        row["detail"] = detail
        _save(rows)


def arm(ip: str) -> dict:
    with _LOCK:
        rows = _load()
        row = next((item for item in rows if item.get("ip") == ip), None)
        if row is None:
            raise ValueError("колонка ещё не появлялась")
        if not row.get("platform"):
            raise ValueError("колонка ещё не назвала модель")
        platform = row["platform"]
        if platform not in ANDROID and platform not in BUILDROOT:
            raise ValueError("для %s сценарий ещё не готов" % row.get("title", platform))
        row["job"] = "armed"
        row["detail"] = "запускаю"
        _save(rows)
        return dict(row)


def armed(ip: str) -> bool:
    row = get(ip)
    return bool(row) and row.get("job") in ("armed", "work")
