# -*- coding: utf-8 -*-
"""SNI-шлюз для MAP/DNAT (Kerio): :443 принимает весь HTTPS, quasar — на локальный MITM, остальное — TCP relay."""

from __future__ import annotations

import os
import select
import socket
import sys
import threading

from sni_policy import host_blocked

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RELAY_TIMEOUT = max(30.0, float(os.environ.get("SNI_RELAY_TIMEOUT", "300")))
PEEK_SIZE = 16384


def quasar_sni_hosts() -> set[str]:
    raw = os.environ.get(
        "QUASAR_SNI_HOSTS",
        "quasar.yandex.net,quasar.yandex.ru,scbh.yandex.net",
    )
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def is_quasar_host(host: str | None) -> bool:
    h = (host or "").lower().rstrip(".")
    if not h:
        return False
    for q in quasar_sni_hosts():
        if h == q or h.endswith("." + q):
            return True
    return False


def parse_sni(client_hello: bytes) -> str | None:
    """Имя из TLS ClientHello (SNI). Без сторонних зависимостей."""
    if len(client_hello) < 5 or client_hello[0] != 0x16:
        return None
    rec_len = int.from_bytes(client_hello[3:5], "big")
    if 5 + rec_len > len(client_hello):
        handshake = client_hello[5 : 5 + rec_len]
    else:
        handshake = client_hello[5 : 5 + rec_len]
    if len(handshake) < 4 or handshake[0] != 0x01:
        return None
    hs_len = int.from_bytes(handshake[1:4], "big")
    body = handshake[4 : 4 + hs_len]
    if len(body) < 34:
        return None
    i = 34  # version(2)+random(32)
    if i >= len(body):
        return None
    sid_len = body[i]
    i += 1 + sid_len
    if i + 2 > len(body):
        return None
    cs_len = int.from_bytes(body[i : i + 2], "big")
    i += 2 + cs_len
    if i >= len(body):
        return None
    comp_len = body[i]
    i += 1 + comp_len
    if i + 2 > len(body):
        return None
    ext_total = int.from_bytes(body[i : i + 2], "big")
    i += 2
    ext_end = i + ext_total
    while i + 4 <= ext_end and i + 4 <= len(body):
        etype = int.from_bytes(body[i : i + 2], "big")
        elen = int.from_bytes(body[i + 2 : i + 4], "big")
        i += 4
        ext = body[i : i + elen]
        i += elen
        if etype != 0:
            continue
        if len(ext) < 2:
            continue
        j = 2
        nlist = int.from_bytes(ext[0:2], "big")
        j = 2
        seen = 0
        while seen < nlist and j + 3 <= len(ext):
            name_type = ext[j]
            name_len = int.from_bytes(ext[j + 1 : j + 3], "big")
            j += 3
            name = ext[j : j + name_len]
            j += name_len
            seen += 1
            if name_type == 0 and name:
                try:
                    return name.decode("ascii")
                except UnicodeDecodeError:
                    return name.decode("utf-8", "replace")
    return None


def _relay(a: socket.socket, b: socket.socket) -> None:
    socks = [a, b]
    while socks:
        try:
            r, _, _ = select.select(socks, [], [], RELAY_TIMEOUT)
        except (ValueError, OSError):
            break
        if not r:
            break
        for s in r:
            try:
                chunk = s.recv(65536)
            except OSError:
                chunk = b""
            if not chunk:
                return
            other = b if s is a else a
            try:
                other.sendall(chunk)
            except OSError:
                return


def _handle(client: socket.socket, peer: tuple[str, int]) -> None:
    client.settimeout(8.0)
    try:
        first = client.recv(PEEK_SIZE, socket.MSG_PEEK)
    except OSError as e:
        print(f"[SNI] {peer[0]} peek fail: {e!r}")
        client.close()
        return
    sni = parse_sni(first)
    backend = os.environ.get("EQUASAR_TLS_HOST", "127.0.0.1")
    bport = int(os.environ.get("HTTPS_PORT", "8443"))

    upstream = None
    if is_quasar_host(sni):
        try:
            upstream = socket.create_connection((backend, bport), timeout=10)
        except OSError as e:
            print(f"  !!! [SNI] {peer[0]} {sni} -> equasar {backend}:{bport} FAIL: {e!r}")
            client.close()
            return
        print(f"[SNI] {peer[0]} {sni or '?'} -> EQUASAR {backend}:{bport}")
    elif not sni:
        if os.environ.get("SNI_DEFAULT_EQUASAR", "1") == "1":
            try:
                upstream = socket.create_connection((backend, bport), timeout=10)
            except OSError as e:
                print(f"  !!! [SNI] {peer[0]} no SNI -> equasar FAIL: {e!r}")
                client.close()
                return
            print(f"[SNI] {peer[0]} no SNI -> EQUASAR (MAP/Android)")
        else:
            print(f"[SNI] {peer[0]} no SNI, close")
            client.close()
            return
    else:
        if host_blocked(sni):
            print(f"[POLICY] DROP {peer[0]} sni={sni}")
            client.close()
            return
        try:
            upstream = socket.create_connection((sni, 443), timeout=12)
        except OSError as e:
            print(f"  !!! [SNI] {peer[0]} {sni} passthrough FAIL: {e!r}")
            client.close()
            return
        print(f"[SNI] {peer[0]} {sni} -> passthrough :443")
    if upstream is None:
        client.close()
        return
    client.settimeout(None)
    upstream.settimeout(None)
    try:
        _relay(client, upstream)
    finally:
        for s in (client, upstream):
            try:
                s.close()
            except OSError:
                pass


def run_sni_gateway(port: int | None = None) -> None:
    bind = os.environ.get("BIND_IP") or "0.0.0.0"
    port = int(port if port is not None else os.environ.get("SNI_GATEWAY_PORT", "443"))
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((bind, port))
    except OSError as e:
        print(f"[!] SNI gateway не смог bind {bind}:{port}: {e!r}")
        return
    srv.listen(256)
    deny = os.environ.get("SNI_DENY_OTA", "1")
    print(
        f"[*] SNI gateway {bind}:{port} — quasar -> "
        f"{os.environ.get('EQUASAR_TLS_HOST', '127.0.0.1')}:{os.environ.get('HTTPS_PORT', '8443')}, "
        f"passthrough -> :443, SNI_DENY_OTA={deny}"
    )
    sys.stdout.flush()
    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            continue
        threading.Thread(target=_handle, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    run_sni_gateway()