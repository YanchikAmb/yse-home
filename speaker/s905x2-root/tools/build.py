#!/usr/bin/env python3
"""Build the payload and install it on the station.

Usage:
    python build.py /path/to/ndk                       # build only
    python build.py /path/to/ndk --device              # build, push, run
    python build.py /path/to/ndk --device --serial <device>:5555

The NDK path is the unpacked Android NDK r26d or similar; the script looks for
the armv7a clang wrapper inside it.  A static build is not possible with this
toolchain, so the payload is a position-independent executable and the compiler
is told so explicitly.
"""

import argparse
import hashlib
import os
import subprocess
import sys

CC_NAME = "armv7a-linux-androideabi21-clang.cmd"
DEFAULT_SERIAL = "192.168.1.100:5555"
REMOTE = "/data/local/tmp/reader4"
REMOTE_LOG = "/data/local/tmp/r4.log"


def find_cc(ndk):
    for root, _dirs, files in os.walk(ndk):
        if CC_NAME in files:
            return os.path.join(root, CC_NAME)
        if CC_NAME[:-4] in files:
            return os.path.join(root, CC_NAME[:-4])
    raise SystemExit("no %s under %s" % (CC_NAME, ndk))


def build(src_dir, ndk, payload_cmd=None):
    # The path has to be absolute: the compiler runs with the source directory
    # as its working directory, so a relative one would resolve elsewhere.
    cc = os.path.abspath(find_cc(ndk))
    binary = os.path.join(src_dir, "reader4")
    # The compiler driver is a .cmd wrapper, so it has to go through the
    # command processor; that means handing over one quoted string rather than
    # an argument list, which the shell would join incorrectly.
    # The command is handed to sh -c as root. Passing it as -D would send it
    # through the command processor, where shell metacharacters in the payload
    # (&, >, |) would be interpreted; writing a header keeps the value opaque.
    if payload_cmd is not None:
        header = os.path.join(src_dir, "payload_cmd.h")
        escaped = payload_cmd.replace("\\", "\\\\").replace('"', '\\"')
        with open(header, "w") as f:
            f.write("#define ROOT_PAYLOAD_CMD \"%s\"\n" % escaped)
    else:
        header = None
    command = '"%s" -O2 -fPIE -pie -DSHELL -o "%s" reader4.c -ldl' % (
        cc, binary)
    if header:
        command = '"%s" -O2 -fPIE -pie -DSHELL -DROOT_PAYLOAD_CMD_FROM_FILE -I"%s" -o "%s" reader4.c -ldl' % (
            cc, src_dir, binary)
    result = subprocess.run(command, cwd=src_dir, shell=True,
                            capture_output=True, text=True)
    errors = [line for line in result.stderr.splitlines() if "error" in line]
    if result.returncode != 0 or errors:
        print("\n".join(errors) or result.stderr, file=sys.stderr)
        raise SystemExit("build failed")
    digest = hashlib.md5(open(binary, "rb").read()).hexdigest()
    print("built %s  md5 %s" % (binary, digest))
    return binary, digest

def adb(serial, *args, **kwargs):
    return subprocess.run(["adb", "-s", serial, *args],
                          capture_output=True, text=True, **kwargs)


def wait_for_device(serial):
    # connect/get-state address the transport itself, so they take no -s; the
    # -s form is only valid for commands that target an already selected one.
    for _ in range(40):
        subprocess.run(["adb", "connect", serial],
                       capture_output=True, text=True)
        state = subprocess.run(["adb", "get-state"],
                               capture_output=True, text=True)
        if "device" in state.stdout:
            return True
        import time
        time.sleep(6)
    raise SystemExit("device %s never came back" % serial)


def reconnect(serial):
    """Re-establish the TCP transport.

    The station's adbd drops the link under load; a single failure says
    nothing about the device, so the connection is torn down and rebuilt
    before a command is retried.
    """
    import time
    subprocess.run(["adb", "disconnect", serial],
                   capture_output=True, text=True)
    time.sleep(2)
    subprocess.run(["adb", "connect", serial],
                   capture_output=True, text=True)
    time.sleep(2)


def shell(serial, command, timeout=120, attempts=3):
    out = None
    for _ in range(attempts):
        out = adb(serial, "shell", command, timeout=timeout)
        if "error: closed" not in out.stderr and "device offline" not in out.stderr:
            return out
        reconnect(serial)
    return out


def push(serial, local, remote, attempts=3):
    out = None
    for _ in range(attempts):
        out = adb(serial, "push", local, remote, timeout=300)
        if "1 file pushed" in out.stdout + out.stderr:
            return out
        reconnect(serial)
    return out


def run_on_device(serial, binary, keep_alive=False):
    wait_for_device(serial)

    before = shell(serial, "uptime; getenforce")
    print("before: %s" % before.stdout.strip())

    pushed = push(serial, binary, REMOTE)
    if "1 file pushed" not in pushed.stdout + pushed.stderr:
        raise SystemExit("push failed: %s" % (pushed.stdout + pushed.stderr))

    local = hashlib.md5(open(binary, "rb").read()).hexdigest()
    remote = shell(serial, "md5sum %s" % REMOTE).stdout.split()[0]
    if local != remote:
        raise SystemExit("push corrupted the file: %s != %s" % (local, remote))

    # When the payload starts a server, killing it after a fixed delay would
    # defeat the run; --keep-alive lets the started process outlive the shell
    # that launched it.
    timeout = "" if keep_alive else "timeout 240 "
    script = ("chmod 755 %s; rm -f %s; "
              "%s%s > %s 2>&1; echo RUN_DONE; "
              "uptime; getenforce; echo '=== log ==='; tail -80 %s"
              % (REMOTE, REMOTE_LOG, timeout, REMOTE, REMOTE_LOG, REMOTE_LOG))
    out = shell(serial, script, timeout=400, attempts=2)
    print(out.stdout)


def main():
    kit = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser()
    parser.add_argument("ndk")
    parser.add_argument("--device", action="store_true",
                        help="push and run after building")
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--src", default=os.path.join(kit, "src"))
    parser.add_argument("--payload-cmd", default=None,
                        help="command the root payload runs via sh -c")
    parser.add_argument("--keep-alive", action="store_true",
                        help="do not kill the payload; let started services run")
    args = parser.parse_args()

    binary, _digest = build(args.src, args.ndk, args.payload_cmd)
    if args.device:
        run_on_device(args.serial, binary, args.keep_alive)


if __name__ == "__main__":
    main()
