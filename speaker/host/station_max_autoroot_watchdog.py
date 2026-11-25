# -*- coding: utf-8 -*-
"""Сторож Max: один reader4 на новый boot_id (через ADB), без повторов в том же boot.

Запускается как yse-max-autoroot@stolovaya.service на frigate.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

IP = os.environ.get("YSE_SPEAKER", "")
SPEAKER_ID = os.environ.get("YSE_SPEAKER_ID", "stolovaya")
ADB = f"adb -s {IP}:5555" if IP else "adb"
STATE_DIR = Path(f"/var/lib/yse-max-autoroot-{SPEAKER_ID}")
STATE_DIR.mkdir(parents=True, exist_ok=True)
BOOT_ID_FILE = STATE_DIR / "last_boot_id"
DONE_FILE = STATE_DIR / "done_boot_id"
ATTEMPT_FILE = STATE_DIR / "attempted_boot_id"
ROOT_D = Path(os.environ.get("YSE_AUTOROOT_ROOT", "/storage/yse/s905x2-root"))
NDK = os.environ.get("YSE_AUTOROOT_NDK", "/home/administrator/android-ndk-r26d")
LOCK_BIN = Path(os.environ.get("YSE_AUTOROOT_LOCK", "/home/administrator/yse-stand/lock/bin"))
AUTH_PUB = os.environ.get("YSE_AUTOROOT_KEY", "/home/administrator/.ssh/secretary_backup.pub")
MIN_UPTIME = int(os.environ.get("YSE_AUTOROOT_MIN_UP", "300"))
POLL = int(os.environ.get("YSE_AUTOROOT_POLL", "45"))


def refresh() -> str:
    """Адрес из пакетов колонки. Кабель и Wi-Fi меняют его, сторож идёт следом."""
    global IP, ADB
    fallback = os.environ.get("YSE_SPEAKER", "").strip()
    base = os.environ.get("YSE_BASE", "").strip()
    if not base:
        env = Path("/storage/yse/%s.env" % SPEAKER_ID)
        try:
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("YSE_BASE="):
                    base = line.split("=", 1)[1].strip()
        except OSError:
            base = ""
    try:
        import yse_peer
        ip = yse_peer.current_for(base, fallback) if base else fallback
    except Exception:
        ip = fallback
    ip = (ip or fallback).strip()
    if ip and ip != IP:
        if IP:
            run(f"adb disconnect {IP}:5555", timeout=15)
            print("peer %s -> %s" % (IP, ip), flush=True)
        else:
            print("peer %s" % ip, flush=True)
        IP = ip
        ADB = "adb -s %s:5555" % IP
    return IP


def run(cmd: str, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out.strip()


def adb_shell(script: str, timeout: int = 30) -> str:
    safe = script.replace("'", "'\"'\"'")
    _, out = run(f"{ADB} shell '{safe}'", timeout=timeout)
    return out


def connect() -> bool:
    run(f"adb connect {IP}:5555", timeout=25)
    code, out = run(f"{ADB} get-state", timeout=15)
    return code == 0 and "device" in out


def boot_id() -> str:
    return adb_shell("cat /proc/sys/kernel/random/boot_id").splitlines()[0].strip()


def uptime_sec() -> int:
    line = adb_shell("cat /proc/uptime").split()
    try:
        return int(float(line[0]))
    except (IndexError, ValueError):
        return 0


def rooted() -> bool:
    out = adb_shell("test -f /dev/.yse_rooted && pgrep dropbear")
    return "dropbear" in out or ".yse_rooted" in out


def ssh_up() -> bool:
    code, _ = run(f"nc -zv -w2 {IP} 2222", timeout=8)
    return code == 0


def build_reader4() -> bool:
    code, out = run(
        f'cd "{ROOT_D}" && python3 tools/build.py "{NDK}" '
        '--payload-cmd "sh /data/local/tmp/as_payload.sh"',
        timeout=180,
    )
    if code != 0:
        print("build_reader4_fail", out[-800:])
    return code == 0 and (ROOT_D / "src" / "reader4").is_file()


def push_prereqs() -> bool:
    if not connect():
        return False
    if LOCK_BIN.joinpath("dropbear").is_file():
        run(f"{ADB} push {LOCK_BIN}/dropbear /data/local/tmp/dropbear", timeout=60)
        run(f"{ADB} push {LOCK_BIN}/dropbearkey /data/local/tmp/dropbearkey", timeout=60)
    auth = AUTH_PUB
    run(f"test -f {auth} && {ADB} push {auth} /data/local/tmp/authorized_keys", timeout=30)
    run("cp /storage/yse/app/station_max_as_payload.sh /tmp/as_payload.sh 2>/dev/null || true", timeout=10)
    code, _ = run(f"{ADB} push /tmp/as_payload.sh /data/local/tmp/as_payload.sh", timeout=30)
    if code != 0:
        return False
    adb_shell(
        "chmod 755 /data/local/tmp/as_payload.sh /data/local/tmp/dropbear /data/local/tmp/dropbearkey 2>/dev/null"
    )
    if not build_reader4():
        return False
    code, _ = run(f"{ADB} push {ROOT_D}/src/reader4 /data/local/tmp/reader4", timeout=60)
    return code == 0


def run_reader4_once() -> bool:
    if not push_prereqs():
        return False
    adb_shell(
        "chmod 755 /data/local/tmp/reader4; "
        "rm -f /data/local/tmp/r4_autoroot.log; "
        "timeout 220 /data/local/tmp/reader4 > /data/local/tmp/r4_autoroot.log 2>&1; "
        "tail -8 /data/local/tmp/r4_autoroot.log"
    )
    out = adb_shell("grep -E AUTOROOT_PAYLOAD_OK|dropbear /data/local/tmp/r4_autoroot.log; test -f /dev/.yse_rooted && echo MARKER_OK")
    ok = "AUTOROOT_PAYLOAD_OK" in out or "MARKER_OK" in out
    if ssh_up():
        key = "/home/administrator/.ssh/secretary_backup"
        _, ssh_out = run(
            f'ssh -i {key} -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=5 '
            f'-p 2222 root@{IP} "id; test -f /dev/.yse_rooted && echo SSH_ROOTED"',
            timeout=15,
        )
        ok = ok or "uid=0" in ssh_out
    print("reader4_result", out[-500:], "ssh_up", ssh_up())
    return ok or rooted()


def maybe_run_for_boot(bid: str) -> None:
    prev = BOOT_ID_FILE.read_text(encoding="utf-8").strip() if BOOT_ID_FILE.is_file() else ""
    done = DONE_FILE.read_text(encoding="utf-8").strip() if DONE_FILE.is_file() else ""
    attempted = ATTEMPT_FILE.read_text(encoding="utf-8").strip() if ATTEMPT_FILE.is_file() else ""
    if bid != prev:
        BOOT_ID_FILE.write_text(bid, encoding="utf-8")
        for f in (DONE_FILE, ATTEMPT_FILE):
            if f.is_file():
                f.unlink()
    if bid == done and rooted() and ssh_up():
        return
    u = uptime_sec()
    if u < MIN_UPTIME:
        return
    if bid == attempted:
        return
    if bid == done:
        return
    print(f"autoroot boot_id={bid} uptime={u} — reader4 once")
    ATTEMPT_FILE.write_text(bid, encoding="utf-8")
    if run_reader4_once():
        DONE_FILE.write_text(bid, encoding="utf-8")


def main() -> None:
    print(f"watchdog speaker={SPEAKER_ID}")
    while True:
        try:
            refresh()
            if not IP:
                print("adb_not_ready")
            elif connect():
                bid = boot_id()
                if bid:
                    maybe_run_for_boot(bid)
            else:
                print("adb_not_ready")
        except Exception as exc:
            print("watchdog_error", exc)
        time.sleep(POLL)


if __name__ == "__main__":
    main()