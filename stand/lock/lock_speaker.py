# -*- coding: utf-8 -*-
"""После нашей прошивки сам ставит SSH и закрывает обновления.

Стенд зовёт note_and_arm, когда колонка уже на версии из firmware/,
и on_adb, когда ей отдан ответ с включённым ADB. Пока версия не совпала,
заводской образ не трогаем: иначе закрепление сотрёт следующая прошивка.
"""

import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path

import columns
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


def _adb_probe(ip):
    """Макс: приложение debuggable, ключ лежит в files/token. SSH у него нет."""
    if not _port(ip, 5555):
        subprocess.run([ADB, "connect", "%s:5555" % ip], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=20)
    if not _port(ip, 5555):
        return ""
    path = _adb(ip, "shell", "pm path com.yse.station", timeout=20)
    if b"package:" not in path.stdout:
        return ""
    token = secrets.token_hex(8)
    remote = (
        "run-as com.yse.station sh -c '"
        "if [ ! -s files/token ]; then printf %s " + token + " > files/token; fi; "
        "echo SHELL=yes; echo -n TOKEN=; cat files/token; echo; "
        "echo PLATFORM=yandexstation_2'"
    )
    out = _adb(ip, "shell", remote, timeout=25)
    return out.stdout.decode("utf-8", "replace")


def probe(ip):
    """Читает модель и ключ страницы. Статус «прошита» не ставит."""
    text = ""
    try:
        out = _ssh(ip, (
            "D=/data/opt/yandex-station; "
            "if [ -s $D/token ]; then echo SHELL=yes; echo -n TOKEN=; cat $D/token; echo; "
            "echo -n PLATFORM=; tr -d '\\r\\n' < $D/settings/vqe.txt; echo; "
            "else echo SHELL=no; fi"
        ))
        text = out.stdout.decode("utf-8", "replace")
        if out.returncode != 0 and "SHELL=" not in text:
            text = ""
    except Exception:
        text = ""
    if "SHELL=" not in text:
        text = _adb_probe(ip)
    if "SHELL=" not in text:
        raise RuntimeError("колонка не ответила ни по SSH, ни по ADB")
    shell = "SHELL=yes" in text
    platform = ""
    page_key = ""
    for line in text.splitlines():
        if line.startswith("PLATFORM="):
            platform = line.split("=", 1)[1].strip()
        elif line.startswith("TOKEN="):
            page_key = line.split("=", 1)[1].strip()
    if platform not in columns.TITLES:
        platform = ""
    return columns.apply_probe(ip, platform, page_key, shell)


def request(ip):
    """Кнопка «Переделать»: человек явно попросил поставить оболочку."""
    row = columns.arm(ip)
    platform = row.get("platform") or ""
    if platform in columns.ANDROID:
        _start(ip, _install_apk)
        return row
    if ip in _NOTED:
        _kick(ip, force=True)
    else:
        columns.set_job(ip, "armed", "жду, пока колонка возьмёт прошивку")
    return row


def _kick(ip, force=False):
    with _GUARD:
        if ip in _BUSY:
            return
        if not force and (not columns.armed(ip) or ip in SKIP or _disabled()):
            return
        _BUSY.add(ip)
    threading.Thread(target=_run, args=(ip,), daemon=True).start()


def _start(ip, fn):
    with _GUARD:
        if ip in _BUSY:
            return
        _BUSY.add(ip)
    threading.Thread(target=_guard, args=(ip, fn), daemon=True).start()


def _guard(ip, fn):
    try:
        fn(ip)
    except Exception as e:
        columns.set_job(ip, "fail", str(e))
        _say(ip, "ошибка %r" % (e,))
    finally:
        with _GUARD:
            _BUSY.discard(ip)


def _run(ip):
    try:
        info = _NOTED.get(ip) or {}
        platform = info.get("platform") or ""
        if platform in columns.ANDROID:
            _install_apk(ip)
            return
        version = info.get("version") or ""
        columns.set_job(ip, "work", "ставлю SSH и закрываю обновления")
        action = plan(ip, version, _port(ip, 2222), _state().get(ip), set(), False)
        if action == "done":
            columns.set_job(ip, "done", "уже готово")
            return
        if action == "check" and _ssh_settled(ip):
            _save(ip, version, "done")
            columns.set_job(ip, "done", "SSH и запрет обновлений уже на месте")
            _say(ip, "SSH и запрет обновлений уже на месте")
            return
        if not _wait_adb(ip, 180):
            columns.set_job(ip, "armed", "порт 5555 ещё закрыт, повторю на следующем запросе")
            _say(ip, "порт 5555 не открылся, повторю на следующем запросе")
            return
        _install(ip, version)
    except Exception as e:
        columns.set_job(ip, "fail", str(e))
        _say(ip, "ошибка %r" % (e,))
    finally:
        with _GUARD:
            _BUSY.discard(ip)


def _install_apk(ip):
    """Макс и Станция: обычный ADB, без root и без reader4."""
    apk = os.environ.get("YSE_MAX_APK", "/opt/yse-max/station.apk")
    if not os.path.isfile(apk):
        columns.set_job(ip, "fail", "нет APK. Соберите yse-max и положите station.apk на сервис")
        _say(ip, "нет APK %s" % apk)
        return
    columns.set_job(ip, "work", "жду ADB и ставлю приложение")
    if not _wait_adb(ip, 180):
        columns.set_job(ip, "fail", "порт 5555 не открылся")
        _say(ip, "порт 5555 не открылся")
        return
    subprocess.run([ADB, "connect", "%s:5555" % ip], stdout=subprocess.PIPE,
                   stderr=subprocess.STDOUT, timeout=20)
    installed = _adb(ip, "install", "-r", "-d", "-g", apk, timeout=180)
    text = installed.stdout.decode("utf-8", "replace")
    if installed.returncode != 0 or "Success" not in text:
        raise RuntimeError("приложение не встало")
    _write_server(ip)
    script = (
        "cmd appops set com.yandex.io.sdk RECORD_AUDIO deny;"
        "pm revoke com.yandex.io.sdk android.permission.RECORD_AUDIO;"
        "pm disable-user --user 0 com.yandex.tv.alice;"
        "appops set com.yse.station WRITE_SETTINGS allow;"
        "pm grant com.yse.station android.permission.READ_LOGS;"
        "pm grant com.yse.station android.permission.ACCESS_FINE_LOCATION;"
        "pm grant com.yse.station android.permission.ACCESS_COARSE_LOCATION;"
        "settings put secure assistant com.yse.station/com.yse.station.VoiceAssist;"
        "settings put secure voice_interaction_service com.yse.station/com.yse.station.VoiceAssist;"
        "am start-foreground-service -n com.yse.station/.Web"
    )
    done = _adb(ip, "shell", script, timeout=60)
    if done.returncode != 0:
        raise RuntimeError("приложение встало, настройки не применились")
    columns.set_job(ip, "done", "приложение поставлено")
    _say(ip, "приложение поставлено")


def _write_server(ip):
    """Адрес голоса в files/server.txt. Приложение читает его вместо константы в исходнике."""
    host = os.environ.get("YSE_VOICE_HOST", "").strip()
    base = os.environ.get("YSE_VOICE_BASE", "18805").strip() or "18805"
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
        return
    if not re.fullmatch(r"\d{2,5}", base):
        return
    _adb(ip, "shell",
         "run-as com.yse.station sh -c 'mkdir -p files && printf \"%s\\n%s\\n\" > files/server.txt'"
         % (host, base), timeout=20)


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
        columns.set_job(ip, "done", "уже закреплена")
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
    columns.set_job(ip, "done", "SSH жив после перезагрузки")
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
