# -*- coding: utf-8 -*-
"""Скачать официальный OTA с облака на стенд (ota-cache/), колонке не отдаётся — только из firmware/."""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request

# DoH + fallback — как в equasar_server.real_ip
UPSTREAM = "quasar.yandex.net"


def _quasar_ip() -> str:
    import socket

    try:
        u = "https://dns.google/resolve?name=%s&type=A" % UPSTREAM
        d = json.loads(urllib.request.urlopen(u, timeout=8).read())
        for a in d.get("Answer", []):
            if a.get("type") == 1:
                return a["data"]
    except Exception:
        pass
    return socket.gethostbyname(UPSTREAM)


def check_updates(platform: str, version: str, device_id: str = "yse-pull-probe") -> dict:
    qs = urllib.parse.urlencode(
        {
            "device_id": device_id,
            "version": version,
            "platform": platform,
            "revision": "rev2",
        }
    )
    # Официальный путь — /check_updates на quasar.yandex.net (не /{platform}/ota/...).
    url = f"https://{UPSTREAM}/check_updates?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": f"{platform}/{version}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def download_url(url: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "yse-stand-ota-pull/1"})
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as out:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)


def main() -> None:
    ap = argparse.ArgumentParser(description="OTA с облака в ota-cache (не в раздачу колонке)")
    ap.add_argument("-p", "--platform", default="yandexstation_2")
    ap.add_argument("-v", "--version", required=True, help="текущий incremental колонки")
    ap.add_argument(
        "-o",
        "--out-dir",
        default=None,
        help="каталог (по умолчанию lite-kit/ota-cache/<platform>/)",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = args.out_dir or os.path.join(base, "ota-cache", args.platform)
    manifest = check_updates(args.platform, args.version)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if not manifest.get("hasUpdate"):
        print("[OTA-PULL] облако: обновлений нет")
        return
    url = manifest.get("downloadUrl") or manifest.get("url")
    ver = manifest.get("version") or "unknown"
    if not url:
        print("[OTA-PULL] нет downloadUrl в ответе")
        return
    name = os.path.basename(urllib.parse.urlparse(url).path) or f"{ver}.zip"
    dest = os.path.join(out, name)
    if args.dry_run:
        print(f"[OTA-PULL] dry-run: скачал бы {url} -> {dest}")
        return
    print(f"[OTA-PULL] качаю {url} -> {dest}")
    download_url(url, dest)
    print(f"[OTA-PULL] готово {dest} ({os.path.getsize(dest)} b)")


if __name__ == "__main__":
    main()