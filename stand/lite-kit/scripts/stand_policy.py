# -*- coding: utf-8 -*-
"""Политика quasar по платформам: run/quasar-policy.json."""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from typing import Any

BASE_DIR = os.environ.get("LITE_KIT_BASE", "/opt/lite-kit")
POLICY_PATH = os.path.join(BASE_DIR, "run", "quasar-policy.json")

_LOCK = threading.Lock()
_CACHE: dict[str, Any] | None = None
_MTIME: float = 0.0

MODELS: dict[str, dict[str, Any]] = {
    "yandexstation_2": {
        "title": "Макс",
        "log_tokens": ["yandexstation_2", "SYNC-PLAIN", "REGISTER"],
        "check_version": "1.56.1.32.929922494.20210326.22",
        "adbd_enabled": True,
        "adbd_network": None,
        "tv_adb_enabled": True,
        "register_honest": True,
        "bind_plain_sync": True,
        "sync_local_only": False,
    },
    "yandexmini_2": {
        "title": "Мини 2",
        "log_tokens": ["yandexmini_2", "yandexmini"],
        "check_version": "0.0.0",
        "adbd_enabled": True,
        "adbd_network": None,
        "tv_adb_enabled": True,
        "register_honest": False,
        "bind_plain_sync": False,
        "sync_local_only": False,
    },
    "yandexmidi": {
        "title": "Станция 2",
        "log_tokens": ["yandexmidi", "yandexmicro"],
        "check_version": "0.0.0",
        "adbd_enabled": True,
        "adbd_network": None,
        "tv_adb_enabled": True,
        "register_honest": False,
        "bind_plain_sync": False,
        "sync_local_only": False,
    },
}


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v == "1" or v.lower() in ("true", "yes", "on")


def _default_root() -> dict[str, Any]:
    plats = deepcopy(MODELS)
    if _env_bool("REGISTER_HONEST", True):
        plats["yandexstation_2"]["register_honest"] = True
    if _env_bool("BIND_PLAIN_SYNC", True):
        plats["yandexstation_2"]["bind_plain_sync"] = True
    return {"ui_active": "yandexstation_2", "platforms": plats}


def _migrate_flat(raw: dict[str, Any]) -> dict[str, Any]:
    """Старый плоский quasar-policy.json → platforms."""
    root = _default_root()
    plats = root["platforms"]
    probe = raw.get("ota_probe_platform") or "yandexstation_2"
    if probe not in plats:
        probe = "yandexstation_2"
    p = plats[probe]
    for key in (
        "adbd_enabled",
        "adbd_network",
        "tv_adb_enabled",
        "sync_local_only",
    ):
        if key in raw:
            p[key] = raw[key]
    if "register_honest_platforms" in raw:
        p["register_honest"] = probe in (raw.get("register_honest_platforms") or [])
    if "bind_plain_sync_platforms" in raw:
        p["bind_plain_sync"] = probe in (raw.get("bind_plain_sync_platforms") or [])
    if raw.get("platforms"):
        for pid, cfg in raw["platforms"].items():
            if pid in plats and isinstance(cfg, dict):
                plats[pid].update(cfg)
    root["ui_active"] = raw.get("ui_active") or probe
    return root


def load_policy(force: bool = False) -> dict[str, Any]:
    global _CACHE, _MTIME
    try:
        mtime = os.stat(POLICY_PATH).st_mtime
    except OSError:
        mtime = 0.0
    with _LOCK:
        if not force and _CACHE is not None and mtime == _MTIME:
            return deepcopy(_CACHE)
        root = _default_root()
        if mtime:
            try:
                raw = json.loads(open(POLICY_PATH, encoding="utf-8").read())
                if isinstance(raw, dict):
                    if "platforms" in raw:
                        root = _default_root()
                        for pid, cfg in raw.get("platforms", {}).items():
                            if pid in root["platforms"] and isinstance(cfg, dict):
                                root["platforms"][pid].update(cfg)
                        root["ui_active"] = raw.get("ui_active") or root["ui_active"]
                    else:
                        root = _migrate_flat(raw)
            except (OSError, json.JSONDecodeError):
                pass
        _CACHE = root
        _MTIME = mtime
        return deepcopy(root)


def save_policy(root: dict[str, Any]) -> dict[str, Any]:
    os.makedirs(os.path.dirname(POLICY_PATH), exist_ok=True)
    out = deepcopy(load_policy(force=True))
    if root.get("platforms"):
        for pid, cfg in root["platforms"].items():
            if pid in out["platforms"] and isinstance(cfg, dict):
                out["platforms"][pid].update(cfg)
    if root.get("ui_active"):
        out["ui_active"] = root["ui_active"]
    with _LOCK:
        with open(POLICY_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
            f.write("\n")
        global _CACHE, _MTIME
        _CACHE = deepcopy(out)
        try:
            _MTIME = os.stat(POLICY_PATH).st_mtime
        except OSError:
            _MTIME = 0.0
    return deepcopy(out)


def patch_platform(platform: str, patch: dict[str, Any]) -> dict[str, Any]:
    root = load_policy(force=True)
    if platform not in root["platforms"]:
        raise ValueError("unknown platform: %s" % platform)
    root["platforms"][platform].update(patch)
    return save_policy(root)


def _plat(platform: str | None) -> str:
    return (platform or "").lower()


def platform_cfg(platform: str | None) -> dict[str, Any]:
    plat = _plat(platform)
    root = load_policy()
    if plat in root["platforms"]:
        return deepcopy(root["platforms"][plat])
    return deepcopy(MODELS.get("yandexstation_2", {}))


def adbd_enabled(platform: str | None = None) -> bool:
    return bool(platform_cfg(platform).get("adbd_enabled", True))


def tv_adb_enabled(platform: str | None = None) -> bool:
    return bool(platform_cfg(platform).get("tv_adb_enabled", True))


def adbd_network_for(platform: str | None) -> bool:
    cfg = platform_cfg(platform)
    if not cfg.get("adbd_enabled", True):
        return False
    net = cfg.get("adbd_network")
    if net is not None:
        return bool(net)
    no_net = {"yandexstation_2", "yandexstation"}
    return _plat(platform) not in no_net


def bind_plain_sync(platform: str | None) -> bool:
    return bool(platform_cfg(platform).get("bind_plain_sync", False))


def register_honest(platform: str | None) -> bool:
    return bool(platform_cfg(platform).get("register_honest", False))


def sync_local_only(platform: str | None = None) -> bool:
    cfg = platform_cfg(platform)
    if platform:
        return bool(cfg.get("sync_local_only", False))
    root = load_policy()
    return any(
        bool(p.get("sync_local_only")) for p in root.get("platforms", {}).values()
    )