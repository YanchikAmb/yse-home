# Сторож колонок на сервере. Раз в минуту ищет в сети колонку с root-входом
# (dropbear :2222; спальня — метод none, Мини 2 — пустой пароль) и без нашей оболочки.
# Home Assistant видит их через GET /v1/found и показывает «Обнаружено». После «Добавить»
# HA зовёт POST /v1/install. GET /netsound — страница, с которой телефон звуком отдаёт
# Wi‑Fi сброшенной колонке. Сторож сам выбирает имя и порты и ставит оболочку.
# Ставить можно только найденную колонку и только по запросу с адреса HA. Работает от root.
import hashlib
import ipaddress
import json
import os
import pathlib
import pwd
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST_IP = os.environ.get("YSE_HOST_IP", "192.168.209.50")
NET = ipaddress.ip_network(os.environ.get("YSE_NET", HOST_IP.rsplit(".", 1)[0] + ".0/24"), strict=False)
HA_IP = os.environ.get("YSE_HA_IP", "").strip()
SKIP = set(filter(None, os.environ.get("YSE_SKIP", "").replace(" ", "").split(",")))
REF = os.environ.get("YSE_REF_SPEAKER", "192.168.209.105")
PORT = int(os.environ.get("YSE_DISCOVER_PORT", "18760"))
PKG = pathlib.Path(os.environ.get("YSE_PKG", "/storage/yse/pkg"))
STORE = pathlib.Path("/storage/yse")
SETUP = PKG / "install" / "yse_station_setup.py"
EVERY = 60
SSH = ["/storage/yse/bin/yse-ssh"]
APP = pathlib.Path("/storage/yse/app")
SPEAKER_DIR = "/data/opt/yandex-station"
# Скрипт сервера -> какие службы перезапустить, если файл изменился.
HOST_UNITS = {
    "yse_talk.py": "yse-talk@",
    "yse_pcm_push.py": "yse-talk@",
    "yse_music.py": "yse-music@",
    "mww_wake_listen.py": "yse-wake@",
    "wake_collect.py": "yse-wake@",
    "wake_train.py": "yse-train",
    "yse_sat.py": "yse-sat@",
}
BUILD_OUTS = (pathlib.Path("/storage/yse/build/out"), pathlib.Path("/tmp/yse-build/out"))
pushed = {}
push_fail = {}
host_cache = {}
release_cache = {}
release_lock = threading.Lock()
PROBE = (
    "grep -qE 'station-midi|station-mini2' /proc/device-tree/compatible 2>/dev/null && echo board; "
    "grep -q station-mini2 /proc/device-tree/compatible 2>/dev/null && echo mini2; "
    "[ -f /system/vendor/quasar/libs/libyandex_evil.so ] && echo evil; "
    "D=/data/opt/yandex-station; [ -f $D/house.pid ] && kill -0 $(cat $D/house.pid) 2>/dev/null && echo installed"
)
MINI2 = {
    "wake.txt": "джарвис",
    "vqe.txt": "yandexmini_2",
    "capture.txt": "hw:0,1 8",
    "spotter.txt": "Yandex yandexmini_2",
}

found = {}
jobs = {}
lock = threading.Lock()
STATIC = {
    "/netsound": ("netsound.html", "text/html; charset=utf-8"),
    "/netsound.html": ("netsound.html", "text/html; charset=utf-8"),
    "/netsound.js": ("netsound.js", "text/javascript; charset=utf-8"),
}


def log(text):
    print(text, flush=True)


def port_open(ip):
    try:
        with socket.create_connection((ip, 2222), timeout=0.5):
            return True
    except OSError:
        return False


def probe(ip):
    try:
        proc = subprocess.run(SSH + [ip, PROBE], capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        return set()
    return set(proc.stdout.decode("utf-8", "replace").split())


def scan_once():
    hosts = [str(a) for a in NET.hosts() if str(a) not in SKIP and str(a) != HOST_IP]
    with ThreadPoolExecutor(64) as pool:
        open_ips = [ip for ip, ok in zip(hosts, pool.map(port_open, hosts)) if ok]
    seen = {}
    for ip in open_ips:
        with lock:
            if ip in jobs and jobs[ip]["state"] == "running":
                continue
        marks = probe(ip)
        if {"board", "evil"} <= marks and "installed" not in marks:
            tail = ip.rsplit(".", 1)[1]
            seen[ip] = {"ip": ip, "name": "Колонка %s" % tail,
                        "kind": "Мини 2" if "mini2" in marks else "Станция 2"}
    with lock:
        new = set(seen) - set(found)
        found.clear()
        found.update(seen)
    for ip in sorted(new):
        log("found %s" % ip)


def speaker_known(ip):
    for env in STORE.glob("*.env"):
        text = env.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^YSE_SPEAKER=%s\s*$" % re.escape(ip), text, re.M):
            return True
    return False


def take_queue():
    """Стенд кладёт сюда IP после SSH. Дальше ставим оболочку и файл портов."""
    folder = STORE / "queue"
    if not folder.is_dir():
        return
    for path in sorted(folder.iterdir()):
        ip = path.name
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
            path.unlink()
            continue
        path.unlink()
        if speaker_known(ip):
            log("queue %s already" % ip)
            continue
        with lock:
            if ip in jobs and jobs[ip].get("state") == "running":
                continue
            jobs[ip] = {"state": "running", "step": "старт"}
        threading.Thread(target=install, args=(ip,), daemon=True).start()
        log("queue %s" % ip)


def scanner():
    while True:
        try:
            scan_once()
        except Exception as exc:
            log("scan fail %s" % type(exc).__name__)
        time.sleep(EVERY)


def queue_loop():
    while True:
        try:
            take_queue()
        except Exception as exc:
            log("queue fail %s" % type(exc).__name__)
        time.sleep(5)


def used_slots():
    try:
        base = int(os.environ.get("YSE_VOICE_BASE", "18805"))
    except ValueError:
        base = 18805
    try:
        sat = int(os.environ.get("YSE_SAT_PORT", "10700"))
    except ValueError:
        sat = 10700
    bases, sats = {base - 10}, {sat}
    for env in STORE.glob("*.env"):
        text = env.read_text(encoding="utf-8", errors="replace")
        for key, bag in (("YSE_BASE", bases), ("YSE_SAT_PORT", sats)):
            m = re.search(r"^%s=(\d+)" % key, text, re.M)
            if m:
                bag.add(int(m.group(1)))
    return max(bases) + 10, max(sats) + 1


def step(ip, text):
    with lock:
        jobs[ip]["step"] = text
    log("job %s %s" % (ip, text))


def setup(args, logf, timeout):
    env = dict(os.environ, YSE_LOCAL="1", YSE_BUILD="/storage/yse/build", PYTHONIOENCODING="utf-8")
    proc = subprocess.run(["python3", str(SETUP)] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          env=env, timeout=timeout)
    out = proc.stdout.decode("utf-8", "replace")
    logf.write("$ %s\n%s\n" % (" ".join(args[:1]), out))
    logf.flush()
    return proc.returncode, out


def install(ip):
    sid = "st" + ip.rsplit(".", 1)[1]
    base, sat = used_slots()
    marks = probe(ip)
    conf = {"id": sid, "name": "Колонка %s" % ip.rsplit(".", 1)[1], "area": "", "speaker": ip,
            "host": HOST_IP, "base": base, "sat_port": sat}
    if "mini2" in marks:
        conf["cutoff"] = "0.85"
        conf["defaults"] = MINI2
    (STORE / "speakers").mkdir(parents=True, exist_ok=True)
    (STORE / "jobs").mkdir(parents=True, exist_ok=True)
    path = STORE / "speakers" / ("%s.json" % sid)
    path.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(STORE / "jobs" / ("%s.log" % sid), "w", encoding="utf-8") as logf:
        try:
            if not (pathlib.Path("/storage/yse/build/out") / "ear64").exists():
                step(ip, "сборка")
                code, out = setup(["build"], logf, 900)
                if code:
                    raise RuntimeError("сборка")
            step(ip, "колонка")
            speaker_args = ["speaker", "--conf", str(path), "--yes"]
            if REF:
                speaker_args += ["--from", REF, "--noupdate"]
            code, out = setup(speaker_args, logf, 1800)
            if code and "уже стоит" not in out:
                raise RuntimeError("колонка")
            step(ip, "сервер")
            code, out = setup(["host", "--conf", str(path), "--yes"], logf, 600)
            if code:
                raise RuntimeError("сервер")
            code, out = setup(["key", "--conf", str(path)], logf, 60)
            token = out.strip().splitlines()[-1] if out.strip() else ""
            if code or not re.fullmatch(r"[0-9a-fA-F]{8,64}", token):
                raise RuntimeError("ключ")
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            with lock:
                jobs[ip].update(state="error", step=str(exc))
            log("job %s error %s" % (ip, exc))
            return
    with lock:
        jobs[ip].update(state="done", step="готово", token=token, name=conf["name"],
                        sat_port=sat, server=HOST_IP)
        found.pop(ip, None)
    log("job %s done" % ip)


def md5_bytes(data):
    return hashlib.md5(data).hexdigest()


def built_bin(name):
    for root in BUILD_OUTS:
        binary = root / name
        if binary.is_file():
            return binary
    return None


def speaker_files():
    """Страница, опрос, ystationd и будильник, если они собраны."""
    items = []
    page = PKG / "www" / "index.html"
    if page.is_file():
        items.append(("www/index.html", page.read_bytes().replace(b"\r\n", b"\n"), "644", "page"))
    script = PKG / "init" / "yse-update.sh"
    if script.is_file():
        items.append(("bin/yse-update", script.read_bytes().replace(b"\r\n", b"\n"), "755", "self"))
    binary = built_bin("ystationd")
    if binary is not None:
        items.append(("bin/ystationd", binary.read_bytes(), "755", "page"))
    binary = built_bin("alarmd")
    if binary is not None:
        items.append(("bin/alarmd", binary.read_bytes(), "755", "alarm"))
    binary = built_bin("keys")
    if binary is not None:
        items.append(("bin/keys", binary.read_bytes(), "755", "keys"))
    binary = built_bin("commandd")
    if binary is not None:
        items.append(("bin/commandd", binary.read_bytes(), "755", "command"))
    binary = built_bin("housed")
    if binary is not None:
        items.append(("bin/housed", binary.read_bytes(), "755", "house"))
    return items


def source_sig():
    """Метка времени и размер. Пока они те же, файлы заново не читаются."""
    sig = []
    for path in (PKG / "www" / "index.html", PKG / "init" / "yse-update.sh"):
        if path.is_file():
            st = path.stat()
            sig.append((str(path), st.st_mtime_ns, st.st_size))
    for name in ("ystationd", "alarmd", "keys", "commandd", "housed"):
        binary = built_bin(name)
        if binary is not None:
            st = binary.stat()
            sig.append((str(binary), st.st_mtime_ns, st.st_size))
    return tuple(sig)


def release():
    """Номер версии колоночных файлов. Номер растёт, только когда состав файлов изменился."""
    with release_lock:
        sig = source_sig()
        if release_cache.get("sig") == sig and release_cache.get("text"):
            return release_cache["items"], release_cache["version"], release_cache["text"]
        items = speaker_files()
        lines = ["file %s %s %s %s" % (name, md5_bytes(data), mode, tag) for name, data, mode, tag in items]
        body = "\n".join(lines) + ("\n" if lines else "")
        stamp = md5_bytes(body.encode("utf-8"))
        stamp_path = STORE / "update.stamp"
        ver_path = STORE / "update.version"
        old_stamp = stamp_path.read_text(encoding="utf-8", errors="replace").strip() if stamp_path.is_file() else ""
        try:
            version = int(ver_path.read_text(encoding="utf-8", errors="replace").strip() or "0")
        except (OSError, ValueError):
            version = 0
        if old_stamp != stamp:
            version += 1
            STORE.mkdir(parents=True, exist_ok=True)
            ver_path.write_text("%s\n" % version, encoding="utf-8")
            stamp_path.write_text(stamp + "\n", encoding="utf-8")
            log("version %s" % version)
        text = "version %s\n%s" % (version, body)
        release_cache.update(sig=sig, items=items, version=version, text=text)
        return items, version, text


def file_by_name(name, items):
    for item in items:
        if item[0] == name:
            return item
    return None


def ssh(ip, cmd, data=None, timeout=20):
    try:
        proc = subprocess.run(SSH + [ip, cmd], input=data, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return 124, b""
    return proc.returncode, proc.stdout


def our_speakers():
    """Колонки с оболочкой. Макс ходит по HTTP и чужой системы, Катя в SKIP."""
    ips = []
    for env in sorted(STORE.glob("*.env")):
        text = env.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^YSE_PLAY=http\s*$", text, re.M):
            continue
        found_ip = re.search(r"^YSE_SPEAKER=([0-9.]+)\s*$", text, re.M)
        if found_ip and found_ip.group(1) not in SKIP and found_ip.group(1) not in ips:
            ips.append(found_ip.group(1))
    return ips


def active_units(prefix):
    if os.environ.get("YSE_DOCKER") == "1":
        folder = pathlib.Path("/storage/yse/run/units")
        if not folder.is_dir():
            return []
        return sorted(path.name for path in folder.iterdir() if path.name.startswith(prefix))
    proc = subprocess.run(
        ["systemctl", "list-units", prefix + "*", "--state=active", "--no-legend", "--plain"],
        capture_output=True, text=True)
    names = []
    for line in proc.stdout.splitlines():
        name = (line.split() or [""])[0]
        if name.startswith(prefix):
            names.append(name)
    return names


def restart_units(units):
    if os.environ.get("YSE_DOCKER") == "1":
        folder = pathlib.Path("/storage/yse/run/restart")
        folder.mkdir(parents=True, exist_ok=True)
        for unit in units:
            (folder / unit).write_text("1", encoding="utf-8")
        return
    subprocess.run(["systemctl", "restart"] + units, capture_output=True)


def sync_host():
    """Скрипты из пакета — в /storage/yse/app. Перезапуск только у того, чей файл изменился."""
    try:
        owner = pwd.getpwnam(os.environ.get("YSE_USER", "administrator"))
    except KeyError:
        owner = None
    restart = set()
    for name, prefix in HOST_UNITS.items():
        src = PKG / "host" / name
        if not src.is_file():
            continue
        st = src.stat()
        sig = (st.st_mtime_ns, st.st_size)
        if host_cache.get(name) == sig:
            continue
        data = src.read_bytes().replace(b"\r\n", b"\n")
        dst = APP / name
        if dst.is_file() and dst.read_bytes() == data:
            host_cache[name] = sig
            continue
        APP.mkdir(parents=True, exist_ok=True)
        tmp = APP / (name + ".new")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o644)
        if owner:
            os.chown(tmp, owner.pw_uid, owner.pw_gid)
        tmp.replace(dst)
        host_cache[name] = sig
        restart.add(prefix)
        log("host %s" % name)
    units = []
    for prefix in restart:
        units.extend(active_units(prefix))
    if units:
        restart_units(units)
        log("restart %s" % " ".join(units))


def remembered(ip):
    if ip not in pushed:
        path = STORE / "pushed" / ip
        if path.is_file():
            pushed[ip] = path.read_text(encoding="utf-8", errors="replace").strip()
    return pushed.get(ip, "")


def remember(ip, version):
    pushed[ip] = str(version)
    folder = STORE / "pushed"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ip).write_text(str(version) + "\n", encoding="utf-8")


def deliver(ip, items, version):
    """Кладёт отличающиеся файлы. Дом, ухо и музыку не перезапускает."""
    code, out = ssh(ip, "kill -0 $(cat %s/house.pid) 2>/dev/null && echo up" % SPEAKER_DIR, timeout=12)
    if code or b"up" not in out:
        return False
    page = False
    self_changed = False
    alarm = False
    keys = False
    command = False
    house = False
    for name, data, mode, tag in items:
        remote = "%s/%s" % (SPEAKER_DIR, name)
        code, out = ssh(ip, "md5sum %s 2>/dev/null" % remote, timeout=12)
        current = out.split()[0].decode() if out.split() else ""
        if current == md5_bytes(data):
            continue
        ssh(ip, "mkdir -p %s" % remote.rsplit("/", 1)[0], timeout=12)
        code, _out = ssh(ip, "cat > %s.new" % remote, data, timeout=40)
        if code:
            return False
        code, _out = ssh(ip, "chmod %s %s.new && mv %s.new %s" % (mode, remote, remote, remote), timeout=12)
        if code:
            return False
        page = page or tag == "page"
        self_changed = self_changed or tag == "self"
        alarm = alarm or tag == "alarm"
        keys = keys or tag == "keys"
        command = command or tag == "command"
        house = house or tag == "house"
        log("push %s %s" % (ip, name))
    init = PKG / "init" / "S99ystation"
    if init.is_file():
        data = init.read_bytes().replace(b"\r\n", b"\n")
        code, out = ssh(ip, "md5sum /etc/init.d/S99ystation 2>/dev/null", timeout=12)
        current = out.split()[0].decode() if out.split() else ""
        if current != md5_bytes(data):
            code, _out = ssh(ip, "cat > %s/S99ystation.new" % SPEAKER_DIR, data, timeout=20)
            if code:
                return False
            script = (
                "mini=0\n"
                "grep -q station-mini2 /proc/device-tree/compatible 2>/dev/null && mini=1\n"
                "mount -o remount,rw /\n"
                "mv %s/S99ystation.new /etc/init.d/S99ystation\n"
                "chmod 755 /etc/init.d/S99ystation\n"
                "[ \"$mini\" = 1 ] && mount -o remount,ro /\n"
                "true\n"
            ) % SPEAKER_DIR
            code, _out = ssh(ip, "sh -s", script.encode(), timeout=20)
            if code:
                return False
            log("push %s S99ystation" % ip)
    code, _out = ssh(ip, "printf '%%s\\n' '%s' > %s/update.version" % (int(version), SPEAKER_DIR), timeout=12)
    if code:
        return False
    if page:
        code, out = ssh(ip, "/etc/init.d/S99ystation restart; echo yse-done", timeout=20)
    elif self_changed:
        code, out = ssh(
            ip,
            "if [ -f %s/yse-update.pid ]; then kill $(cat %s/yse-update.pid) 2>/dev/null; rm -f %s/yse-update.pid; fi; "
            "/etc/init.d/S99ystation start; echo yse-done" % (SPEAKER_DIR, SPEAKER_DIR, SPEAKER_DIR),
            timeout=20)
    else:
        code, out = ssh(ip, "/etc/init.d/S99ystation start; echo yse-done", timeout=20)
    if code != 0 or b"yse-done" not in out:
        return False
    if alarm or keys or command or house:
        parts = []
        if alarm:
            parts.append("if [ -f %s/alarm.pid ]; then kill $(cat %s/alarm.pid) 2>/dev/null; rm -f %s/alarm.pid; fi" % (SPEAKER_DIR, SPEAKER_DIR, SPEAKER_DIR))
        if keys:
            parts.append("if [ -f %s/keys.pid ]; then kill $(cat %s/keys.pid) 2>/dev/null; rm -f %s/keys.pid; fi" % (SPEAKER_DIR, SPEAKER_DIR, SPEAKER_DIR))
        if command:
            parts.append("if [ -f %s/command.pid ]; then kill $(cat %s/command.pid) 2>/dev/null; rm -f %s/command.pid; fi" % (SPEAKER_DIR, SPEAKER_DIR, SPEAKER_DIR))
        if house:
            parts.append("if [ -f %s/house.pid ]; then kill $(cat %s/house.pid) 2>/dev/null; rm -f %s/house.pid; fi" % (SPEAKER_DIR, SPEAKER_DIR, SPEAKER_DIR))
        parts.append("/etc/init.d/S99house start; echo yse-done")
        code, out = ssh(ip, "; ".join(parts), timeout=20)
        return code == 0 and b"yse-done" in out
    return True


def publish_once():
    if not PKG.is_dir():
        return
    try:
        sync_host()
    except OSError as exc:
        log("host fail %s" % exc)
    items, version, _text = release()
    now = time.time()
    for ip in our_speakers():
        if remembered(ip) == str(version):
            continue
        if now - push_fail.get(ip, 0) < 600:
            continue
        try:
            ok = deliver(ip, items, version)
        except Exception as exc:
            log("push %s fail %s" % (ip, type(exc).__name__))
            ok = False
        if ok:
            remember(ip, version)
            push_fail.pop(ip, None)
            log("push %s ok" % ip)
        else:
            push_fail[ip] = now
            log("push %s later" % ip)


def publisher():
    while True:
        try:
            publish_once()
        except Exception as exc:
            log("publish fail %s" % type(exc).__name__)
        time.sleep(EVERY)


class Api(BaseHTTPRequestHandler):
    def reply(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def trusted(self):
        return self.client_address[0] in (HA_IP, "127.0.0.1")

    def send_static(self, name, ctype):
        path = PKG / "www" / name
        try:
            data = path.read_bytes()
        except OSError:
            self.reply(404, {"error": "page"})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        static = STATIC.get(url.path)
        if static:
            self.send_static(*static)
            return
        if url.path == "/v1/update":
            _items, _version, text = release()
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/v1/update/file":
            name = (parse_qs(url.query).get("name") or [""])[0]
            item = file_by_name(name, release()[0]) if re.fullmatch(r"[a-z0-9_./-]{1,80}", name) and ".." not in name else None
            if not item:
                self.reply(404, {"error": "file"})
                return
            data = item[1]
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if url.path == "/v1/found":
            with lock:
                self.reply(200, {"server": HOST_IP, "found": list(found.values())})
            return
        if url.path == "/v1/job":
            ip = (parse_qs(url.query).get("ip") or [""])[0]
            with lock:
                job = dict(jobs.get(ip) or {"state": "none"})
            if not self.trusted():
                job.pop("token", None)
            self.reply(200, job)
            return
        self.reply(404, {"error": "path"})

    def do_POST(self):
        if urlparse(self.path).path != "/v1/install":
            self.reply(404, {"error": "path"})
            return
        if not self.trusted():
            self.reply(403, {"error": "only HA"})
            return
        length = int(self.headers.get("Content-Length") or "0")
        try:
            ip = json.loads(self.rfile.read(length) if 0 < length < 200 else b"{}").get("ip", "")
        except ValueError:
            ip = ""
        with lock:
            if ip in jobs and jobs[ip]["state"] == "running":
                self.reply(200, {"state": "running"})
                return
            if ip not in found:
                self.reply(400, {"error": "not found"})
                return
            jobs[ip] = {"state": "running", "step": "старт"}
        threading.Thread(target=install, args=(ip,), daemon=True).start()
        self.reply(200, {"state": "running"})

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    log("discover %s port %d skip %d" % (NET, PORT, len(SKIP)))
    threading.Thread(target=scanner, daemon=True).start()
    threading.Thread(target=queue_loop, daemon=True).start()
    threading.Thread(target=publisher, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Api).serve_forever()
