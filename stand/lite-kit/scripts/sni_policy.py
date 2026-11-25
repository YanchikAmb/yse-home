# -*- coding: utf-8 -*-
"""Политика хостов для MAP на стенде: deny passthrough (OTA обход quasar)."""

from __future__ import annotations

import os


def _split_hosts(key: str, default: str = "") -> set[str]:
    raw = os.environ.get(key, default)
    return {h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip()}


def _suffixes(key: str, default: str = "") -> list[str]:
    raw = os.environ.get(key, default)
    out: list[str] = []
    for s in raw.split(","):
        s = s.strip().lower().rstrip(".")
        if not s:
            continue
        if not s.startswith("."):
            s = "." + s
        out.append(s)
    return out


def sni_deny_enabled() -> bool:
    return os.environ.get("SNI_DENY_OTA", "1") == "1"


def denied_hosts() -> set[str]:
    default = "androidtvprod.s3.yandex.net,quasar.s3.yandex.net,ota.yandex.net"
    return _split_hosts("SNI_DENY_HOSTS", default)


def denied_suffixes() -> list[str]:
    # суффиксы для прошивочных CDN (не все cdn.yandex.net — там и Алиса)
    default = ".ota.yandex.net,.svc.cdn.yandex.net"
    return _suffixes("SNI_DENY_SUFFIXES", default)


def host_blocked(host: str | None) -> bool:
    """True — passthrough/http-thru запрещён."""
    if not sni_deny_enabled():
        return False
    h = (host or "").lower().rstrip(".")
    if not h:
        return False
    if h in denied_hosts():
        return True
    for suf in denied_suffixes():
        if h.endswith(suf) or h == suf.lstrip("."):
            return True
    return False