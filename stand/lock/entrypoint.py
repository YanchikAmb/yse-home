# -*- coding: utf-8 -*-
"""Старт стенда: закрепитель, панель :8090 на том же IP, equasar."""

import os
import subprocess
import sys

sys.path.insert(0, "/opt/yse-lock")
from install_hook import install

PANEL_ENV = {
    **os.environ,
    "STAND_RUN": "/opt/lite-kit/run",
    "STAND_FIRMWARE": "/opt/lite-kit/firmware",
    "STAND_LOG": "/opt/lite-kit/log",
    "STAND_QUASAR_URL": os.environ.get("STAND_QUASAR_URL", "http://127.0.0.1"),
    "STAND_CONTAINER": os.environ.get("STAND_CONTAINER", "yse-stand"),
    "PANEL_PORT": os.environ.get("PANEL_PORT", "8090"),
}


def start_panel() -> subprocess.Popen | None:
    path = "/opt/yse-panel/app.py"
    if not os.path.isfile(path):
        print("yse-panel: нет", path, flush=True)
        return None
    p = subprocess.Popen(
        [sys.executable, path],
        env=PANEL_ENV,
    )
    print("yse-panel pid=%s http://0.0.0.0:%s" % (p.pid, PANEL_ENV["PANEL_PORT"]), flush=True)
    return p


def main() -> int:
    try:
        changed = install("/opt/lite-kit/scripts/equasar_server.py")
    except SystemExit as ex:
        print("yse-lock hook skip: %s" % ex, flush=True)
        changed = False
    print("yse-lock hook %s" % ("installed" if changed else "already"), flush=True)
    panel = start_panel()
    scan = None
    scan_path = "/opt/yse-lock/scan_columns.py"
    if os.path.isfile(scan_path):
        scan = subprocess.Popen([sys.executable, scan_path])
        print("yse-scan pid=%s" % scan.pid, flush=True)
    os.chdir("/opt/lite-kit")
    equasar = subprocess.Popen(
        [sys.executable, "/opt/lite-kit/scripts/equasar_server.py", "--dns"],
    )
    code = equasar.wait()
    for proc in (panel, scan):
        if proc is not None and proc.poll() is None:
            proc.terminate()
    return code


if __name__ == "__main__":
    raise SystemExit(main())