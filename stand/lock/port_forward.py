# -*- coding: utf-8 -*-
"""Колонка знает один адрес — YSE_IP. Слово и фраза слушаются на компьютере.
Этот процесс принимает их на адресе сервиса и передаёт на соседний адрес,
который поднят на той же машине."""

import os
import socket
import threading
import time

RUN = os.environ.get("LOCK_RUN", "/opt/lite-kit/run")
DEST_FILE = os.path.join(RUN, "forward.ip")

# 18760 — поиск колонок для Home Assistant.
# 18800–18940 — слово, фраза и музыка, по десять портов на колонку.
# 10700–10740 — спутник Home Assistant.
RANGES = ((18760, 18761), (18800, 18940), (10700, 10740))


def _dest():
    try:
        text = open(DEST_FILE, encoding="utf-8").read().strip().split()[0]
    except (OSError, IndexError):
        return ""
    parts = text.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return text
    return ""


def _pipe(left, right):
    try:
        while True:
            data = left.recv(65536)
            if not data:
                break
            right.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (left, right):
            try:
                sock.close()
            except OSError:
                pass


def _tcp(port, dest):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("0.0.0.0", port))
        server.listen(40)
    except OSError:
        server.close()
        return
    while True:
        try:
            client, _ = server.accept()
        except OSError:
            return
        try:
            upstream = socket.create_connection((dest, port), timeout=5)
        except OSError:
            client.close()
            continue
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()


def _udp_back(upstream, downstream, client_addr):
    try:
        while True:
            data, _ = upstream.recvfrom(65535)
            downstream.sendto(data, client_addr)
    except OSError:
        pass


def _udp(port, dest):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError:
        sock.close()
        return
    flows = {}
    lock = threading.Lock()
    while True:
        try:
            data, addr = sock.recvfrom(65535)
        except OSError:
            return
        with lock:
            upstream = flows.get(addr)
        if upstream is None:
            upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                upstream.bind(("0.0.0.0", 0))
            except OSError:
                upstream.close()
                continue
            with lock:
                flows[addr] = upstream
            threading.Thread(
                target=_udp_back, args=(upstream, sock, addr), daemon=True
            ).start()
        try:
            upstream.sendto(data, (dest, port))
        except OSError:
            pass


def main():
    dest = ""
    while not dest:
        dest = _dest()
        if not dest:
            time.sleep(2)
    print("port forward -> %s" % dest, flush=True)
    for start, end in RANGES:
        for port in range(start, end):
            threading.Thread(target=_tcp, args=(port, dest), daemon=True).start()
            threading.Thread(target=_udp, args=(port, dest), daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
