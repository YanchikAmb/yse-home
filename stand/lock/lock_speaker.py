# -*- coding: utf-8 -*-
"""После нашей прошивки сам ставит SSH и закрывает обновления.

Стенд зовёт note_and_arm, когда колонка уже на версии из firmware/,
и on_adb, когда ей отдан ответ с включённым ADB. Пока версия не совпала,
заводской образ не трогаем: иначе закрепление сотрёт следующая прошивка.
"""

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

from log_time import log_timestamp

HERE = Path(__file__).resolve().parent
ADB = os.environ.get("YSE_ADB", "adb")
RUN = Path(os.environ.get("LOCK_RUN", "/opt/lite-kit/run"))
LOG = Path(os.environ.get("LOCK_LOG", "/opt/lite-kit/log"))
SKIP = {ip.strip() for ip in os.environ.get(
    "LOCK_SKIP", "").split(",") if ip.strip()}

_GUARD = threading.Lock()
_BUSY = set()
_NOTED = {}
_STATE = None


def plan(ip, version, ssh_open, state, skip, disabled):
    """skip — не трогать. done — уже закреплена. check — SSH есть, сверка. lock — ставить."""
    if disabled or ip in skip:
        return "skip"
    if (ssh_open and isinstance(state, dict) and state.get("state") == "done"
            and state.get("version") == version):
        return "done"
    if ssh_open:
        return "check"
    return "lock"


def note_and_arm(ip, platform, version):
    if not version:
        return
    with _GUARD:
        _NOTED[ip] = {"platform": platform, "version": version, "ts": time.time()}
    _say(ip, "на нашей прошивке %s, платформа %s" % (version, platform or "?"))
    _kick(ip)


def on_adb(ip):
    with _GUARD:
        info = _NOTED.get(ip)
    if not info or time.time() - info["ts"] > 3600:
        return
    _kick(ip)


def _disabled():
    return os.environ.get("LOCK_DISABLE") == "1" or (RUN / "no_lock").exists()


def _state():
    global _STATE
    if _STATE is None:
        try:
            _STATE = json.loads((RUN / "lock-state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _STATE = {}
    return _STATE


def _save(ip, version, status):
    data = _state()
    data[ip] = {"version": version, "state": status, "at": log_timestamp()}
    RUN.mkdir(parents=True, exist_ok=True)
    tmp = RUN / "lock-state.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(RUN / "lock-state.json")


def _say(ip, msg):
    print("  >>> [LOCK] %s %s <<<" % (ip, msg))
    try:
        import sys
        sys.stdout.flush()
    except Exception:
        pass


def _kick(ip):
    with _GUARD:
        if _disabled() or ip in SKIP or ip in _BUSY:
            return
        _BUSY.add(ip)
    threading.Thread(target=_run, args=(ip,), daemon=True).start()


def _run(ip):
    try:
        info = _NOTED.get(ip) or {}
        version = info.get("version") or ""
        action = plan(ip, version, _port(ip, 2222), _state().get(ip), SKIP, _disabled())
        if action == "skip":
            return
        if action == "done":
            return
        if action == "check" and _ssh_settled(ip):
            _save(ip, version, "done")
            _say(ip, "SSH и запрет обновлений уже на месте")
            return
        if not _wait_adb(ip, 180):
            _say(ip, "порт 5555 не открылся, повторю на следующем запросе")
            return
        _install(ip, version)
    except Exception as e:
        _say(ip, "ошибка %r" % (e,))
    finally:
        with _GUARD:
            _BUSY.discard(ip)


def _port(ip, port):
    sock = socket.socket()
    sock.settimeout(2)
    try:
        return sock.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def _wait_adb(ip, seconds):
    if _port(ip, 5555):
        return True
    _say(ip, "жду ADB")
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(3)
        if _port(ip, 5555):
            return True
    return False


def _adb(ip, *args, timeout=90):
    cmd = [ADB, "-s", "%s:5555" % ip, *args]
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)


def _connect(ip):
    subprocess.run([ADB, "connect", "%s:5555" % ip], stdout=subprocess.PIPE,
                   stderr=subprocess.STDOUT, timeout=20)
    out = _adb(ip, "shell", "id")
    text = out.stdout.decode("utf-8", "replace")
    if out.returncode != 0 or "uid=0" not in text:
        raise RuntimeError("adb не root")


def _ssh(ip, remote):
    env = os.environ.copy()
    env["SSH_ASKPASS"] = str(HERE / "askpass.sh")
    env["SSH_ASKPASS_REQUIRE"] = "force"
    env["DISPLAY"] = "yse"
    return subprocess.run(
        ["ssh", "-n", "-p", "2222",
         "-o", "PreferredAuthentications=password",
         "-o", "PubkeyAuthentication=no",
         "-o", "NumberOfPasswordPrompts=1",
         "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null",
         "-o", "LogLevel=ERROR",
         "-o", "ConnectTimeout=8",
         "root@%s" % ip, remote],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=25, env=env)


PROBE = (
    "r=;"
    "pidof dropbear >/dev/null && r=\"$r dropbear\";"
    "grep -q 'disabled by yandex-noupdate' /system/vendor/quasar/device_ota_update.sh && r=\"$r applier\";"
    "grep -q -- '--updatesd' /system/vendor/quasar/launch.json || r=\"$r launchno\";"
    "ps w 2>/dev/null | grep -q '[-]-updatesd' && r=\"$r running\";"
    "echo $r"
)


def _marks(text):
    return set(text.split())


def _settled(text):
    marks = _marks(text)
    return {"dropbear", "applier", "launchno"} <= marks and "running" not in marks


def _ssh_settled(ip):
    try:
        out = _ssh(ip, PROBE)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and _settled(out.stdout.decode("utf-8", "replace"))


def _install(ip, version):
    _connect(ip)
    probe = _adb(ip, "shell", PROBE)
    text = probe.stdout.decode("utf-8", "replace")
    marks = _marks(text)
    if _settled(text):
        _save(ip, version, "done")
        _say(ip, "уже закреплена, перезагрузка не нужна")
        return
    if not {"dropbear", "applier", "launchno"} <= marks:
        _push(ip)
        noup = _adb(ip, "shell", "sh /data/opt/yandex-noupdate.sh")
        out = noup.stdout.decode("utf-8", "replace")
        if noup.returncode != 0 or "ГОТОВО" not in out:
            raise RuntimeError("запрет обновлений не встал")
        _backup(ip)
        _say(ip, "SSH поставлен, обновления закрыты")
    else:
        _say(ip, "файлы уже закрыты, перезагружаю чтобы остановить updatesd")
    _adb(ip, "reboot", timeout=20)
    if not _wait_port(ip, 2222, 150):
        raise RuntimeError("после перезагрузки нет SSH")
    _save(ip, version, "done")
    _say(ip, "готово, SSH жив после перезагрузки")


def _push(ip):
    _adb(ip, "shell", "mkdir -p /data/opt/bin /data/opt/etc/dropbear")
    for name, dest in (
        ("bin/dropbear", "/data/opt/bin/dropbear"),
        ("bin/dropbearkey", "/data/opt/bin/dropbearkey"),
        ("S99zdropbear", "/data/opt/S99zdropbear"),
        ("yandex-noupdate.sh", "/data/opt/yandex-noupdate.sh"),
    ):
        put = _adb(ip, "push", str(HERE / name), dest, timeout=60)
        if put.returncode != 0:
            raise RuntimeError("не ушёл файл %s" % name)
    script = (
        "chmod 755 /data/opt/bin/dropbear /data/opt/bin/dropbearkey /data/opt/yandex-noupdate.sh;"
        "mount -o remount,rw / && cp /data/opt/S99zdropbear /etc/init.d/S99zdropbear"
        " && chmod 755 /etc/init.d/S99zdropbear && sync && mount -o remount,ro /;"
        "/etc/init.d/S99zdropbear start;"
        "mkdir -p /data/quasar;"
        "touch /data/quasar/activate_adb_quasmodrom /data/quasar/activate_network_adb;"
        "chattr +i /data/quasar/activate_adb_quasmodrom /data/quasar/activate_network_adb"
    )
    done = _adb(ip, "shell", script, timeout=40)
    if done.returncode != 0:
        raise RuntimeError("не запустился dropbear")


def _backup(ip):
    folder = LOG / "lock-backup"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / ("%s-%s.tar" % (ip.replace(".", "-"), log_timestamp("%Y%m%d-%H%M%S")))
    proc = subprocess.run(
        [ADB, "-s", "%s:5555" % ip, "exec-out", "tar -C /data/opt -cf - backup"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if proc.returncode == 0 and proc.stdout:
        dest.write_bytes(proc.stdout)
        _say(ip, "копия исходных файлов обновления %s" % dest.name)


def _wait_port(ip, port, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _port(ip, port):
            return True
        time.sleep(3)
    return False
