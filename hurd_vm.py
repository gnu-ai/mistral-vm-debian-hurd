#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Claire Ivanenka <claire@gnu-ai.org>

"""Headless Debian GNU/Hurd driver: prepare a scriptable CI copy of
the preinstalled disk image, run it under QEMU, drive the serial
console, and run a script inside the guest.

The preinstalled Debian GNU/Hurd disk image (from
https://cdimage.debian.org/cdimage/ports/) is designed to be used
with a graphical console.  This tool turns it into a fully
headless, scriptable machine - WITHOUT building a custom GRUB:

    1. a private copy of the image is prepared once (and cached):
       the ext2 root partition is fsck'ed and its own grub.cfg is
       patched to boot GNU Mach with console=com0.  The kernel, the
       boot script AND the getty (inittab runs "getty 38400
       console") then all use the serial line.

       We deliberately boot the image's own GRUB.  A custom El
       Torito GRUB built by grub-mkimage reliably breaks this
       image: GNU Mach starts, loads every module, then freezes
       forever right after "start pci-arbiter: " (the CPU falls
       back to the kernel idle loop, the first Hurd server never
       resumes the next one).  The very same image boots fine from
       its on-disk GRUB - so we patch that one instead.

    2. this driver logs in as root over the raw serial TCP socket
       (the preinstalled image ships with no root password) and
       feeds it a script.

Usage:
    hurd_vm.py <image>                  # boot to a login prompt
    hurd_vm.py <image> <script.sh>      # boot, then run the script
    options: --ram 2G   guest memory (default 1G)
             --fresh    rebuild the CI copy even if cached
             --keep     keep the VM alive after the script
             --root-pass PASSWORD   the image's root password
                                   (only needed for custom images)

The script runs as root inside the guest; its output arrives on the
serial line (captured in serial.log and echoed here).  The guest
gets user-mode networking (10.0.2.0/24, DNS 10.0.2.3) and a 2222
host forward to its SSH port.  The prepared CI copy is kept between
runs (named <image>.ci.img next to this file): packages installed
by earlier scripts stay installed; pass --fresh to rebuild it.

Requires on the host: qemu-system-x86 (KVM used when /dev/kvm is
available), e2fsprogs (e2fsck, debugfs), python3.  The guest is
given 1 GB of RAM by default: it boots fine with that, and on a
memory-tight host a bigger allocation kills the guest silently
before even SeaBIOS runs.
"""

import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------
# Parameters.
#
# The image itself is described by its own GRUB configuration, so no
# kernel name or UUID is needed here: the patch below simply appends
# the serial console argument to every GNU Mach multiboot line.
IMAGE_URL = ("https://cdimage.debian.org/cdimage/ports/latest/"
             "hurd-amd64/debian-hurd-amd64-20260314.img.tar.xz")

CONSOLE_ARG = "console=com0"     # serial kernel console (COM1)

TIMEOUT_LOGIN = 900       # seconds until the login prompt
TIMEOUT_SCRIPT = 3600     # seconds for the whole guest script

# Prepared copy of the image.  Set HURD_VM_CI_IMAGE to boot a
# private one instead of the shared default — two drivers may run
# concurrently, each on its own copy (mind the RAM: 1 GB each).
CI_IMAGE = os.environ.get("HURD_VM_CI_IMAGE",
                          os.path.join(HERE, "debian-hurd-ci.img"))
SERIAL_LOG = os.path.join(HERE, "serial.log")
QEMU_LOG = os.path.join(HERE, "qemu.log")

# ------------------------------------------------------------------
# Small host-tool helpers.


def find_tool(name):
    """Locate a host binary, looking in /usr/sbin and /sbin too
    (e2fsprogs lives there and is not always in PATH)."""
    candidates = ([shutil.which(name)] if shutil.which(name) else []) \
        + [os.path.join(d, name) for d in ("/usr/sbin", "/sbin")]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    sys.exit(f"[vm] FAIL: required tool not found: {name} "
             f"(Debian: apt install e2fsprogs)")


# ------------------------------------------------------------------
# Preparing the CI copy of the image.


def root_partition(image):
    """Return (start_lba, sectors) of the largest Linux (0x83)
    partition of an MBR-partitioned disk image: the Hurd root."""
    with open(image, "rb") as f:
        mbr = f.read(512)
    if len(mbr) < 512 or mbr[510:512] != b"\x55\xaa":
        sys.exit("[vm] FAIL: no MBR signature in the image")
    best = None
    for i in range(4):
        entry = mbr[446 + 16 * i: 446 + 16 * (i + 1)]
        if entry[4] == 0x83:                      # Linux filesystem
            lba, size = struct.unpack("<II", entry[8:16])
            if best is None or size > best[1]:
                best = (lba, size)
    if best is None:
        sys.exit("[vm] FAIL: no Linux partition in the image")
    return best


def patch_grub_cfg(text):
    """Append the serial console argument to every GNU Mach
    multiboot line of a grub.cfg (idempotent)."""
    out = []
    patched = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if (stripped.startswith("multiboot") and "gnumach" in line
                and CONSOLE_ARG not in line):
            line = line.rstrip() + " " + CONSOLE_ARG + "\n"
            patched += 1
        out.append(line)
    return "".join(out), patched


def prepare_ci_image(src, fresh=False):
    """Create (or reuse) the bootable CI copy of the image.

    The marker file besides the copy holds two lines: the source
    fingerprint, and "clean" or "dirty" - whether the copy was shut
    down properly.  A dirty copy is fsck'ed again before use: an
    unclean shutdown (a killed VM, a crashed driver) leaves the
    root filesystem inconsistent, and the guest would drop to an
    fsck maintenance shell instead of the login prompt.
    """
    marker = CI_IMAGE + ".ready"
    src_stat = os.stat(src)
    fingerprint = f"{os.path.getsize(src)}:{int(src_stat.st_mtime)}"
    e2fsck = find_tool("e2fsck")
    debugfs = find_tool("debugfs")
    lba, sectors = root_partition(src)

    def state_of_marker():
        try:
            with open(marker) as f:
                lines = f.read().split()
            return lines[0], (lines[1] if len(lines) > 1 else "dirty")
        except OSError:
            return None, "dirty"

    def write_marker(state):
        with open(marker, "w") as f:
            f.write(fingerprint + " " + state + "\n")

    def fsck_copy():
        """Repair the root filesystem of the CI copy in place."""
        part = CI_IMAGE + ".part"
        subprocess.run(["dd", f"if={CI_IMAGE}", f"of={part}",
                        "bs=512", f"skip={lba}", f"count={sectors}",
                        "status=none"], check=True)
        r = subprocess.run([e2fsck, "-fy", part],
                           capture_output=True, text=True)
        if r.returncode >= 4:        # 1-3 = fixed, >=4 = unfixable
            os.unlink(part)
            sys.exit("[vm] FAIL: e2fsck could not repair the root "
                     "filesystem:\n" + r.stdout[-2000:])
        subprocess.run(["dd", f"if={part}", f"of={CI_IMAGE}",
                        "bs=512", f"seek={lba}", "conv=notrunc",
                        "status=none"], check=True)
        os.unlink(part)

    old_print, old_state = state_of_marker()
    if not fresh and old_print == fingerprint and os.path.isfile(CI_IMAGE):
        if old_state != "clean":
            print("[vm] the CI copy was not shut down cleanly: "
                  "repairing its filesystem")
            fsck_copy()
            write_marker("clean")
        else:
            print(f"[vm] reusing the prepared CI copy: {CI_IMAGE}")
        return CI_IMAGE

    print(f"[vm] preparing the CI copy (once): {CI_IMAGE}")
    if os.path.exists(CI_IMAGE):
        os.unlink(CI_IMAGE)
    # A plain cp bypasses QEMU's image locking (an overlay would
    # take a lock the running VM refuses) and keeps the holes.
    subprocess.run(["cp", "--sparse=always", src, CI_IMAGE], check=True)

    # Extract the root partition, fsck it (a previously killed VM
    # leaves it dirty - the guest would drop to a maintenance
    # shell), patch grub.cfg, and put it back in place.
    part = CI_IMAGE + ".part"
    subprocess.run(["dd", f"if={CI_IMAGE}", f"of={part}",
                    "bs=512", f"skip={lba}", f"count={sectors}",
                    "status=none"], check=True)
    print("[vm] checking and patching the root filesystem")
    r = subprocess.run([e2fsck, "-fy", part],
                       capture_output=True, text=True)
    if r.returncode >= 4:        # 1-3 = fixed, >=4 = unfixable
        os.unlink(part)
        sys.exit("[vm] FAIL: e2fsck could not repair the root "
                 "filesystem:\n" + r.stdout[-2000:])

    cfg_path = part + ".grub.cfg"
    subprocess.run([debugfs, "-R", "dump /boot/grub/grub.cfg " + cfg_path,
                    part], check=True, capture_output=True)
    with open(cfg_path) as f:
        cfg = f.read()
    patched_cfg, count = patch_grub_cfg(cfg)
    if count == 0:
        os.unlink(part)
        sys.exit("[vm] FAIL: no GNU Mach multiboot line found in the "
                 "image grub.cfg - is this a Debian GNU/Hurd image?")
    print(f"[vm] serial console enabled on {count} boot entr"
          f"{'y' if count == 1 else 'ies'} ({CONSOLE_ARG})")
    with open(cfg_path, "w") as f:
        f.write(patched_cfg)

    # debugfs "write" creates the file in its *current* directory,
    # so cd to /boot/grub inside the same command file.
    cmds = part + ".debugfs.cmds"
    with open(cmds, "w") as f:
        f.write("cd /boot/grub\n"
                "rm grub.cfg\n"
                f"write {cfg_path} grub.cfg\n")
    subprocess.run([debugfs, "-w", "-f", cmds, part],
                   check=True, capture_output=True)

    # debugfs always exits 0, even when a command failed: verify
    # that the patched configuration really is in the filesystem.
    r = subprocess.run([debugfs, "-R", "cat /boot/grub/grub.cfg", part],
                       capture_output=True, text=True)
    if CONSOLE_ARG not in r.stdout:
        os.unlink(part)
        sys.exit("[vm] FAIL: the grub.cfg patch did not land in the "
                 "image\n" + r.stdout[-2000:])
    subprocess.run(["dd", f"if={part}", f"of={CI_IMAGE}",
                    "bs=512", f"seek={lba}", "conv=notrunc",
                    "status=none"], check=True)
    for tmp in (part, cfg_path, cmds):
        os.unlink(tmp)

    with open(marker, "w") as f:
        f.write(fingerprint + " clean\n")
    return CI_IMAGE


# ------------------------------------------------------------------
# A virtual machine is already running with the image?


def find_running_vms(image):
    """Return the PIDs of the QEMU processes holding IMAGE open.

    Implemented by scanning /proc/*/fd (no external tools, works for
    the processes of the current user).
    """
    image = os.path.abspath(image)
    pids = []
    try:
        proc = os.listdir("/proc")
    except OSError:
        return pids
    for pid in proc:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
            if "qemu" not in comm:
                continue
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    target = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if target == image:
                    pids.append(int(pid))
                    break
        except OSError:
            continue
    return sorted(pids)


# ------------------------------------------------------------------


class SerialVM:
    """QEMU with the serial line on a local TCP socket (raw bytes,
    not Telnet: the telnet chardev's IAC negotiation proved flaky).

    QEMU's stderr is captured in qemu.log and echoed whenever the
    process dies - never swallow the reason again.
    """

    def __init__(self, disk: str, memory: str = "1G"):
        # Pick a free ephemeral port: no conflict with a lingering VM.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.disk = disk
        self.memory = memory
        self.proc = None
        self.log = b""
        self.sock = None

    def _base_cmd(self, accel):
        # Boot the disk itself: the image's own GRUB, with the
        # patched grub.cfg, handles everything from there.
        return [
            "qemu-system-x86_64", accel, "-m", self.memory, "-no-reboot",
            "-drive", f"file={self.disk},cache=writeback",
            "-net", "user,hostfwd=tcp:127.0.0.1:2222-:22",
            "-net", "nic,model=e1000",
            "-display", "none",
            "-serial", f"tcp:127.0.0.1:{self.port},server,nowait",
        ]

    def start(self):
        accel = "-enable-kvm" if os.path.exists("/dev/kvm") else "-accel tcg"
        with open(QEMU_LOG, "wb") as errf:
            self.proc = subprocess.Popen(self._base_cmd(accel),
                                         stdout=subprocess.DEVNULL,
                                         stderr=errf)
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    self.sock = socket.create_connection(
                        ("127.0.0.1", self.port), timeout=5)
                    self.sock.settimeout(5)
                    return
                except OSError:
                    if self.proc.poll() is not None:
                        reason = self._qemu_tail()
                        if (accel == "-enable-kvm"
                                and ("kvm" in reason.lower()
                                     or "could not access" in reason.lower())):
                            print(f"[vm] KVM failed, falling back to "
                                  f"TCG:\n{reason}")
                            accel = "-accel tcg"
                            errf.close()
                            return self.start()
                        if "Failed to get" in reason and "lock" in reason:
                            pids = find_running_vms(self.disk)
                            raise RuntimeError(
                                "virtual machine already running: "
                                + (", ".join(f"pid {p}" for p in pids)
                                   if pids else "unknown pid")
                                + " is using " + self.disk
                                + "; close it first")
                        raise RuntimeError("QEMU exited early:\n" + reason)
                    time.sleep(0.5)
            raise RuntimeError("serial port never came up:\n" + self._qemu_tail())

    def _qemu_tail(self) -> str:
        try:
            with open(QEMU_LOG, "rb") as f:
                return f.read().decode("latin-1", "replace")[-2000:]
        except OSError:
            return "(no QEMU stderr captured)"

    def read_some(self, wait: float = 5.0) -> bytes:
        self.sock.settimeout(wait)
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return b""
        self.log += data
        return data

    def wait_for(self, pattern: str, timeout: int) -> bool:
        rx = re.compile(pattern.encode())
        deadline = time.time() + timeout
        while time.time() < deadline:
            if rx.search(self.log):
                return True
            self.read_some(5)
            if self.proc.poll() is not None:
                raise RuntimeError("QEMU exited during wait for " + pattern)
        return False

    def send(self, line: str):
        self.sock.sendall(line.encode() + b"\r")

    def stop(self):
        if self.sock:
            self.sock.close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ------------------------------------------------------------------


def parse_args(argv):
    opts = {"ram": "1G", "fresh": False, "keep": False, "root_pass": None}
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--fresh":
            opts["fresh"] = True
        elif a == "--keep":
            opts["keep"] = True
        elif a == "--ram":
            i += 1
            opts["ram"] = argv[i]
        elif a == "--root-pass":
            i += 1
            opts["root_pass"] = argv[i]
        else:
            rest.append(a)
        i += 1
    return rest, opts


def run(src, script, opts):

    if not os.path.isfile(src):
        print(f"[vm] FAIL: no such image: {src}")
        return 2
    disk = prepare_ci_image(src, opts["fresh"])
    marker = disk + ".ready"

    # From now on the copy is "dirty" until the guest has powered
    # off cleanly: a crash or a kill must not leave the marker
    # lying about a clean filesystem.
    with open(marker) as f:
        fingerprint = f.read().split()[0]
    with open(marker, "w") as f:
        f.write(fingerprint + " dirty\n")

    vm = SerialVM(disk, memory=opts["ram"])
    vm.start()
    rc = 1
    try:
        print(f"[vm] waiting for the login prompt "
              f"(up to {TIMEOUT_LOGIN}s)...")
        if not vm.wait_for(r"login:", TIMEOUT_LOGIN):
            print("[vm] FAIL: no login prompt on the serial console")
            return 1
        print("[vm] login prompt reached, logging in as root")
        vm.send("root")
        # The stock image has no root password; custom images may.
        if not vm.wait_for(r"Password:|root@|# ", 120):
            print("[vm] FAIL: no response after the login name")
            return 1
        if re.search(rb"Password:", vm.log) and opts["root_pass"]:
            vm.send(opts["root_pass"])
        elif re.search(rb"Password:", vm.log):
            print("[vm] FAIL: the image wants a root password; "
                  "pass --root-pass PASSWORD")
            return 1
        if not vm.wait_for(r"root@|# ", 120):
            print("[vm] FAIL: no shell prompt after login")
            return 1

        if script is None:
            print("[vm] interactive boot complete")
            rc = 0
        else:
            with open(script) as f:
                body = f.read()
            vm.log = b""
            # Feed the script through a heredoc, then run it.
            vm.send("cat > /tmp/guest.sh <<'GUESTEOF'")
            for line in body.splitlines():
                vm.send(line)
            vm.send("GUESTEOF")
            vm.send("sh /tmp/guest.sh; echo GUESTRC=$?")
            if not vm.wait_for(r"GUESTRC=(\d+)", TIMEOUT_SCRIPT):
                print("[vm] FAIL: the guest script did not complete")
            else:
                m = re.search(rb"GUESTRC=(\d+)", vm.log)
                rc = 0 if int(m.group(1)) == 0 else 1
            print(vm.log.decode("latin-1", "replace")[-4000:])
        if rc == 0 and not opts["keep"]:
            # Shut the guest down cleanly and WAIT for it: killing
            # QEMU mid-shutdown leaves the filesystem inconsistent.
            # A powered-off guest makes QEMU exit by itself
            # (ACPI), so watch the process instead of guessing a
            # delay.
            vm.send("poweroff")
            print("[vm] waiting for the guest to power off...")
            deadline = time.time() + 180
            while time.time() < deadline and vm.proc.poll() is None:
                vm.read_some(5)
            if vm.proc.poll() is None:
                print("[vm] warning: the guest did not power off in "
                      "time; the CI copy will be repaired next run")
            else:
                print("[vm] guest powered off cleanly")
                with open(marker, "w") as f:
                    f.write(fingerprint + " clean\n")
    finally:
        with open(SERIAL_LOG, "wb") as logf:
            logf.write(vm.log)
        vm.stop()
        print("[vm] serial console saved to serial.log")
    return rc


def main():
    # Report progress live even when the output is redirected
    # (background runs), instead of buffering until the end.
    sys.stdout.reconfigure(line_buffering=True)
    rest, opts = parse_args(sys.argv[1:])
    if not rest:
        print(__doc__)
        return 2
    src = rest[0]
    script = rest[1] if len(rest) > 1 else None
    try:
        return run(src, script, opts)
    except Exception as e:
        print(f"[vm] {e}")
        try:
            with open(SERIAL_LOG, "rb") as f:
                tail = f.read()[-3000:]
            print("---- serial console (tail) ----")
            print(tail.decode("latin-1", "replace"))
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
