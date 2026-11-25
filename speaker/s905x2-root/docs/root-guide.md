# Getting root on a Yandex Station Max

A step-by-step account of how root was obtained on a retail Yandex Station Max
(YNDX-0008), from a stock device with nothing but network ADB.

This is the practical companion to the main [README](../README.md). The README
explains *why* the exploit works — the hook choice, the shellcode layout, the
kernel constants. This document is the *procedure*: what to do, in what order,
what to expect at each step, and what to do afterwards.

```
uid=0(root) gid=0(root) groups=0(root) context=u:r:kernel:s0
```

> **Read [Disclaimer](../README.md#disclaimer) first.** The payload overwrites
> twelve words of kernel text and sets SELinux to permissive. A crash mid-run
> leaves the device in a bad state.

---

## Contents

- [The method, and why it is this one](#the-method-and-why-it-is-this-one)
- [What you need](#what-you-need)
- [Step 0 — confirm you have the right firmware](#step-0--confirm-you-have-the-right-firmware)
- [Step 1 — get network ADB](#step-1--get-network-adb)
- [Step 2 — build the payload](#step-2--build-the-payload)
- [Step 3 — run it, once](#step-3--run-it-once)
- [Step 4 — read the result](#step-4--read-the-result)
- [Step 5 — dump what you need while you have root](#step-5--dump-what-you-need-while-you-have-root)
- [Step 6 — clean up](#step-6--clean-up)
- [What root here can and cannot do](#what-root-here-can-and-cannot-do)
- [Recovery](#recovery)
- [How this differs from the DNS method](#how-this-differs-from-the-dns-method)
- [Where these steps came from](#where-these-steps-came-from)

---

## The method, and why it is this one

Two approaches circulate for getting root on a Yandex Station. Only one works on
the Max, and it is worth being clear about which and why, because the other is
far more widely documented and will waste your time.

**The DNS method** redirects `quasar.yandex.net` to a server that answers the
firmware's activation request with `adbd.remoteActivation=true`, which makes the
firmware start `adbd` on port 5555 as root. It works on the Buildroot-based
speakers — Lite, Mini, Midi, Station 2. **It does not work on the Max.** The Max
is an Android device (`YaOS_X`, Android 9, SDK 28) with a production
`adbd`, and `adb root` there returns:

```
adbd cannot run as root in production builds
```

The Max also has no `/system/vendor/quasar/` tree, no `activate_adb.sh` and no
`onboot.sh` to trigger it — the pieces the DNS method relies on are simply not
present. Published documentation on the Max records the DNS route as
non-working and the platform as needing research.

**This repository is that research.** The Max is closed down by AVB 1.1,
dm-verity on `/vendor` and `/`, SELinux enforcing, and a production `adbd`.
Editing the root filesystem is not available, so the way in is a local kernel
escalation:

The escalation path is a use-after-free in the Mali GPU driver (`kbase`). The
driver hands userspace a `mem_handle` for a page it keeps using for its own page
tables after freeing. The freed page gets reused as a page table for a GPU
address, and through that userspace gains write access to an arbitrary physical
page of kernel memory. The payload uses that to relax SELinux and then write
shellcode that calls `commit_creds(init_cred)`.

The result is root on the process that triggers it, running in the kernel
domain. Nothing is written to a block device and no firmware image is modified,
so the device boots unchanged afterwards.

---

## What you need

- **The device on the matching firmware.** This exact guide targets build
  `PPR1.180610.011.20220415`, kernel `4.9.113-g985a1e6b`, with KASLR disabled.
  See [Step 0](#step-0--confirm-you-have-the-right-firmware) — do not skip it.
- **ADB access over the network.** The Max exposes `:5555` once the server-side
  activation has run. See [Step 1](#step-1--get-network-adb).
- **An Android NDK**, r26d or similar. Only the `.cmd` wrapper variants of the
  NDK tools work on Windows.
- **Python 3** to run the build script.

Two warnings that cost people runs:

- `-static` does not link with this toolchain. The build script already passes
  `-fPIE -pie`, which is required.
- `-DSHELL` is mandatory. Without it the payload does not interact with the
  device the way the procedure expects.

---

## Step 0 — confirm you have the right firmware

Every constant in `src/reader4.c` is tied to one kernel build. Running it against
a different one overwrites the wrong words of kernel text. The payload checks the
fingerprint before doing anything and refuses on a mismatch, but check first so
you do not waste a run:

```bash
adb shell getprop ro.build.fingerprint
```

Expected:

```
Yandex/yandexstation_2/yandexstation_2:9/PPR1.180610.011/20220415:user/release-keys
```

Also worth recording, in case you later need to compute new constants:

```bash
adb shell getprop ro.product.device      # yandexstation_2
adb shell uname -a                        # 4.9.113-g985a1e6b
adb shell id                              # uid=2000(shell)
adb shell getenforce                      # Enforcing
```

If the fingerprint differs, stop and follow
[Porting to another kernel](../README.md#porting-to-another-kernel). Do not run
the payload.

### Do not let it update

An OTA changes the kernel build and invalidates every constant. Before doing
anything else, block updates — the Max has no `/etc/init.d`, so this means
disabling the update path in the Android package manager or firewalling the
update host at the router. If you got here through the DNS method on another
model, note that the Max never had that route to begin with.

Once a device has updated past `20220415`, this payload no longer applies and
the constants must be recomputed from the new kernel image.

---

## Step 1 — get network ADB

The Max runs a production `adbd`, so `adb root` is refused. What you need is
simply `adbd` listening on TCP, which the server-side activation provides. The
device comes back from that activation as `uid=2000(shell)`, which is enough —
the exploit escalates from there.

```bash
adb connect <station-ip>:5555
adb devices          # must show:  <station-ip>:5555    device
adb shell id         # uid=2000(shell) ... context=u:r:shell:s0
```

Two things to note about this state:

- **`/data/local/tmp` is writable**, `/tmp` is not. The procedure and the build
  script both use `/data/local/tmp`.
- **`dmesg` is restricted** (`kernel.dmesg_restrict`), and many `/proc` and
  `/sys` nodes return `Permission denied`. This is expected at this stage and is
  part of why the escalation is needed.

If `adb shell id` reports `uid=2000(shell)`, proceed. If it reports anything else,
stop and sort that out first.

---

## Step 2 — build the payload

```bash
python tools/build.py /path/to/android-ndk-r26d
```

The script finds the `armv7a-linux-androideabi21-clang.cmd` wrapper under the NDK
path, compiles `src/reader4.c` with `-O2 -fPIE -pie -DSHELL`, and writes
`src/reader4`.

The expected md5 of the binary for this kernel build is:

```
7d59c29b246ec99ce578a095ae0bfa43
```

Check it, because a mismatch means the source tree or the toolchain is not what
you think:

```bash
md5sum src/reader4
```

### Where the payload command comes from

`reader4.c` re-executes a shell command as root at the end. The command is
compiled in, and by default it is whatever `src/payload_cmd.h` defines:

```c
#define ROOT_PAYLOAD_CMD "id > /data/local/tmp/who.txt; getenforce >> /data/local/tmp/who.txt"
```

`payload_cmd.h` is **generated**, not source. `tools/build.py` writes it from its
`--payload` argument, and it is deliberately written to a header rather than
passed as `-D`: the command is handed to `sh -c` as root, and `-D` would send it
through the Windows command processor first, where `&`, `>`, `|` in the payload
would be interpreted.

To use your own command, pass it at build time:

```bash
python tools/build.py /path/to/ndk --payload "id > /data/local/tmp/who.txt; getenforce >> /data/local/tmp/who.txt"
```

The file is listed in `.gitignore` and is not part of the repository. If you have
a checkout that still contains one, delete it — a stale command there silently
becomes the payload.

---

## Step 3 — run it, once

Use the script, which does the checks for you:

```bash
python tools/build.py /path/to/android-ndk-r26d --device --serial <station-ip>:5555
```

It prints `uptime` before running, pushes the binary, verifies the md5 on the
device after the push, runs it with a timeout, and prints the tail of the log.

By hand, the equivalent — and note that the `rm`, the run and the log read are
**one command**, because the device can reboot between separate ones:

```bash
adb push src/reader4 /data/local/tmp/reader4
adb shell "md5sum /data/local/tmp/reader4"        # compare with the local copy
adb shell "chmod 755 /data/local/tmp/reader4"
adb shell "rm -f /data/local/tmp/r4.log; timeout 240 /data/local/tmp/reader4 > /data/local/tmp/r4.log 2>&1; echo RUN_DONE; id; getenforce"
```

**Before every run, check `uptime`.** The device reboots spontaneously on its
own, `/data/local/tmp` is wiped by a reboot, and `r4.log` survives from the
previous run — so a stale log is easy to mistake for a fresh one. Uptime should
be at least three minutes.

```bash
adb shell uptime
```

**Run it once.** Each run writes `0` to `selinux_enforcing` and mutates twelve
words of `sel_read_checkreqprot`. A repeat run without a reboot is not
recommended; see [Repeat runs](../README.md#repeat-runs-and-restoring-selinux).

---

## Step 4 — read the result

A successful run ends with the payload reading
`/sys/fs/selinux/checkreqprot`, which triggers the hook. The log shows the
escalation and then the re-executed command's own output:

```bash
adb shell cat /data/local/tmp/r4.log
```

The proof that the root is real is the `id` from the re-executed process:

```
uid=0(root) gid=0(root) groups=0(root) context=u:r:kernel:s0
```

### Why the check has to be a new process

bionic caches `getuid()` and `geteuid()` when a process starts. Checking inside
the process that read the node shows nothing — the cached values are still the
old ones. That is why the payload does:

```c
execl("/system/bin/sh", "sh", "-c", payload, (char*)NULL);
```

and the `id` you read comes from that new process. See
[Check root from a new process](../README.md#check-root-from-a-new-process).

### If it did not work

- **`EXIT_STATUS=139` or `138`** — SIGSEGV/SIGBUS. The layout race was lost.
  Reboot and try again; the [Pitfalls](../README.md#pitfalls-that-cost-runs)
  section lists the changes that made this worse, and what not to touch.
- **`RUN_DONE` never printed, `ps` shows the process in state `R 0`** without
  `PGD_RESTORED` — it is wedged. `adb reboot`, then wait 70 s, then another
  150 s.
- **Exit code 255** — the device rebooted during the command.
- **`push` reported `error: closed`** — the push silently failed. This is why
  the md5 is compared on the device, not just reported locally.

---

## Step 5 — dump what you need while you have root

Root here is on **one process**, not a persistent daemon. Whatever you want to
take off the device has to happen inside the payload command, in the same run,
because there is no session to come back to.

The payload in this repository does exactly that — it dumps the `boot`
partition, which is how the load addresses in the README were confirmed against
the real device:

```c
const char* payload =
  "id; getenforce;"
  "dd if=/dev/block/platform/ffe07000.emmc/by-name/boot"
  " of=/data/local/tmp/boot.img bs=1048576;"
  "chmod 0644 /data/local/tmp/boot.img;"
  "md5sum /data/local/tmp/boot.img;"
  "ls -l /data/local/tmp/boot.img;"
  "echo 1 > /sys/fs/selinux/enforce;"
  "getenforce";
execl("/system/bin/sh", "sh", "-c", payload, (char*)NULL);
```

Three details in there are load-bearing:

**The partition path is exactly that.** `/dev/block/by-name/` does not exist on
this build — `by-name` lives inside the platform directory,
`/dev/block/platform/ffe07000.emmc/by-name/`.

**The `chmod` is not cosmetic.** The file `dd` produces is created `0600` owned
by `root`, and `adbd` on this build does not run as root, so without the `chmod`
the copy cannot be pulled off:

```bash
adb pull /data/local/tmp/boot.img
```

A run where the `chmod` is missing or fails **looks successful in the log** while
the file stays unreadable. Check `ls -lZ /data/local/tmp/boot.img` after any run
that was supposed to produce a dump.

**`echo 1 > /sys/fs/selinux/enforce` at the end.** Returning to enforcing does
not need a reboot and takes effect immediately — this is verified on this build.
Do it in the same run, so the device is not left permissive.

To work out what else is worth taking, remember that the shell you get runs in
`u:r:kernel:s0`. Paths that are `Permission denied` to `shell` are readable
here: `/proc/kallsyms`, `/proc/config.gz`, and the block devices. If you are
porting this to another build, `/proc/kallsyms` is the one that matters most —
it is what `tools/extract-offsets.py` wants.

---

## Step 6 — clean up

```bash
adb shell rm -f /data/local/tmp/reader4 /data/local/tmp/r4.log
adb pull /data/local/tmp/boot.img    # before removing it, if you want the copy
adb shell rm -f /data/local/tmp/boot.img
adb shell getenforce                 # must print Enforcing
```

`getenforce` printing `Enforcing` confirms the payload's final `echo 1` ran.
If it prints `Permissive`, write it back from a root run:

```bash
echo 1 > /sys/fs/selinux/enforce
```

### What a reboot does and does not fix

- **Returns the twelve words of kernel text** that the run overwrote. This is
  the reason a reboot is the clean way to reset.
- **Does not** affect SELinux state — the `echo 1` in the payload already did
  that, without a reboot.
- **Wipes `/data/local/tmp`**, which is why the log and binary disappear.

So a reboot is not a repair for a failed run so much as the normal way to get
back to a known state before trying again.

---

## What root here can and cannot do

This is the part that surprises people, so it is worth stating plainly.

**You cannot mount `/system`, `/vendor` or `/` read-write and keep the change.**
AVB 1.1 and dm-verity are enforcing, and `verifiedbootstate=green`. Editing the
root filesystem breaks verity and the device will not boot. Root here does not
change that — verity is verified below the kernel interface this exploit gives
you write access through.

**You do not get a persistent root shell.** The privilege is granted to the
process that triggers the hook. Once it exits, the next process is
`uid=2000(shell)` again. There is no daemon left listening, no `su`, no
modified `adbd`.

**What it is actually good for:** reading anything on the device, dumping
partitions, extracting kernel symbols (which is how the constants for a port
are obtained), and inspecting the platform. It is a research primitive, not a
jailbreak.

**Root is in the kernel domain (`u:r:kernel:s0`), not `u:r:init:s0` or
`u:r:su:s0`.** Some things behave differently than a normal Android root shell
because of this. Notably, this is why the payload finishes by restoring
enforcing rather than leaving a relaxed policy behind — a process in the kernel
domain that fails can take the device down with it.

---

## Recovery

There is no supported way to permanently modify this device, so "recovery" here
means getting back to a working stock device after a bad run.

**Device unresponsive or wedged.** Cut power, wait, power on. The exploit makes
no persistent changes, so a power cycle returns the device to its previous
state. If it will not come back, `adb reboot` while it still answers; if it does
not answer at all, a power cycle is the only option.

**Device bootloops.** This should not happen from an exploit run, since nothing
persistent is written — a loop more likely means a botched attempt at something
else. The route back is the recovery/fastboot path as described in the platform
documentation, but note that this repository deliberately does **not** include
anything that flashes a partition, and none of the steps here ever wrote to one.

**SELinux left permissive.** `echo 1 > /sys/fs/selinux/enforce` from a root run,
or reboot — a reboot restores the field from the policy at boot.

**A copy of `boot.img` you no longer want.** Delete it. It is an exact record of
this device's boot partition; it is useful for comparing against a firmware
package if it ever needs to be shown the partition was untouched, and for
nothing else — see
[The kernel inside boot.img is encrypted](../README.md#the-kernel-inside-bootimg-is-encrypted).

---

## How this differs from the DNS method

Worth stating once, because the two get confused constantly and the confusion
sends people down the wrong path on the Max.

| | DNS method | This method |
|---|---|---|
| Devices | Lite, Mini, Midi, Station 2 | **Station Max (YNDX-0008)**, Station 1 |
| Platform | Buildroot / quasar | **Android 9 (YaOS_X)** |
| Mechanism | server answers activation with `adbd.remoteActivation=true` | **use-after-free in the Mali driver** |
| Result | `adbd` on `:5555` as root, persistent | root on **one process**, in the kernel domain |
| Requires | router DNS control, **factory reset** | nothing but network ADB |
| Firmware change | server-driven markers in `/data/quasar` | **none at all** |
| Survives reboot | yes | no |

Two practical consequences:

- **The DNS method needs a factory reset** of the speaker. This one does not.
- **This method leaves nothing behind.** Nothing is installed, no marker file is
  created, no block device is written. A reboot returns the device to exactly
  the state it was in.

If your device is a Max and you were following a DNS-method guide, that is why
it did not work — the firmware simply has no such code path. Conversely, if your
device is a Buildroot speaker, this repository does not apply: the Mali driver
and the SELinux node it hooks are Android-platform specifics.

---

## Where these steps came from

This is our own work, against a retail device, starting from network ADB and no
prior tooling. It is recorded here because the material that does exist for
Yandex Stations — the most complete public write-up being
[`Marker284/yandex-station-extended`](https://github.com/Marker284/yandex-station-extended),
which backs `hackalice.me` — **documents the Max as having no known root route**
and points at the bootloader or an exploit as open directions.

That documentation is still worth reading, for two things in particular:

- Its platform notes on the Max match what we found: `yandexstation_2`,
  Android 9, `production adbd`, AVB + dm-verity + SELinux enforcing, and the
  DNS method marked as not applicable.
- Its `contrib/` scripts are a good catalogue of what you can do with the device
  **once you have root** — audio arbitration, AirPlay fixes, USB host. None of
  them obtain root; every one assumes it is already there.

What is new here is the escalation itself, and the constants that make it work
on this build:

| Constant | Value | What it is |
|---|---|---|
| `YNDX_INIT_CRED` | `0x164b268` | `init_cred` |
| `YNDX_COMMIT_CREDS` | `0x4c1e0` | `commit_creds` |
| `YNDX_SEL_READ_ANCHOR` | `0x36c578` | `sel_read_checkreqprot` |
| `YNDX_SELINUX_ENFORCING` | `0x189352c` | `selinux_enforcing` |
| `YNDX_AVC_DENIED` | `0x361d80` | `avc_denied` (evaluated, not used — see README) |
| `KERNEL_BASE` | `0x1080000` | load address, confirmed against the dumped `boot.img` |

The reasoning behind the hook choice, the shellcode and the pitfalls is in the
[README](../README.md); this document deliberately does not repeat it.
