# -*- coding: utf-8 -*-
"""Шлёт сырой PCM на колонку, у которой нет aplay. stdin — s16le, аргументы: url rate channels role."""
import socket
import sys
from urllib.parse import urlparse


def main():
    url, rate, channels, role = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or 80
    path = parsed.path or "/"
    sock = socket.create_connection((host, port), 5)
    head = (
        "POST %s HTTP/1.0\r\n"
        "Host: %s\r\n"
        "Content-Type: application/octet-stream\r\n"
        "X-Rate: %s\r\n"
        "X-Channels: %s\r\n"
        "X-Role: %s\r\n"
        "Connection: close\r\n"
        "\r\n" % (path, host, rate, channels, role)
    )
    sock.sendall(head.encode("ascii"))
    while True:
        chunk = sys.stdin.buffer.read(4800 * 4)
        if not chunk:
            break
        sock.sendall(chunk)
    try:
        sock.shutdown(socket.SHUT_WR)
        sock.recv(256)
    except OSError:
        pass
    sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
