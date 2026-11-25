# -*- coding: utf-8 -*-
"""Слово, ответы, музыка, спутник, обучение, сторож и root Макса. Раньше это были службы systemd."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

STORE = Path("/storage/yse")
PKG_HOST = Path("/storage/yse/pkg/host")
APP = PKG_HOST if (PKG_HOST / "yse_talk.py").is_file() else STORE / "app"
UNITS = STORE / "run" / "units"
RESTART = STORE / "run" / "restart"
VENV = Path("/storage/yse-mww/trainer/.venv/bin/python")
children = {}
stop_all = False


def log(text):
    print(text, flush=True)


def on_stop(signum, _frame):
    global stop_all
    stop_all = True


def read_env(path):
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def mark(unit, pid):
    UNITS.mkdir(parents=True, exist_ok=True)
    (UNITS / unit).write_text(str(pid), encoding="utf-8")


def clear(unit):
    path = UNITS / unit
    if path.exists():
        path.unlink()


def launch(unit, argv, env, cwd):
    proc = subprocess.Popen(argv, env=env, cwd=cwd)
    children[unit] = proc
    mark(unit, proc.pid)
    log("start %s pid %s" % (unit, proc.pid))


def halt(unit):
    proc = children.pop(unit, None)
    clear(unit)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def specs():
    found = []
    base = os.environ.copy()
    base["PYTHONUNBUFFERED"] = "1"
    base["YSE_DOCKER"] = "1"
    found.append(("yse-discover.service", [sys.executable, "-u", str(APP / "yse_discover.py")], base, None))
    if VENV.is_file():
        train = base.copy()
        train["TF_CPP_MIN_LOG_LEVEL"] = "2"
        found.append((
            "yse-train.service",
            [str(VENV), "-u", str(APP / "wake_train.py"), "serve"],
            train,
            "/storage/yse-mww/trainer",
        ))
    for path in sorted(STORE.glob("*.env")):
        if path.name in ("discover.env",):
            continue
        sid = path.stem
        env = read_env(path)
        found.append(("yse-talk@%s.service" % sid, [sys.executable, "-u", str(APP / "yse_talk.py")], env, None))
        found.append(("yse-music@%s.service" % sid, [sys.executable, "-u", str(APP / "yse_music.py")], env, None))
        found.append(("yse-sat@%s.service" % sid, [sys.executable, "-u", str(APP / "yse_sat.py")], env, None))
        if VENV.is_file():
            wake = env.copy()
            wake["CUDA_VISIBLE_DEVICES"] = ""
            wake["TF_CPP_MIN_LOG_LEVEL"] = "2"
            found.append((
                "yse-wake@%s.service" % sid,
                [str(VENV), "-u", str(APP / "mww_wake_listen.py")],
                wake,
                "/storage/yse-mww",
            ))
        if env.get("YSE_PLAY") == "http":
            root = env.copy()
            root["YSE_SPEAKER_ID"] = sid
            found.append((
                "yse-max-autoroot@%s.service" % sid,
                [sys.executable, "-u", str(APP / "station_max_autoroot_watchdog.py")],
                root,
                None,
            ))
    return found


def reap():
    for unit, proc in list(children.items()):
        if proc.poll() is None:
            continue
        log("exit %s code %s" % (unit, proc.returncode))
        children.pop(unit, None)
        clear(unit)


def take_restarts():
    if not RESTART.is_dir():
        return
    for path in list(RESTART.iterdir()):
        path.unlink()
        halt(path.name)


def main():
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    log("docker speaker services, scripts %s" % APP)
    while not stop_all:
        reap()
        take_restarts()
        wanted = {item[0]: item for item in specs()}
        for unit in list(children):
            if unit not in wanted:
                halt(unit)
        for unit, item in wanted.items():
            proc = children.get(unit)
            if proc is not None and proc.poll() is None:
                continue
            _unit, argv, env, cwd = item
            try:
                launch(unit, argv, env, cwd)
            except OSError as exc:
                log("fail %s %s" % (unit, exc))
        time.sleep(2)
    for unit in list(children):
        halt(unit)


if __name__ == "__main__":
    main()
