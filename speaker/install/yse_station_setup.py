# -*- coding: utf-8 -*-
"""Установочный пакет своей оболочки Яндекс Станции 2 (yandexmidi).

  python yse_station_setup.py init                 (один раз: вопросы, пишет install/site.json)
  python yse_station_setup.py all     --yes        (prepare + build + server + компонент HA)
  python yse_station_setup.py prepare --yes        (пакеты, окружения Python, модели на сервере)
  python yse_station_setup.py build
  python yse_station_setup.py check   --conf ...\\install\\speakers\\bedroom.json
  python yse_station_setup.py speaker --conf ...\\new.json [--from IP_Станции_2] --yes
  python yse_station_setup.py host    --conf ...\\new.json --yes
  python yse_station_setup.py key     --conf ...\\new.json   (или --ip IP_колонки)
  python yse_station_setup.py server  --yes
  python yse_station_setup.py stt     --yes
  python yse_station_setup.py voice   --yes
  python yse_station_setup.py column  --ip IP --model max|station2|mini2 [--apk APK] --yes

build    собирает все программы колонки на сервере в /tmp/yse-build/out.
speaker  ставит оболочку на новую колонку: Quasar в сторону, программы, страница,
         обёртка aplay, настройки, host.conf. Блоб распознавания soda/ копирует
         с колонки --from. Уже установленную колонку не трогает без --upgrade.
         --upgrade меняет программы, но housed не перезапускает. --noupdate
         ещё и закрывает обновления прошивки скриптом с колонки --from.
host     ставит службы сервера для колонки: yse-wake@, yse-music@, yse-talk@, yse-sat@.
         Порты должны быть свободны, иначе отказ.
check    только читает: процессы колонки, host.conf, обёртку aplay, Quasar, службы и порты.
key      печатает ключ страницы колонки (файл token) — для поля вверху страницы и для HA.
         Только в свой терминал, в чаты и коммиты не копировать.
server   кладёт пакет в /storage/yse/pkg и включает сторожа yse-discover.
         Сторож сразу копирует изменившиеся скрипты сервера и перезапускает только
         их службы, затем выкладывает на колонки страницу и опрос обновлений.
         Колонка раз в 3 минуты сама спрашивает /v1/update, если в момент выкладки
         её не было в сети. Дом, ухо и музыка не перезапускаются. Колонки из --skip и
         с play=http в рассылку не входят.
         --skip — колонки, которые сторож не трогает; --ha — адрес HA.
stt      контейнеры распознавания: Whisper на видеокарте (127.0.0.1:18950), GigaAM
         v3_e2e_rnnt (127.0.0.1:18953) и крупный Vosk (127.0.0.1:18951), вне блоков портов
         колонок (база..база+9). Модели в /storage/yse/stt/models. --vosk, --whisper,
         --vosk-mem (лимит памяти Vosk), --hf (зеркало Hugging Face).
voice    свой голос yse-voice (127.0.0.1:18952): Silero с ударениями и RVC на видеокарте,
         модели прогреты, ответ потоком по предложениям. Библиотеки и модели — в
         /storage/yse/voice (корень сервера почти полон). --rvc-from — откуда взять
         jarvis.pth и vihrov.pth. Свои голоса учит _global_cursor_scripts/yse_rvc_train.py.

init     спрашивает адреса сервера и HA, подсеть, колонку-образец и пишет install/site.json
         (в git не входит). Пароли туда писать необязательно: без них спросит при запуске
         или возьмёт YSE_SERVER_PASSWORD / SSH-ключ.
prepare  готовит чистый сервер Ubuntu 22.04: apt (docker, кросс-gcc, ffmpeg, python3-venv),
         окружения Python слова и музыки, библиотеки служб, Vosk small, VAD, образы сборки,
         модель слова, ключ голоса Яндекса и soda/ для Станции 2 из site.json.
         Повтор безопасен: готовое пропускается.
all      prepare, build, server и компонент HA (если в site.json есть доступ к HA).
         Колонки дальше ставятся кнопкой в HA. --stt и --voice добавляют распознавание
         и свой голос (нужна видеокарта NVIDIA).

Доступ к серверу — install/site.json, без него .req (cp1251) уровнем выше репозитория.
Пароли и ключи не печатаются.
Запускается из _global_cursor_scripts (службы берутся оттуда) или из install/ репозитория
(службы берутся из host/).
"""
import argparse
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tarfile

HERE = pathlib.Path(__file__).resolve().parent
if (HERE.parent / "src" / "ear.c").exists():
    REPO = HERE.parent
    SCRIPTS = REPO / "host"
    WS = REPO.parent
else:
    WS = HERE.parent
    REPO = WS / "yandex_station"
    SCRIPTS = HERE
DIR = "/data/opt/yandex-station"
BUILD = os.environ.get("YSE_BUILD", "/tmp/yse-build")
APP = "/storage/yse/app"
HOST_SCRIPTS = ("mww_wake_listen.py", "wake_collect.py", "wake_train.py", "yse_music.py", "yse_talk.py",
                "yse_sat.py", "yse_pcm_push.py", "yse_google_ai.py", "yse_peer.py",
                "station_max_autoroot_watchdog.py")
UNITS = ("yse-wake@.service", "yse-music@.service", "yse-talk@.service", "yse-sat@.service",
         "yse-max-autoroot@.service")
SPEAKER_BIN = ("housed", "keys", "ear64", "alarmd", "commandd", "eqd", "glow", "bthead",
               "ystationd", "aplay48", "ringcat", "jarvisd")
ARM = ("arm-linux-gnueabihf-gcc -std=c11 -O2 -Wall -Wextra -Werror -Wno-format-truncation -D_FILE_OFFSET_BITS=64 "
       "-march=armv7-a -mtune=cortex-a53 -mfloat-abi=hard -mfpu=neon -I/src/src")
ARM_BUILDS = {
    "housed": "src/house.c src/mini2_led.c -ldl -lm",
    "commandd": "src/commandd.c src/mini2_led.c -ldl -lm",
    "keys": "src/keys.c src/mini2_led.c -lm",
    "bthead": "src/bthead.c",
    "alarmd": "src/alarm.c -lm",
    "eqd": "src/eqd.c src/mini2_led.c -lm",
    "glow": "src/glow.c src/mini2_led.c -lm",
    "ystationd": "-pthread src/httpd.c src/bridge.c src/mini2_led.c -lm",
}
A64 = ("aarch64-linux-gnu-gcc -std=c11 -O2 -Wall -Wextra -Werror -Wno-format-truncation "
       "-D_FILE_OFFSET_BITS=64 -I%s/src" % BUILD)
A64_BUILDS = {
    "ear64": "%s/src/ear.c %s/src/mini2_led.c -ldl -lm" % (BUILD, BUILD),
    # Мини 2: фраза всегда уходит на сервер, libsoda не открывается, загрузчик из soda/lib не нужен.
    "ear64s": "-static %s/src/ear.c %s/src/mini2_led.c -ldl -lm" % (BUILD, BUILD),
    "aplay48": "-static %s/src/aplay48.c -lm" % BUILD,
    "ringcat": "-static %s/src/ringcat.c" % BUILD,
}
# jarvisd — C++ на TFLite Micro: в образе Яндекса нет g++, бинарь статический.
CXX_IMAGE = "yse-armcxx:latest"
CXX_DOCKERFILE = ("FROM debian:bookworm-slim\n"
                  "RUN apt-get update && apt-get install -y --no-install-recommends "
                  "g++-arm-linux-gnueabihf make && rm -rf /var/lib/apt/lists/*\n")
CXX_CPU = "-O2 -march=armv7-a -mtune=cortex-a53 -mfpu=neon -mfloat-abi=hard"
FRONTEND_SRC = ("kiss_fft_int16.cc fft.cc fft_util.cc filterbank.cc filterbank_util.cc frontend.cc "
                "frontend_util.cc log_lut.cc log_scale.cc log_scale_util.cc noise_reduction.cc "
                "noise_reduction_util.cc pcan_gain_control.cc pcan_gain_control_util.cc window.cc window_util.cc")
JARVISD_BUILD = r"""
set -e
cd /src
rm -rf obj && mkdir -p obj/tflm obj/fe
CXX=arm-linux-gnueabihf-g++
CPU="%(cpu)s"
TF="-Ithird_party/tflm -Ithird_party/tflm/third_party/flatbuffers/include"
DEF="-DTF_LITE_STATIC_MEMORY -DFLATBUFFERS_LOCALE_INDEPENDENT=0 -ffunction-sections -fdata-sections"
find third_party/tflm/tensorflow -name '*.cc' | xargs -P 8 -I{} sh -c \
  '$0 -std=c++17 $1 $2 $3 -fno-rtti -fno-exceptions -c {} -o obj/tflm/$(echo {} | tr / _).o' "$CXX" "$CPU" "$TF" "$DEF"
M=third_party/microfrontend
FE="-I$M -I$M/kissfft -DFIXED_POINT=16 $DEF"
for f in %(frontend)s; do $CXX -std=c++17 $CPU $FE -c $M/tensorflow/lite/experimental/microfrontend/lib/$f -o obj/fe/$f.o; done
$CXX -std=c++17 $CPU $FE -c $M/kissfft/kiss_fft.cc -o obj/fe/kiss_fft.o
$CXX -std=c++17 $CPU $FE -c $M/kissfft/tools/kiss_fftr.cc -o obj/fe/kiss_fftr.o
SYS="-isystem third_party/tflm -isystem third_party/tflm/third_party/flatbuffers/include -isystem $M"
$CXX -std=c++17 $CPU -Wall -Wextra -Werror -Wno-format-truncation $SYS -Isrc $DEF -fno-rtti -fno-exceptions \
  -c src/jarvisd.cc -o obj/jarvisd.o
$CXX -static -Wl,--gc-sections $CPU -o out/jarvisd obj/jarvisd.o obj/fe/*.o obj/tflm/*.o -lm
arm-linux-gnueabihf-strip out/jarvisd
""" % {"cpu": CXX_CPU, "frontend": FRONTEND_SRC}
PKG_PARTS = ("src", "www", "init", "install", "host")
DISCOVER_PORT = 18760
DEFAULTS = {
    "wake.txt": "джарвис",
    "wake.sound": "1",
    "ring.txt": "1",
    "ring.color": "3aa0ff",
    "ring.bright": "160",
    "ring.effect": "wave",
    "voice.txt": "alisa",
    "nn.txt": "yandex",
    "news.txt": "yandex",
    "bt.txt": "0",
    "vqe.txt": "yandexmidi",
    "capture.txt": "hw:0,2 16",
    "spotter.txt": "Yandex yandexmidi",
    "clock.off": "3",
}


SITE_FILE = pathlib.Path(os.environ.get("YSE_SITE", str(REPO / "install" / "site.json")))
_site_cache = {}


def site():
    """Настройки дома: install/site.json, на сервере — окружение из discover.env."""
    if "data" not in _site_cache:
        data = {}
        if SITE_FILE.is_file():
            data = json.loads(SITE_FILE.read_text(encoding="utf-8"))
        _site_cache["data"] = data
    return _site_cache["data"]


def site_get(path, env=None, default=""):
    value = site()
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    if value in (None, "", []):
        value = os.environ.get(env, "") if env else ""
    return value if value not in (None, "", []) else default


def service_user():
    return site_get("service_user", "YSE_USER") or site_get("server.user") or "administrator"


def ha_url():
    url = site_get("ha.url", "YSE_HA_URL")
    if not url and os.environ.get("YSE_HA_IP"):
        url = "http://%s:8123" % os.environ["YSE_HA_IP"]
    return url.rstrip("/")


def ha_ip():
    m = re.search(r"//([^:/]+)", ha_url())
    return m.group(1) if m else ""


def skip_list():
    skip = site_get("skip", "YSE_SKIP")
    return ",".join(skip) if isinstance(skip, list) else skip


def clock_off():
    return str(site_get("clock_offset", "YSE_CLOCK_OFF", 3))


def site_path(value):
    path = pathlib.Path(value)
    return path if path.is_absolute() else (SITE_FILE.parent / path).resolve()


def creds():
    if site_get("server.ip") and not site_get("server.req"):
        host, user = site_get("server.ip"), site_get("server.user", default="root")
        pw = site_get("server.password", "YSE_SERVER_PASSWORD")
        if not pw and not site_get("server.key_file") and sys.stdin.isatty():
            import getpass
            pw = getpass.getpass("пароль %s@%s (пусто — SSH-ключ): " % (user, host))
            os.environ["YSE_SERVER_PASSWORD"] = pw
        return host, user, pw
    req = site_path(site_get("server.req")) if site_get("server.req") else WS / ".req"
    if not req.is_file():
        raise SystemExit("нет install/site.json: сначала python install/yse_station_setup.py init")
    text = req.read_bytes().decode("cp1251")
    host = user = pw = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("хост"):
            host = s.split()[-1]
        elif "логин" in s:
            user = s.split(":", 1)[-1].strip()
        elif "пароль" in s:
            pw = s.split(":", 1)[-1].strip()
    if not (host and user and pw):
        raise SystemExit("в .req нет хоста, логина или пароля")
    return host, user, pw


class LocalHost:
    """Тот же интерфейс, что у Host, но команды идут прямо на этой машине (сервер, root)."""

    def __init__(self):
        self.host = os.environ.get("YSE_HOST_IP") or site_get("server.ip") or "127.0.0.1"

    def run(self, cmd, timeout=300, sudo=False, data=None):
        try:
            proc = subprocess.run(["bash", "-c", cmd], input=(data or "").encode("utf-8"),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "timeout"
        return proc.returncode, proc.stdout.decode("utf-8", "replace")

    def put_bytes(self, remote, data):
        pathlib.Path(remote).write_bytes(data)

    def put_text(self, local, remote):
        self.put_bytes(remote, pathlib.Path(local).read_bytes().replace(b"\r\n", b"\n"))

    def mkdir(self, path):
        os.makedirs(path, exist_ok=True)


def open_host():
    if os.environ.get("YSE_LOCAL") == "1":
        return LocalHost()
    return Host()


class Host:
    def __init__(self):
        import paramiko
        self.host, user, self.pw = creds()
        self.cli = paramiko.SSHClient()
        self.cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        key = site_get("server.key_file") or None
        self.cli.connect(self.host, username=user, password=self.pw or None, key_filename=key, timeout=15,
                         allow_agent=not self.pw, look_for_keys=not self.pw)
        self.sftp = self.cli.open_sftp()

    def run(self, cmd, timeout=300, sudo=False, data=None):
        stdin, stdout, stderr = self.cli.exec_command(cmd, timeout=timeout)
        if sudo:
            stdin.write(self.pw + "\n")
        if data is not None:
            stdin.write(data)
        stdin.flush()
        stdin.channel.shutdown_write()
        out = (stdout.read() + stderr.read()).decode("utf-8", "replace")
        return stdout.channel.recv_exit_status(), out.replace(self.pw, "***") if self.pw else out

    def put_bytes(self, remote, data):
        with self.sftp.file(remote, "wb") as fh:
            fh.write(data)

    def put_text(self, local, remote):
        self.put_bytes(remote, pathlib.Path(local).read_bytes().replace(b"\r\n", b"\n"))

    def mkdir(self, path):
        self.run("mkdir -p '%s'" % path)


YSE_SSH = """#!/bin/sh
# Вход на колонку. Спальня пускает метод none. Мини 2 — пустой пароль dropbear -B.
ip=$1
shift
mark=/tmp/yse-ssh-$ip
probe() {
  timeout 6 ssh -n -o BatchMode=yes -o PreferredAuthentications=none \\
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \\
    -o LogLevel=ERROR -o ConnectTimeout=5 -p 2222 "root@$ip" true >/dev/null 2>&1
}
run_none() {
  exec ssh -o BatchMode=yes -o PreferredAuthentications=none \\
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \\
    -o LogLevel=ERROR -o ConnectTimeout=8 -p 2222 "root@$ip" "$@"
}
run_empty() {
  SSH_ASKPASS=/storage/yse/bin/ssh-empty SSH_ASKPASS_REQUIRE=force DISPLAY=yse \\
  exec ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no \\
    -o NumberOfPasswordPrompts=1 \\
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \\
    -o LogLevel=ERROR -o ConnectTimeout=8 -p 2222 "root@$ip" "$@"
}
method=
if [ -f "$mark" ]; then
  method=$(cat "$mark" 2>/dev/null || true)
fi
if [ "$method" != none ] && [ "$method" != empty ]; then
  if probe; then method=none; else method=empty; fi
  printf '%s\\n' "$method" > "$mark" 2>/dev/null || true
fi
if [ "$method" = none ]; then
  run_none "$@"
fi
run_empty "$@"
"""
SSH_EMPTY = "#!/bin/sh\nexit 0\n"

def ensure_ssh(h):
    h.mkdir("/storage/yse/bin")
    h.put_bytes("/storage/yse/bin/yse-ssh", YSE_SSH.encode("utf-8"))
    h.put_bytes("/storage/yse/bin/ssh-empty", SSH_EMPTY.encode("utf-8"))
    h.run("chmod 755 /storage/yse/bin/yse-ssh /storage/yse/bin/ssh-empty")


def speaker_ssh(ip):
    return "/storage/yse/bin/yse-ssh %s" % ip


def load_conf(path):
    conf = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    for key in ("id", "name", "speaker", "host", "base", "sat_port"):
        if key not in conf:
            raise SystemExit("в конфиге нет поля %s" % key)
    if not re.fullmatch(r"[a-z0-9_-]{1,24}", conf["id"]):
        raise SystemExit("id: латиница, цифры, - и _")
    for key in ("speaker", "host"):
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", conf[key]):
            raise SystemExit("%s: нужен IPv4" % key)
    if not 1024 <= int(conf["base"]) <= 65000:
        raise SystemExit("base вне 1024..65000")
    return conf


def cmd_build(_args):
    h = open_host()
    h.run("rm -rf %s && mkdir -p %s/src %s/out" % (BUILD, BUILD, BUILD))
    for path in sorted((REPO / "src").iterdir()):
        if path.suffix in (".c", ".cc", ".h", ".inc"):
            h.put_text(path, "%s/src/%s" % (BUILD, path.name))
    pack = io.BytesIO()
    with tarfile.open(fileobj=pack, mode="w:gz") as tar:
        tar.add(str(REPO / "third_party"), arcname="third_party")
    h.put_bytes("%s/third_party.tgz" % BUILD, pack.getvalue())
    h.run("cd %s && tar -xzf third_party.tgz && rm third_party.tgz" % BUILD)
    h.put_bytes("%s/jarvisd.sh" % BUILD, JARVISD_BUILD.encode("utf-8"))
    bad = 0
    for name, args in A64_BUILDS.items():
        code, out = h.run("%s -o %s/out/%s %s" % (A64, BUILD, name, args))
        print(("ok   " if code == 0 else "FAIL ") + name)
        if code:
            print(out[-1500:])
            bad += 1
    for name, args in ARM_BUILDS.items():
        code, out = h.run(
            "sudo -S -p '' docker run --rm -v %s:/src spotter-tap-build:latest "
            "sh -c 'cd /src && %s -o out/%s %s'" % (BUILD, ARM, name, args), sudo=True)
        print(("ok   " if code == 0 else "FAIL ") + name)
        if code:
            print(out[-1500:])
            bad += 1
    code, _ = h.run("sudo -S -p '' docker image inspect %s >/dev/null 2>&1" % CXX_IMAGE, sudo=True)
    if code:
        h.put_bytes("%s/cxx.Dockerfile" % BUILD, CXX_DOCKERFILE.encode("utf-8"))
        h.run("cd %s && sudo -S -p '' docker build -q -t %s -f cxx.Dockerfile ." % (BUILD, CXX_IMAGE),
              sudo=True, timeout=1800)
    code, out = h.run("sudo -S -p '' docker run --rm -v %s:/src %s sh /src/jarvisd.sh 2>&1"
                      % (BUILD, CXX_IMAGE), sudo=True, timeout=1800)
    print(("ok   " if code == 0 else "FAIL ") + "jarvisd")
    if code:
        print(out[-1500:])
        bad += 1
    code, out = h.run(
        "sudo -S -p '' docker run --rm -v %s:/src spotter-tap-build:latest sh -c "
        "'cd /src/out && for f in %s; do echo $f $(arm-linux-gnueabihf-objdump -T $f "
        "| grep -o \"GLIBC_[0-9.]*\" | sort -uV | tail -n 1); done'"
        % (BUILD, " ".join(ARM_BUILDS)), sudo=True)
    print("glibc: " + ", ".join(l.strip() for l in out.split("\n") if "GLIBC" in l))
    for line in out.split("\n"):
        m = re.search(r"GLIBC_(\d+)\.(\d+)", line)
        if m and (int(m.group(1)), int(m.group(2))) > (2, 30):
            print("FAIL glibc новее 2.30: " + line)
            bad += 1
    h.run("sudo -S -p '' chown -R $(id -u):$(id -g) %s" % BUILD, sudo=True)
    print("готово, %s/out" % BUILD if not bad else "ошибок: %d" % bad)
    return 1 if bad else 0


def speaker_run(h, ip, script, timeout=120):
    return h.run(speaker_ssh(ip) + " sh -s", timeout=timeout, data=script)


def volprev_pcm():
    """Писк при смене громкости со страницы: 0,12 с, 880 Гц, s16 16 кГц моно."""
    import math
    import struct
    rate = 16000
    n = int(rate * 0.12)
    out = bytearray()
    for i in range(n):
        env = min(1.0, i / 160.0, (n - i) / 400.0)
        out += struct.pack("<h", int(12000 * env * math.sin(2 * math.pi * 880 * i / rate)))
    return bytes(out)


def cmd_speaker(args):
    conf = load_conf(args.conf)
    if not args.yes:
        raise SystemExit("нужен --yes: команда меняет колонку")
    if args.noupdate and not args.src:
        raise SystemExit("--noupdate берёт скрипт с колонки --from")
    ip = conf["speaker"]
    h = open_host()
    ensure_ssh(h)
    code, out = speaker_run(h, ip, (
        "uname -m; ls /lib/libc-2.30.so >/dev/null 2>&1 && echo glibc230; "
        "ls /system/vendor/quasar/libs/libyandex_evil.so >/dev/null 2>&1 && "
        "grep -qE 'station-midi|station-mini2' /proc/device-tree/compatible 2>/dev/null && echo evil; "
        "grep -q station-mini2 /proc/device-tree/compatible 2>/dev/null && echo mini2; "
        "[ -f %s/house.pid ] && kill -0 $(cat %s/house.pid) 2>/dev/null && echo installed; "
        "[ -s %s/soda/libsoda.so ] && echo hassoda; "
        "df -k /data | tail -n 1 | awk '{print \"free\", $4}'\n" % (DIR, DIR, DIR)))
    if code or "evil" not in out or "glibc230" not in out:
        raise SystemExit("колонка не похожа на Станцию с root:\n" + out[-600:])
    mini2 = "mini2" in out.split()
    if "installed" in out and not args.upgrade:
        raise SystemExit("оболочка уже стоит и работает. Повтор только с --upgrade")
    free = int((re.search(r"free (\d+)", out) or [0, "0"])[1])
    soda_from = None
    if not mini2 and "hassoda" not in out.split():
        _c, have = h.run("test -s /storage/yse/soda/libsoda.so && echo yes")
        if "yes" in have:
            soda_from = "сервера"
        elif args.src and "yes" in speaker_run(h, args.src, "[ -s %s/soda/libsoda.so ] && echo yes; true\n" % DIR, timeout=30)[1]:
            soda_from = args.src
    static_ear = mini2 or ("hassoda" not in out.split() and not soda_from)
    if not args.upgrade and free < (140000 if soda_from else 20000):
        raise SystemExit("на /data мало места: %d КБ" % free)
    code, out = h.run("cd %s/out && ls %s" % (BUILD, " ".join(SPEAKER_BIN + (("ear64s",) if static_ear else ()))))
    if code:
        raise SystemExit("сначала build:\n" + out[-600:])

    print("1. Quasar в сторону")
    speaker_run(h, ip, (
        "mount -o remount,rw /\n"
        "if [ -f /etc/init.d/S99quasar ]; then /etc/init.d/S99quasar stop; "
        "mkdir -p %s; mv /etc/init.d/S99quasar %s/S99quasar.saved; fi\n"
        "killall quasar_launcher2 2>/dev/null; sleep 1; killall maind 2>/dev/null; true\n" % (DIR, DIR)))
    if args.noupdate:
        code, out = h.run("%s 'cat /data/opt/yandex-noupdate.sh' | %s 'cat > /data/opt/yandex-noupdate.sh && sh /data/opt/yandex-noupdate.sh'"
                          % (speaker_ssh(args.src), speaker_ssh(ip)), timeout=120)
        print(out[-800:])

    if not args.upgrade:
        if static_ear:
            print("2. распознавание на сервере, soda/ не нужен")
            speaker_run(h, ip, "mkdir -p %s/settings\nprintf 'cloud\\n' > %s/settings/ear.engine\n" % (DIR, DIR))
        elif soda_from:
            source = "tar -C /storage/yse -cf - soda" if soda_from == "сервера" else \
                "%s 'tar -C %s -cf - soda'" % (speaker_ssh(args.src), DIR)
            print("2. распознавание soda/ с %s" % soda_from)
            code, out = h.run(
                "%s | %s 'mkdir -p %s && tar -C %s -xf -'"
                % (source, speaker_ssh(ip), DIR, DIR), timeout=900)
            if code:
                raise SystemExit("soda не скопировалась:\n" + out[-600:])

    print("3. программы")
    for name in SPEAKER_BIN + (("ear64s",) if static_ear else ()):
        code, out = h.run("cat %s/out/%s | %s 'mkdir -p %s/bin && cat > %s/bin/%s.new && chmod 755 %s/bin/%s.new'"
                          % (BUILD, name, speaker_ssh(ip), DIR, DIR, name, DIR, name), timeout=120)
        if code:
            raise SystemExit("%s не лёг:\n%s" % (name, out[-400:]))
    model = conf.get("model", "/storage/yse-mww/trainer/ru_jarvis_mww.tflite")
    code, out = h.run("cat %s | %s 'mkdir -p %s/jarvis && cat > %s/jarvis/ru_jarvis_mww.tflite.new && "
                      "mv %s/jarvis/ru_jarvis_mww.tflite.new %s/jarvis/ru_jarvis_mww.tflite'"
                      % (model, speaker_ssh(ip), DIR, DIR, DIR, DIR), timeout=60)
    if code:
        raise SystemExit("модель слова не легла:\n" + out[-400:])
    h.put_bytes("/tmp/yse-update.sh", (REPO / "init" / "yse-update.sh").read_bytes().replace(b"\r\n", b"\n"))
    code, out = h.run("cat /tmp/yse-update.sh | %s 'cat > %s/bin/yse-update.new && chmod 755 %s/bin/yse-update.new'"
                      % (speaker_ssh(ip), DIR, DIR), timeout=30)
    if code:
        raise SystemExit("опрос обновлений не лёг:\n" + out[-400:])
    for local, remote in (("www/index.html", DIR + "/www/index.html.new"),
                          ("init/S99house", "/etc/init.d/S99house.new"),
                          ("init/S99ystation", "/etc/init.d/S99ystation.new")):
        data = (REPO / local).read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
        speaker_run(h, ip, "mkdir -p %s/www\ncat > %s << 'YSE_EOF'\n%sYSE_EOF\n" % (DIR, remote, data))

    h.put_bytes("/tmp/yse-host-volprev.pcm", volprev_pcm())
    h.run("cat /tmp/yse-host-volprev.pcm | %s 'cat > %s/volprev.pcm'" % (speaker_ssh(ip), DIR), timeout=60)

    print("4. настройки")
    lines = ["S=%s/settings; mkdir -p $S %s/phrases" % (DIR, DIR),
             "printf 'host=%s\\nbase=%d\\n' > $S/host.conf" % (conf["host"], int(conf["base"]))]
    for key, value in dict(DEFAULTS, **{"clock.off": clock_off()}).items():
        value = conf.get("defaults", {}).get(key, value)
        lines.append("[ -s $S/%s ] || printf '%%s' '%s' > $S/%s" % (key, value.replace("'", ""), key))
    lines.append("[ -s %s/token ] || { od -An -tx1 -N8 /dev/urandom | tr -d ' \\n' > %s/token; chmod 600 %s/token; }"
                 % (DIR, DIR, DIR))
    speaker_run(h, ip, "\n".join(lines) + "\n")

    print("5. замена и запуск")
    code, out = speaker_run(h, ip, (
        "mount -o remount,rw /\n"
        "D=%s\n"
        "[ -f /usr/bin/aplay.real ] || cp /usr/bin/aplay /usr/bin/aplay.real\n"
        "for f in ear64 commandd eqd keys alarmd; do\n"
        "  p=$D/$(echo $f | sed 's/ear64/ear/;s/commandd/command/;s/eqd/eq/;s/alarmd/alarm/').pid\n"
        "  [ -f $p ] && kill $(cat $p) 2>/dev/null\n"
        "done\n"
        "[ -f $D/ystationd.pid ] && kill $(cat $D/ystationd.pid) 2>/dev/null; rm -f $D/ystationd.pid\n"
        "sleep 2\n"
        "[ -f $D/ear.pid ] && kill -9 $(cat $D/ear.pid) 2>/dev/null\n"
        "for f in $D/bin/*.new; do b=${f%%.new}; [ -f $b ] && cp $b $b.prev; mv $f $b; done\n"
        "cp $D/bin/aplay48 /usr/bin/aplay.new && mv /usr/bin/aplay.new /usr/bin/aplay\n"
        "mv $D/www/index.html.new $D/www/index.html\n"
        "for s in S99house S99ystation; do mv /etc/init.d/$s.new /etc/init.d/$s; chmod 755 /etc/init.d/$s; done\n"
        "/etc/init.d/S99house start; /etc/init.d/S99ystation start; sleep 2\n"
        "%s"
        "for p in house ear command eq keys alarm ystationd; do\n"
        "  if [ -f $D/$p.pid ] && kill -0 $(cat $D/$p.pid) 2>/dev/null; then echo \"$p ok\"; else echo \"$p НЕТ\"; fi\n"
        "done\n" % (DIR, "sync; mount -o remount,ro /\n" if mini2 else "")), timeout=60)
    print(out)
    print("колонка готова. Страница http://%s:18080, ключ — файл token на колонке" % ip)
    return 0


def env_text(conf):
    rows = {
        "YSE_NAME": conf["name"],
        "YSE_AREA": conf.get("area", ""),
        "YSE_SPEAKER": conf["speaker"],
        "YSE_BASE": str(int(conf["base"])),
        "YSE_SAT_PORT": str(int(conf["sat_port"])),
        "YSE_MODEL": conf.get("model", "/storage/yse-mww/trainer/ru_jarvis_mww.tflite"),
    }
    if ha_url():
        rows["YSE_HA_URL"] = ha_url()
    cutoff = str(conf.get("cutoff", "")).strip()
    if cutoff:
        rows["YSE_CUTOFF"] = cutoff
    play = str(conf.get("play", "")).strip()
    if play:
        rows["YSE_PLAY"] = play
    if conf.get("max_stt"):
        rows["YSE_MAX_STT"] = "1"
    settings_dir = str(conf.get("settings_dir", "")).strip()
    if settings_dir:
        rows["YSE_SETTINGS"] = settings_dir
    return "".join("%s=%s\n" % (k, v.replace("\n", " ")) for k, v in rows.items())


def put_unit(h, name, remote_dir):
    data = (REPO / "install" / "units" / name).read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    user = service_user()
    data = data.replace("User=administrator", "User=" + user).replace("Group=administrator", "Group=" + user)
    h.put_bytes("%s/%s" % (remote_dir, name), data.encode("utf-8"))


def busy_ports(h, conf):
    base = int(conf["base"])
    want = [base, base + 1, base + 3, base + 4, base + 5, int(conf["sat_port"])]
    _code, out = h.run("ss -ltnuH")
    taken = set(int(m) for m in re.findall(r":(\d+)\s", out))
    return [p for p in want if p in taken]


def cmd_host_docker(args):
    """В контейнере службы уже запущены. Здесь только файл колонки, его подхватит yse_dock."""
    conf = load_conf(args.conf)
    store = pathlib.Path("/storage/yse")
    store.mkdir(parents=True, exist_ok=True)
    (store / ("%s.env" % conf["id"])).write_text(env_text(conf), encoding="utf-8")
    print("колонка %s в docker, порты с %s" % (conf["id"], conf["base"]))
    return 0


def cmd_host(args):
    if os.environ.get("YSE_DOCKER") == "1":
        return cmd_host_docker(args)
    conf = load_conf(args.conf)
    if not args.yes:
        raise SystemExit("нужен --yes: команда ставит службы на сервер")
    h = open_host()
    ensure_ssh(h)
    install_root_tree(h)
    busy = busy_ports(h, conf)
    if busy:
        raise SystemExit("порты уже заняты: %s. Возьмите другой base и sat_port" % busy)
    h.mkdir("/tmp/yse-host")
    for name in HOST_SCRIPTS:
        h.put_text(SCRIPTS / name, "/tmp/yse-host/" + name)
    for name in UNITS:
        put_unit(h, name, "/tmp/yse-host")
    h.put_bytes("/tmp/yse-host/%s.env" % conf["id"], env_text(conf).encode("utf-8"))
    user = service_user()
    code, out = h.run("p='env PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install --user -q %s'; "
                      "if [ $(id -u) = 0 ]; then sudo -u %s -H $p; else $p; fi 2>&1 | tail -n 2"
                      % (USER_PIP.replace("'", ""), user))
    print(out.strip() or "библиотеки служб ok")
    _c, out = h.run("mkdir -p /storage/yse/models && cd /storage/yse/models && "
                    "{ test -s silero_vad.onnx || curl -fsSL -o silero_vad.onnx %s; } && "
                    "ls -la silero_vad.onnx" % VAD_URL, timeout=120)
    print(out.strip()[-200:])
    sid = conf["id"]
    h.put_text(SCRIPTS / "station_max_as_payload.sh", "/tmp/yse-host/station_max_as_payload.sh")
    payload_install = "install -o %s -m 644 /tmp/yse-host/station_max_as_payload.sh %s/ && " % (user, APP)
    autoroot_enable = ""
    if str(conf.get("play", "")).strip() == "http":
        autoroot_enable = "systemctl enable --now yse-max-autoroot@%s && " % sid
    code, out = h.run(
        "sudo -S -p '' bash -c '"
        "mkdir -p %s && chown %s:%s /storage/yse %s && "
        "install -o %s -m 644 /tmp/yse-host/*.py %s/ && "
        + payload_install +
        "install -o %s -m 644 /tmp/yse-host/%s.env /storage/yse/%s.env && "
        "install -m 644 /tmp/yse-host/yse-*@.service /etc/systemd/system/ && "
        "systemctl daemon-reload && "
        + autoroot_enable +
        "systemctl enable --now yse-wake@%s yse-music@%s yse-talk@%s yse-sat@%s && "
        "systemctl is-active yse-wake@%s yse-music@%s yse-talk@%s yse-sat@%s'"
        % ((APP, user, user, APP, user, APP, user, sid, sid) + (sid,) * 8), sudo=True, timeout=120)
    print(out)
    _c, demo = h.run("test -s /storage/yse/demo.token && echo yes || echo no")
    if "yes" not in demo:
        print("нет /storage/yse/demo.token: ответы Яндекса без входа человека работать не будут")
    print("в HA: Настройки → Интеграции → Wyoming, хост %s, порт %d" % (h.host, int(conf["sat_port"])))
    return code


def cmd_check(args):
    conf = load_conf(args.conf)
    ip = conf["speaker"]
    h = open_host()
    ensure_ssh(h)
    _code, out = speaker_run(h, ip, (
        "D=%s\n"
        "for p in house ear command eq keys alarm ystationd; do\n"
        "  if [ -f $D/$p.pid ] && kill -0 $(cat $D/$p.pid) 2>/dev/null; then echo \"$p ok\"; else echo \"$p НЕТ\"; fi\n"
        "done\n"
        "echo host.conf: $(cat $D/settings/host.conf 2>/dev/null | tr '\\n' ' ' || echo нет)\n"
        "echo nn: $(cat $D/settings/nn.txt 2>/dev/null)\n"
        "[ -f /usr/bin/aplay.real ] && cmp -s /usr/bin/aplay $D/bin/aplay48 && echo 'aplay обёртка ok' || echo 'aplay НЕ обёртка'\n"
        "[ -x $D/bin/ringcat ] && echo 'ringcat ok' || echo 'ringcat нет'\n"
        "ps | grep -E 'quasar|maind' | grep -v grep >/dev/null && echo 'Quasar РАБОТАЕТ' || echo 'Quasar выключен'\n"
        % DIR), timeout=30)
    print("колонка %s" % ip)
    print(out)
    sid = conf["id"]
    base = int(conf["base"])
    _code, out = h.run(
        "for u in yse-wake@%s yse-music@%s yse-talk@%s yse-sat@%s yse-wake-listen yse-music yse-talk yse-sat; do "
        "s=$(systemctl is-active $u 2>/dev/null); [ \"$s\" = inactive ] || echo \"$u $s\"; done; "
        "ss -ltnuH | grep -E ':(%d|%d|%d|%d|%d)\\s' | awk '{print $1, $5}'; "
        "curl -s -m 3 http://127.0.0.1:%d/v1/state || echo 'спутник HA не отвечает'"
        % (sid, sid, sid, sid, base, base + 1, base + 3, base + 4, int(conf["sat_port"]), base + 4),
        timeout=30)
    print("сервер")
    print(out)
    return 0


def cmd_key(args):
    ip = args.ip or (load_conf(args.conf)["speaker"] if args.conf else "")
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
        raise SystemExit("нужен --conf или --ip колонки")
    h = open_host()
    ensure_ssh(h)
    code, out = speaker_run(h, ip, "cat %s/token\n" % DIR, timeout=20)
    key = out.strip()
    if code or not re.fullmatch(r"[0-9a-fA-F]{8,64}", key):
        raise SystemExit("ключа на колонке нет: сначала speaker")
    print(key)
    return 0


def pkg_tar():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for part in PKG_PARTS:
            for path in sorted((REPO / part).rglob("*")):
                if path.name == "site.json":
                    continue
                if path.is_file() and not any(p.startswith(".") or p == "__pycache__" for p in path.parts):
                    data = path.read_bytes()
                    if path.suffix in (".py", ".c", ".h", ".inc", ".html", ".js", ".json", ".service", ".sh", ""):
                        data = data.replace(b"\r\n", b"\n")
                    info = tarfile.TarInfo(path.relative_to(REPO).as_posix())
                    info.size = len(data)
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def install_root_tree(h):
    """Кладёт исходники reader4 из этого репозитория в /storage/yse/s905x2-root."""
    root = REPO / "s905x2-root"
    if not (root / "src" / "reader4.c").is_file():
        print("нет s905x2-root рядом с установщиком")
        return
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def skip(info):
            name = info.name.replace("\\", "/")
            if "/.git" in name or name.endswith(".pyc") or "/__pycache__/" in name:
                return None
            return info
        tar.add(root, arcname="s905x2-root", filter=skip)
    h.put_bytes("/tmp/yse-s905x2-root.tgz", buf.getvalue())
    _code, out = h.run(
        "sudo -S -p '' bash -c 'mkdir -p /storage/yse && tar -C /storage/yse -xzf /tmp/yse-s905x2-root.tgz "
        "&& rm -f /tmp/yse-s905x2-root.tgz && test -s /storage/yse/s905x2-root/src/reader4.c && echo root-tree-ok'",
        sudo=True, timeout=60)
    print((out or "").strip()[-200:])
    install_dropbear(h)
    ensure_ndk(h)


def install_dropbear(h):
    """Кладёт dropbear для Макса в /storage/yse/dropbear. Бинарники лежат в этом репозитории."""
    src = REPO / "host" / "dropbear"
    if not (src / "dropbear").is_file() or not (src / "dropbearkey").is_file():
        print("нет speaker/host/dropbear")
        return
    h.put_bytes("/tmp/yse-dropbear", (src / "dropbear").read_bytes())
    h.put_bytes("/tmp/yse-dropbearkey", (src / "dropbearkey").read_bytes())
    _code, out = h.run(
        "sudo -S -p '' bash -c 'mkdir -p /storage/yse/dropbear && "
        "install -m 755 /tmp/yse-dropbear /storage/yse/dropbear/dropbear && "
        "install -m 755 /tmp/yse-dropbearkey /storage/yse/dropbear/dropbearkey && "
        "rm -f /tmp/yse-dropbear /tmp/yse-dropbearkey && echo dropbear-ok'",
        sudo=True, timeout=30)
    print((out or "").strip()[-200:])


def ensure_ndk(h):
    """NDK r26d нужен, чтобы собрать reader4. Если его нет, качается официальный архив."""
    _code, out = h.run(
        "if [ -x /storage/yse/android-ndk-r26d/ndk-build ] || "
        "[ -x /home/administrator/android-ndk-r26d/ndk-build ]; then echo ndk-ok; else echo ndk-no; fi")
    if "ndk-ok" in (out or ""):
        print("ndk ok")
        return
    print("качаю Android NDK r26d")
    _code, out = h.run(
        "sudo -S -p '' bash -c 'mkdir -p /storage/yse && cd /tmp && "
        "curl -fL --retry 3 -o android-ndk-r26d-linux.zip "
        "https://dl.google.com/android/repository/android-ndk-r26d-linux.zip && "
        "unzip -q -o android-ndk-r26d-linux.zip -d /storage/yse && "
        "rm -f android-ndk-r26d-linux.zip && "
        "test -x /storage/yse/android-ndk-r26d/ndk-build && echo ndk-ok'",
        sudo=True, timeout=1800)
    print((out or "").strip()[-200:])


def install_google(h):
    """Чат Google «Режим ИИ». Профиль входа не стирает. Войти один раз по VNC."""
    user = service_user()
    h.mkdir("/tmp/yse-host")
    put_unit(h, "yse-google-ai.service", "/tmp/yse-host")
    _code, out = h.run(
        "sudo -S -p '' bash -c '"
        "export DEBIAN_FRONTEND=noninteractive && apt-get update -qq && "
        "apt-get install -y -qq xvfb x11vnc python3-venv && "
        "mkdir -p /storage/yse/google-ai && chown -R %s:%s /storage/yse/google-ai && "
        "sudo -u %s python3 -m venv /storage/yse/google-ai/venv && "
        "sudo -u %s /storage/yse/google-ai/venv/bin/pip install -q playwright && "
        "sudo -u %s /storage/yse/google-ai/venv/bin/playwright install chromium && "
        "/storage/yse/google-ai/venv/bin/playwright install-deps chromium && "
        "install -m 644 /tmp/yse-host/yse-google-ai.service /etc/systemd/system/yse-google-ai.service && "
        "systemctl daemon-reload && systemctl enable --now yse-google-ai && "
        "systemctl is-active yse-google-ai'"
        % (user, user, user, user, user),
        sudo=True, timeout=1800)
    print((out or "").strip()[-400:])


def cmd_server(args):
    if not args.yes:
        raise SystemExit("нужен --yes: команда ставит сторожа на сервер")
    h = open_host()
    ensure_ssh(h)
    install_root_tree(h)
    h.mkdir("/tmp/yse-host")
    h.put_bytes("/tmp/yse-host/pkg.tgz", pkg_tar())
    put_unit(h, "yse-discover.service", "/tmp/yse-host")
    put_unit(h, "yse-train.service", "/tmp/yse-host")
    ha = args.ha if args.ha is not None else ha_ip()
    skip = args.skip if args.skip is not None else skip_list()
    src = args.src if args.src is not None else site_get("ref_speaker", "YSE_REF_SPEAKER")
    if not ha:
        print("адрес HA не задан (--ha или ha.url в site.json): установка кнопкой из HA не заработает")
    rows = [("YSE_HOST_IP", h.host), ("YSE_HA_IP", ha), ("YSE_SKIP", skip), ("YSE_REF_SPEAKER", src),
            ("YSE_USER", service_user()), ("YSE_CLOCK_OFF", clock_off())]
    url = ha_url() or ("http://%s:8123" % ha if ha else "")
    if url:
        rows.append(("YSE_HA_URL", url))
    net = site_get("network", "YSE_NET")
    if net:
        rows.append(("YSE_NET", net))
    env = "".join("%s=%s\n" % row for row in rows)
    h.put_bytes("/tmp/yse-host/discover.env", env.encode("utf-8"))
    code, out = h.run(
        "sudo -S -p '' bash -c '"
        "mkdir -p /storage/yse && rm -rf /storage/yse/pkg.new && mkdir -p /storage/yse/pkg.new && "
        "tar -C /storage/yse/pkg.new -xzf /tmp/yse-host/pkg.tgz && rm -rf /storage/yse/pkg && "
        "mv /storage/yse/pkg.new /storage/yse/pkg && "
        "install -m 600 /tmp/yse-host/discover.env /storage/yse/discover.env && "
        "install -m 644 /tmp/yse-host/yse-discover.service /tmp/yse-host/yse-train.service /etc/systemd/system/ && "
        "systemctl daemon-reload && systemctl enable yse-discover yse-train && systemctl restart yse-discover && "
        "sleep 5 && systemctl restart yse-train && systemctl is-active yse-discover yse-train'", sudo=True, timeout=120)
    print(out)
    install_google(h)
    print("сторож: http://%s:%d/v1/found — HA сам покажет новые колонки" % (h.host, DISCOVER_PORT))
    return code


STT_DIR = "/storage/yse/stt"
STT_FILES = ("docker-compose.yml", "stt_server.py", "whisper.Dockerfile", "vosk.Dockerfile", "gigaam.Dockerfile")
VOSK_URL = "https://alphacephei.com/vosk/models/%s.zip"
VAD_URL = "https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx"
USER_PIP = "wyoming==1.5.4 vosk==0.3.45 'numpy<3' 'onnxruntime>=1.17' 'websockets>=12'"
MUSIC_PIP = "yandex-music==3.2.2"
TRAINER = "/storage/yse-mww/trainer"
TRAINER_GIT = "https://github.com/interkelstar/microwakeword-trainer.git"
TRAINER_PIP = ("'microwakeword @ git+https://github.com/kahrendt/microWakeWord.git@"
               "a70bd740d4e79ee8a8bb3db843fe862b88d5d6b0' pymicro-features==2.0.2 pyyaml scipy tqdm "
               "audiomentations datasets")
VOSK_SMALL = "vosk-model-small-ru-0.22"
WAKE_MODEL = TRAINER + "/ru_jarvis_mww.tflite"
APT = ("python3-venv python3-pip python3-dev build-essential ffmpeg libva2 "
       "gcc-aarch64-linux-gnu libc6-dev-arm64-cross curl git unzip openssh-client")
PREPARE = r"""
set -e
U=%(user)s
step() { echo "== $*"; }
as_user() { sudo -u "$U" -H "$@"; }
step пакеты
export DEBIAN_FRONTEND=noninteractive
need=
for p in %(apt)s; do dpkg -s $p >/dev/null 2>&1 || need="$need $p"; done
command -v docker >/dev/null || need="$need docker.io docker-compose-v2"
if [ -n "$need" ]; then
  apt-get update -qq 2>&1 | grep -v '^W:' || true
  apt-get install -y -qq --no-install-recommends $need >/dev/null
fi
echo "поставлено:${need:- ничего нового}"
docker compose version >/dev/null 2>&1 || echo "ВНИМАНИЕ: нет docker compose, stt и voice не встанут"
step каталоги
mkdir -p /storage/yse/app /storage/yse/bin /storage/yse/models /storage/yse/soda /storage/yse/build /storage/yse-music /storage/yse-mww
chown "$U:$U" /storage/yse /storage/yse/app /storage/yse/bin /storage/yse/models /storage/yse-music /storage/yse-mww
step библиотеки служб
as_user env PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install --user -q %(user_pip)s 2>&1 | grep -v -i 'warning' | tail -n 3 || true
step музыка
[ -x /storage/yse-music/.venv/bin/python ] || as_user python3 -m venv /storage/yse-music/.venv
as_user /storage/yse-music/.venv/bin/pip install -q %(music_pip)s
step слово
T=%(trainer)s
[ -e "$T/train_mww.py" ] || as_user git clone -q %(trainer_git)s "$T"
[ -x "$T/.venv/bin/python" ] || as_user python3 -m venv "$T/.venv"
if ! as_user "$T/.venv/bin/python" -c 'import tensorflow, pymicro_features' >/dev/null 2>&1; then
  echo "TensorFlow и microWakeWord: 10-20 минут"
  as_user "$T/.venv/bin/pip" install -q -U pip setuptools wheel
  as_user "$T/.venv/bin/pip" install -q %(trainer_pip)s
fi
step распознавание
V=/storage/yse/%(vosk)s
if [ ! -d "$V" ]; then
  cd /storage/yse && curl -fsSL -o vosk-small.zip https://alphacephei.com/vosk/models/%(vosk)s.zip
  python3 -m zipfile -e vosk-small.zip . && rm -f vosk-small.zip && chown -R "$U:$U" "$V"
fi
[ -s /storage/yse/models/silero_vad.onnx ] || curl -fsSL -o /storage/yse/models/silero_vad.onnx %(vad)s
step образ сборки
docker image inspect spotter-tap-build:latest >/dev/null 2>&1 || docker build -q -t spotter-tap-build:latest /tmp/yse-prep >/dev/null
echo готово
"""


def cmd_stt(args):
    if not args.yes:
        raise SystemExit("нужен --yes: команда ставит контейнеры распознавания на сервер")
    h = open_host()
    h.mkdir("/tmp/yse-stt")
    for name in STT_FILES:
        h.put_text(REPO / "host" / "stt" / name, "/tmp/yse-stt/" + name)
    env = "VOSK_MODEL=%s\nWHISPER_MODEL=%s\nVOSK_MEM=%s\n" % (args.vosk, args.whisper, args.vosk_mem)
    if args.hf:
        env += "HF_ENDPOINT=%s\n" % args.hf
    h.put_bytes("/tmp/yse-stt/.env", env.encode("utf-8"))
    print("1. файлы; модель Vosk %s качается в фоне (журнал %s/models/vosk/download.log)" % (args.vosk, STT_DIR))
    fetch = ("cd %s/models/vosk && curl -fsSL -C - -o %s.zip %s && python3 -m zipfile -e %s.zip . && rm -f %s.zip"
             % (STT_DIR, args.vosk, VOSK_URL % args.vosk, args.vosk, args.vosk))
    code, out = h.run(
        "sudo -S -p '' bash -c '"
        "mkdir -p %s/models/vosk %s/models/whisper %s/models/gigaam && cp /tmp/yse-stt/* /tmp/yse-stt/.env %s/ && "
        "if [ -d %s/models/vosk/%s ]; then echo vosk_ready; "
        "elif pgrep -f \"curl.*%s.zip\" >/dev/null; then echo vosk_downloading; "
        "else nohup flock -n %s/models/vosk/.lock sh -c \"%s\" > %s/models/vosk/download.log 2>&1 & echo vosk_started; fi'"
        % (STT_DIR, STT_DIR, STT_DIR, STT_DIR, STT_DIR, args.vosk, args.vosk, STT_DIR, fetch, STT_DIR),
        sudo=True, timeout=60)
    print(out.strip()[-300:])
    services = "whisper gigaam"
    if "vosk_ready" in out and args.with_vosk:
        _c, avail = h.run("awk '/MemAvailable/ {print int($2/1048576)}' /proc/meminfo")
        need = int(args.vosk_mem.rstrip("gG")) + 1
        if int(avail.strip() or 0) >= need:
            services = "whisper gigaam vosk"
        else:
            print("Vosk не запущен: свободно %s ГБ, нужно %d" % (avail.strip(), need))
    print("2. сборка и запуск: %s" % services)
    code, out = h.run("sudo -S -p '' bash -c 'cd %s && docker compose build %s 2>&1 | tail -n 8 && "
                      "docker compose up -d %s 2>&1 | tail -n 8'" % (STT_DIR, services, services),
                      sudo=True, timeout=3600)
    print(out[-1500:])
    print("3. проверка (первый запуск Whisper качает модель)")
    ports = "18950 18953 18951" if "vosk" in services else "18950 18953"
    _c, out = h.run("for p in %s; do r=''; for i in $(seq 1 40); do "
                    "r=$(curl -s -m 3 http://127.0.0.1:$p/health) && [ -n \"$r\" ] && break; sleep 5; done; "
                    "echo $p ${r:-нет ответа}; done" % ports, timeout=600)
    print(out)
    if services == "whisper":
        print("Vosk поднимается только с --with-vosk, когда модель докачалась и памяти хватает")
    return code


VOICE_DIR = "/storage/yse/voice"
VOICE_MODELS = (("v5_6_ru.pt", "https://models.silero.ai/models/tts/ru/v5_6_ru.pt"),
                ("voxceleb_resnet34_LM.onnx", "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM"
                                              "/resolve/main/voxceleb_resnet34_LM.onnx"))
TORCH_WHL = "torch-2.1.2+cu121-cp310-cp310-linux_x86_64.whl"
VOICE_BUILD = ("apt-get update -qq && apt-get install -y -qq --no-install-recommends build-essential git curl >/dev/null && "
               "if [ ! -x /voice/venv/bin/python ]; then "
               "mkdir -p /voice/cache/wheels && cd /voice/cache/wheels && "
               "for i in $(seq 1 60); do curl -fsSL -C - --speed-limit 1000000 --speed-time 20 -o %s "
               "https://download.pytorch.org/whl/cu121/%s && break; sleep 2; done && "
               "python -m venv /voice/venv; fi && "
               "/voice/venv/bin/pip install -q -U \\\"pip<24.1\\\" wheel setuptools==69.5.1 && "
               "/voice/venv/bin/pip install -q --cache-dir /voice/cache/pip --find-links /voice/cache/wheels "
               "-r /voice/app/requirements.txt && "
               "cp /voice/app/requirements.txt /voice/venv/requirements.ok && echo BUILD-OK"
               % (TORCH_WHL, TORCH_WHL.replace("+", "%%2B")))


def bundled_models():
    """Каталог models/ рядом с репозиторием: слово и голоса, которые едут вместе с кодом."""
    for root in (HERE.parent.parent, HERE.parent):
        if (root / "models" / "wake").is_dir() or (root / "models" / "voice").is_dir():
            return root / "models"
    return HERE.parent.parent / "models"


def cmd_voice(args):
    if not args.yes:
        raise SystemExit("нужен --yes: команда ставит контейнер голоса на сервер")
    h = open_host()
    h.mkdir("/tmp/yse-voice")
    for name in ("voice_server.py", "requirements.txt", "docker-compose.yml"):
        h.put_text(REPO / "host" / "voice" / name, "/tmp/yse-voice/" + name)
    packed = ""
    voice_root = bundled_models() / "voice"
    for name in ("jarvis", "vihrov"):
        src = voice_root / name / ("%s.pth" % name)
        if not src.is_file():
            continue
        h.put_bytes("/tmp/yse-voice/%s.pth" % name, src.read_bytes())
        packed += "mkdir -p rvc/{0} && cp -f /tmp/yse-voice/{0}.pth rvc/{0}/{0}.pth; ".format(name)
    models = " ".join("test -s rvc/{0}/{0}.pth || {{ test -s {1}/{0}/{0}.pth && mkdir -p rvc/{0} && "
                      "cp {1}/{0}/{0}.pth rvc/{0}/; }};".format(n, args.rvc_from) for n in ("jarvis", "vihrov"))
    fetch = " ".join("{ test -s models/%s || for i in $(seq 1 30); do curl -fsSL -C - --speed-limit 300000 "
                     "--speed-time 15 -o models/%s %s && break; sleep 2; done; };" % (name, name, url)
                     for name, url in VOICE_MODELS)
    print("1. файлы, Silero v5_6, отпечатки голоса WeSpeaker, модели RVC из %s"
          % ("репозитория" if packed else args.rvc_from))
    code, out = h.run(
        "sudo -S -p '' bash -c '"
        "mkdir -p %s/app %s/models %s/rvc %s/cache/tmp && cd %s && "
        "cp /tmp/yse-voice/voice_server.py /tmp/yse-voice/requirements.txt app/ && "
        "cp /tmp/yse-voice/docker-compose.yml . && %s %s %s ls models rvc'"
        % ((VOICE_DIR,) * 5 + (packed, fetch, models)), sudo=True, timeout=900)
    print(out.strip()[-300:])
    print("2. библиотеки (torch и RVC, ~5 ГБ в %s/venv; первый раз 10–30 минут, потом только новые)" % VOICE_DIR)
    code, out = h.run(
        "sudo -S -p '' bash -c 'cd %s && if cmp -s app/requirements.txt venv/requirements.ok; then echo venv_ready; else "
        "docker run --rm -e TMPDIR=/voice/cache/tmp -v %s:/voice python:3.10-slim "
        "sh -c \"%s\" 2>&1 | tail -n 5; fi'" % (VOICE_DIR, VOICE_DIR, VOICE_BUILD), sudo=True, timeout=7200)
    print(out.strip()[-600:])
    base = VOICE_DIR + "/venv/lib/python3.10/site-packages/rvc_python/base_model"
    h.run("sudo -S -p '' bash -c 'mkdir -p %s && for f in hubert/hubert_base.pt rmvpe/rmvpe.pt; do "
          "test -s %s/assets/$f && cp -n %s/assets/$f %s/; done; true'" % (base, args.rvc_from, args.rvc_from, base),
          sudo=True, timeout=120)
    print("3. запуск yse-voice (127.0.0.1:18952)")
    code, out = h.run("sudo -S -p '' bash -c 'cd %s && docker compose up -d --force-recreate 2>&1 | tail -n 3'"
                      % VOICE_DIR, sudo=True, timeout=300)
    print(out.strip())
    _c, out = h.run("r=''; for i in $(seq 1 60); do r=$(curl -s -m 3 http://127.0.0.1:18952/health) && "
                    "echo \"$r\" | grep -q true && break; sleep 5; done; echo ${r:-нет ответа}", timeout=400)
    print(out.strip())
    print("голос на странице колонки: «Джарвис (свой сервер)» и др.; при сбое ответит голос Яндекса")
    return code


def ask(prompt, default=""):
    shown = " [%s]" % default if default not in ("", None) else ""
    value = input("%s%s: " % (prompt, shown)).strip()
    return value or default


def cmd_init(_args):
    data = json.loads(SITE_FILE.read_text(encoding="utf-8")) if SITE_FILE.is_file() else {}
    srv, ha = data.setdefault("server", {}), data.setdefault("ha", {})
    print("Ответ Enter оставляет значение в скобках. Пароли можно не вводить: спросит при запуске.")
    srv["ip"] = ask("IP домашнего сервера (Ubuntu 22.04)", srv.get("ip", ""))
    srv["user"] = ask("пользователь SSH на сервере (с sudo)", srv.get("user", ""))
    srv["key_file"] = ask("SSH-ключ к серверу, путь (пусто — пароль)", srv.get("key_file", ""))
    data["service_user"] = ask("от кого работают службы", data.get("service_user") or srv["user"])
    net = data.get("network") or (srv["ip"].rsplit(".", 1)[0] + ".0/24" if srv["ip"] else "")
    data["network"] = ask("подсеть колонок", net)
    ha["url"] = ask("адрес Home Assistant", ha.get("url", ""))
    host = re.search(r"//([^:/]+)", ha["url"] or "")
    ha["ssh"] = ask("SSH дополнения HA (Advanced SSH), хост; пусто — без автоустановки компонента",
                    ha.get("ssh") or (host.group(1) if host else ""))
    if ha["ssh"]:
        ha["ssh_port"] = int(ask("порт SSH дополнения", ha.get("ssh_port", 22)))
        ha["login"] = ask("логин SSH дополнения", ha.get("login", "root"))
    data["ref_speaker"] = ask("IP чужой колонки с soda (Enter — распознавание на этом компьютере)",
                              data.get("ref_speaker", ""))
    data["soda_tar"] = ask("архив soda.tar (Enter — его нет, так и нужно)", data.get("soda_tar", ""))
    data["clock_offset"] = int(ask("часовой пояс, часов от UTC", data.get("clock_offset", 3)))
    data["wake_model"] = ask("своя модель слова, путь или URL (Enter — «джарвис» из репозитория)",
                             data.get("wake_model", ""))
    data["yandex_token_file"] = ask("файл токена Яндекса (Enter — свой голос; голос Яндекса после входа на колонке)",
                                    data.get("yandex_token_file", ""))
    skip = data.get("skip") or []
    skip = ask("колонки, которые не трогать, через запятую", ",".join(skip))
    data["skip"] = [s.strip() for s in skip.split(",") if s.strip()]
    data.setdefault("rvc_from", "/storage/rvc")
    SITE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("записано %s. Дальше: python install/yse_station_setup.py all --yes" % SITE_FILE)
    return 0


def upload_file(h, src, remote, mode, owner):
    """src — путь на этой машине или URL; кладёт на сервер, если там ещё пусто."""
    code, _ = h.run("test -s '%s'" % remote)
    if code == 0:
        return "уже есть"
    if re.match(r"https?://", src):
        tmp = "/tmp/yse-prep/upload"
        code, out = h.run("curl -fsSL -o %s '%s'" % (tmp, src), timeout=900)
        if code:
            return "не скачался: " + out[-200:]
    else:
        path = site_path(src)
        if not path.is_file():
            return "нет файла " + str(path)
        tmp = "/tmp/yse-prep/upload"
        h.put_bytes(tmp, path.read_bytes())
    code, out = h.run("sudo -S -p '' install -D -o %s -g %s -m %s %s '%s' && rm -f %s"
                      % (owner, owner, mode, tmp, remote, tmp), sudo=True)
    return "ok" if code == 0 else out[-200:]


def cmd_prepare(args):
    if not args.yes:
        raise SystemExit("нужен --yes: команда ставит пакеты и окружения на сервер")
    h = open_host()
    user = service_user()
    net = site_get("network", "YSE_NET") or h.host.rsplit(".", 1)[0] + ".0/24"
    h.mkdir("/tmp/yse-prep")
    h.put_text(REPO / "Dockerfile", "/tmp/yse-prep/Dockerfile")
    script = PREPARE % {"user": user, "apt": APT, "user_pip": USER_PIP, "music_pip": MUSIC_PIP,
                        "trainer": TRAINER, "trainer_git": TRAINER_GIT, "trainer_pip": TRAINER_PIP,
                        "vosk": VOSK_SMALL, "vad": VAD_URL, "net": net}
    h.put_bytes("/tmp/yse-prep/prepare.sh", script.encode("utf-8"))
    print("подготовка сервера %s (пользователь служб %s, подсеть %s)" % (h.host, user, net))
    code, out = h.run("sudo -S -p '' bash /tmp/yse-prep/prepare.sh 2>&1", sudo=True, timeout=7200)
    print(out.strip()[-3000:])
    if code:
        raise SystemExit("подготовка остановилась, код %d" % code)
    ensure_ssh(h)

    print("== модель слова")
    model = site_get("wake_model", "YSE_WAKE_MODEL")
    if not model:
        bundled = bundled_models() / "wake" / "ru_jarvis_mww.tflite"
        if bundled.is_file():
            model = str(bundled)
    if model:
        print(upload_file(h, model, WAKE_MODEL, "644", user))
    else:
        _c, out = h.run("test -s %s && echo есть || echo 'нет модели слова'" % WAKE_MODEL)
        print(out.strip())

    print("== ключ голоса Яндекса")
    token = site_get("yandex_token_file", "YSE_TOKEN_FILE")
    if token:
        print(upload_file(h, token, "/storage/yse/demo.token", "600", user))
    else:
        _c, out = h.run("test -s /storage/yse/demo.token && echo есть || echo 'нет: голос Яндекса только после входа на колонке'")
        print(out.strip())

    print("== soda/ для уха")
    _c, out = h.run("test -s /storage/yse/soda/libsoda.so && echo есть")
    if "есть" in out:
        print("уже есть")
    elif site_get("soda_tar"):
        path = site_path(site_get("soda_tar"))
        h.put_bytes("/tmp/yse-prep/soda.tar", path.read_bytes())
        code, out = h.run("sudo -S -p '' tar -C /storage/yse -xf /tmp/yse-prep/soda.tar && rm -f /tmp/yse-prep/soda.tar",
                          sudo=True, timeout=600)
        print("ok" if code == 0 else out[-300:])
    elif site_get("ref_speaker"):
        ref = site_get("ref_speaker")
        code, out = h.run("%s 'tar -C %s -cf - soda' | sudo -S -p '' tar -C /storage/yse -xf -"
                          % (speaker_ssh(ref), DIR), sudo=True, timeout=900)
        print("скопирована с %s" % ref if code == 0 else "не скопировалась: " + out[-300:])
    else:
        print("нет soda — фраза идёт на распознавание на этом компьютере. Для новой колонки так и нужно")
    return 0


def cmd_all(args):
    if not args.yes:
        raise SystemExit("нужен --yes")
    steps = [("prepare", cmd_prepare), ("build", cmd_build), ("server", cmd_server),
             ("stt", cmd_stt), ("voice", cmd_voice)]
    for name, fn in steps:
        print("\n######## %s" % name)
        code = fn(args)
        if code:
            raise SystemExit("шаг %s не прошёл" % name)
    if site_get("ha.ssh") or site_get("ha.req"):
        print("\n######## компонент Home Assistant")
        sys.path.insert(0, str(HERE))
        import yse_ha_link
        yse_ha_link.cmd_component(args)
    else:
        print("\nкомпонент HA не ставился: в site.json нет ha.ssh. Вручную — docs/install.md, «Home Assistant»")
    print("\nГотово. Колонки с root появятся в HA как «Обнаружено» в течение минуты.")
    return 0


def column_kind(model):
    key = re.sub(r"[\s_-]+", "", (model or "").strip().lower())
    return {
        "max": "max", "yandexstation2": "max", "stationmax": "max",
        "station2": "station2", "yandexmidi": "station2", "midi": "station2",
        "mini2": "mini2", "yandexmini2": "mini2",
    }.get(key)


def column_slots(h):
    code, out = h.run("""python3 - <<'PY'
import pathlib, re
bases, sats = {18765}, {10700}
root = pathlib.Path("/storage/yse")
if root.is_dir():
    for env in root.glob("*.env"):
        text = env.read_text(encoding="utf-8", errors="replace")
        for key, bag in (("YSE_BASE", bases), ("YSE_SAT_PORT", sats)):
            m = re.search("^%s=(\\\\d+)" % key, text, re.M)
            if m:
                bag.add(int(m.group(1)))
print("%d %d" % (max(bases) + 10, max(sats) + 1))
PY""", timeout=20)
    if code != 0:
        raise SystemExit("не прочитал занятые порты:\\n" + out[-400:])
    parts = out.strip().split()
    if len(parts) < 2:
        raise SystemExit("порты: " + out.strip()[-200:])
    return int(parts[-2]), int(parts[-1])


def column_max(args):
    print("Макс %s." % args.ip)
    print("Ставлю APK, отзываю микрофон у com.yandex.io.sdk, выключаю com.yandex.tv.alice.")
    print("Прошивку не меняю. Псевдорут не запускаю: для установки APK он не нужен.")
    if not args.yes:
        print("Повторите с --yes и --apk, когда на колонке открыт ADB :5555.")
        return 0
    apk = pathlib.Path(args.apk) if args.apk else None
    if apk is None or not apk.is_file():
        raise SystemExit("нужен --apk: сборка station-max с HOST и BASE этого сервера")
    h = open_host()
    try:
        remote = "/tmp/yse-column-max.apk"
        h.put_bytes(remote, apk.read_bytes())
        script = """set +e
adb connect %(ip)s:5555
adb -s %(ip)s:5555 install -r -d -g %(apk)s || exit 20
adb -s %(ip)s:5555 shell cmd appops set com.yandex.io.sdk RECORD_AUDIO deny
adb -s %(ip)s:5555 shell pm revoke com.yandex.io.sdk android.permission.RECORD_AUDIO
adb -s %(ip)s:5555 shell pm disable-user --user 0 com.yandex.tv.alice
adb -s %(ip)s:5555 shell appops set com.yse.station WRITE_SETTINGS allow
adb -s %(ip)s:5555 shell pm grant com.yse.station android.permission.READ_LOGS
adb -s %(ip)s:5555 shell pm grant com.yse.station android.permission.ACCESS_FINE_LOCATION
adb -s %(ip)s:5555 shell pm grant com.yse.station android.permission.ACCESS_COARSE_LOCATION
adb -s %(ip)s:5555 shell settings put secure assistant com.yse.station/com.yse.station.VoiceAssist
adb -s %(ip)s:5555 shell settings put secure voice_interaction_service com.yse.station/com.yse.station.VoiceAssist
adb -s %(ip)s:5555 shell am start-foreground-service -n com.yse.station/.Web
sleep 4
curl -s -m 4 http://%(ip)s:18080/api/status || echo PANEL_DOWN
echo
""" % {"ip": args.ip, "apk": remote}
        code, out = h.run(script, timeout=120)
        print(out[-1500:])
        if code != 0 or "PANEL_DOWN" in out:
            raise SystemExit("Макс не поднял страницу. Если ADB :5555 закрыт, на заводском Максе он сам не откроется.")
    finally:
        h.cli.close()
    print("Страница: http://%s:18080/" % args.ip)
    return 0


def column_linux(args, kind):
    label = "Станция 2" if kind == "station2" else "Мини 2"
    print("%s %s." % (label, args.ip))
    print("DNS колонки — IP стенда, затем сброс. Стенд закроет обновления и откроет SSH :2222.")
    if not args.yes:
        print("Когда SSH есть, повторите с --yes: поставлю оболочку и службы. Уже стоящую колонку не трогаю.")
        return 0
    ref = site_get("ref_speaker", "YSE_REF_SPEAKER")
    if not ref:
        raise SystemExit("в site.json нет ref_speaker: с неё берётся скрипт запрета обновлений")
    h = open_host()
    try:
        code, out = h.run("timeout 8 /storage/yse/bin/yse-ssh %s true" % args.ip, timeout=20)
        if code != 0:
            print("SSH :2222 ещё нет. После «обновления закрыты» в журнале стенда повторите column --yes.")
            return 0
        code, out = speaker_run(
            h, args.ip,
            "[ -f /data/opt/yandex-station/house.pid ] && "
            "kill -0 $(cat /data/opt/yandex-station/house.pid) 2>/dev/null && echo installed; true\n",
            timeout=20)
        if "installed" in out.split():
            raise SystemExit("оболочка уже стоит. Программы обновляет speaker --upgrade, не column.")
        base, sat = column_slots(h)
        host = h.host
    finally:
        h.cli.close()
    tail = args.ip.rsplit(".", 1)[1]
    conf = {
        "id": "st" + tail, "name": "Колонка " + tail, "area": "",
        "speaker": args.ip, "host": host, "base": base, "sat_port": sat,
    }
    if kind == "mini2":
        conf["cutoff"] = "0.85"
        conf["defaults"] = {
            "wake.txt": "джарвис",
            "vqe.txt": "yandexmini_2",
            "capture.txt": "hw:0,1 8",
            "spotter.txt": "Yandex yandexmini_2",
        }
    folder = REPO / "install" / "speakers"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("st%s.json" % tail)
    path.write_text(json.dumps(conf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ns = argparse.Namespace(conf=str(path), yes=True, src=ref, upgrade=False, noupdate=True)
    cmd_speaker(ns)
    cmd_host(argparse.Namespace(conf=str(path), yes=True))
    cmd_key(argparse.Namespace(conf=str(path), ip=None))
    print("Готово. Имя и комната — в Home Assistant. Страница: http://%s:18080/" % args.ip)
    return 0


def cmd_column(args):
    kind = column_kind(args.model)
    if kind is None:
        raise SystemExit("модель: max, station2 или mini2")
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", args.ip):
        raise SystemExit("ip: нужен IPv4")
    if kind == "max":
        return column_max(args)
    return column_linux(args, kind)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    sub.add_parser("init")
    sub.add_parser("prepare").add_argument("--yes", action="store_true")

    def server_args(p):
        p.add_argument("--ha", default=None, help="IP HA, по умолчанию из site.json")
        p.add_argument("--skip", default=None, help="IP через запятую, по умолчанию из site.json")
        p.add_argument("--from", dest="src", default=None, help="колонка-образец, по умолчанию ref_speaker")

    def stt_args(p):
        p.add_argument("--vosk", default="vosk-model-ru-0.42")
        p.add_argument("--whisper", default="large-v3-turbo")
        p.add_argument("--vosk-mem", default="6g")
        p.add_argument("--with-vosk", action="store_true")
        p.add_argument("--hf", default="")

    def voice_args(p):
        p.add_argument("--rvc-from", default=site_get("rvc_from", default="/storage/rvc"))

    p = sub.add_parser("all")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--stt", action="store_true", help="распознавание входит в all и без этого флага")
    p.add_argument("--voice", action="store_true", help="свой голос входит в all и без этого флага")
    server_args(p)
    stt_args(p)
    voice_args(p)
    p = sub.add_parser("server")
    p.add_argument("--yes", action="store_true")
    server_args(p)
    p = sub.add_parser("stt")
    p.add_argument("--yes", action="store_true")
    stt_args(p)
    p = sub.add_parser("voice")
    p.add_argument("--yes", action="store_true")
    voice_args(p)
    for name in ("check", "speaker", "host", "key"):
        p = sub.add_parser(name)
        p.add_argument("--conf", required=name != "key")
        if name == "key":
            p.add_argument("--ip")
        if name in ("speaker", "host"):
            p.add_argument("--yes", action="store_true")
        if name == "speaker":
            p.add_argument("--from", dest="src", default=site_get("ref_speaker", "YSE_REF_SPEAKER"))
            p.add_argument("--upgrade", action="store_true")
            p.add_argument("--noupdate", action="store_true")
    p = sub.add_parser("column")
    p.add_argument("--ip", required=True)
    p.add_argument("--model", required=True, help="max, station2 или mini2")
    p.add_argument("--apk", default="", help="APK Макса с HOST и BASE этого сервера")
    p.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    return {"build": cmd_build, "check": cmd_check, "speaker": cmd_speaker, "host": cmd_host,
            "key": cmd_key, "server": cmd_server, "stt": cmd_stt, "voice": cmd_voice,
            "init": cmd_init, "prepare": cmd_prepare, "all": cmd_all,
            "column": cmd_column}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
