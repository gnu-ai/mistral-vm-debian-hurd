#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Claire Ivanenka <claire@gnu-ai.org>

"""Headless Debian GNU/Hurd driver: build the boot ISO, run QEMU,
drive the serial console, and run a script inside the guest.

The preinstalled Debian GNU/Hurd disk image (from
https://cdimage.debian.org/cdimage/ports/) is designed to be used
with a graphical console.  This tool turns it into a fully
headless, scriptable machine:

    1. a standalone El Torito GRUB image (built by make_iso.py) is
       booted from a tiny ISO and gives GRUB a serial terminal;
    2. the Hurd is booted with console=com0, so the GNU Mach
       kernel, the boot script and the getty all use the serial
       line;
    3. this driver logs in as root over the Telnet serial socket
       and feeds it a script.

Usage:
    hurd_vm.py <image>                  # boot to a login prompt
    hurd_vm.py <image> <script.sh>      # boot, then run the script
    add --copy to force a private copy of the image when a virtual
    machine is already running it

The script runs as root inside the guest; its stdout/stderr arrive
on the serial line (captured in serial.log and echoed here).  The
guest has user-mode networking (10.0.2.0/24, DNS 10.0.2.3), so it
can reach the network.

Requires on the host: qemu-system-x86 (KVM used when /dev/kvm is
available), grub-mkimage with the i386-pc modules (Debian:
grub-pc-bin + grub-common), python3.
"""

import os
import re
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------
# Parameters of the Debian GNU/Hurd preinstalled image.
#
# To adapt to another image, boot it once with a probe GRUB config
# (see probe.cfg) or read its /boot/grub/grub.cfg, and set:
IMAGE_URL = ("https://cdimage.debian.org/cdimage/ports/latest/"
             "hurd-amd64/debian-hurd-amd64-20260314.img.tar.xz")
ROOT_PART = "2"                            # root partition (msdos)
FS_UUID = "41bea907-da2e-4747-b848-8a2af4ce43bf"
KERNEL = "/boot/gnumach-1.8-amd64-up.gz"  # on the root filesystem

GRUB_MODULES = ["serial", "terminal", "biosdisk", "ata", "ext2", "fat",
                "part_msdos", "multiboot", "boot", "echo", "search",
                "gzio", "cat", "ls"]

TIMEOUT_LOGIN = 900       # seconds until the login prompt
TIMEOUT_CMD = 1800        # seconds per guest command
TIMEOUT_SCRIPT = 3600     # seconds for the whole guest script

# ------------------------------------------------------------------


def build_boot_iso(workdir: str) -> str:
    """Create the El Torito ISO with the serial-console GRUB."""
    cfg_in = os.path.join(HERE, "hurd-boot.cfg")
    early = os.path.join(workdir, "hurd-boot.cfg")
    with open(cfg_in) as f:
        cfg = f.read()
    cfg = (cfg.replace("@KERNEL@", KERNEL)
              .replace("@ROOTPART@", ROOT_PART)
              .replace("@UUID@", FS_UUID))
    with open(early, "w") as f:
        f.write(cfg)

    eltorito = os.path.join(workdir, "grub.eltorito")
    subprocess.run(
        ["grub-mkimage", "-O", "i386-pc-eltorito", "-o", eltorito,
         "-c", early, "-p", f"(hd0,msdos{ROOT_PART})/boot/grub"]
        + GRUB_MODULES,
        check=True)

    iso = os.path.join(workdir, "hurd-boot.iso")
    subprocess.run([sys.executable,
                    os.path.join(HERE, "make_iso.py"), eltorito, iso],
                   check=True)
    return iso


# ------------------------------------------------------------------
# A virtual machine is already running with the image?


def find_running_vms(image: str):
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


def check_vm_already_running(image: str, allow_copy: bool) -> bool:
    """Abort cleanly when another QEMU already runs the image.

    Returns True when the caller must proceed on a private copy.
    """
    pids = find_running_vms(image)
    if not pids:
        return False
    msg = ("[vm] virtual machine already running: "
           + ", ".join(f"pid {p}" for p in pids)
           + f" is using {image}")
    if allow_copy:
        print(msg)
        print("[vm] --copy given: booting a private copy "
              "(beware: the source is live, the copy may be torn)")
        return True
    print(msg)
    print("[vm] close it first, or pass --copy to force a private copy")
    sys.exit(2)


class SerialVM:
    """QEMU with the serial line on a local TCP socket (raw bytes,
    not Telnet: the telnet chardev's IAC negotiation proved flaky).

    QEMU's stderr is captured in qemu.log and echoed whenever the
    process dies - never swallow the reason again.
    """

    def __init__(self, iso: str, disk: str, memory: str = "2G",
                 allow_copy: bool = False):
        # pick a free ephemeral port: no conflict with a lingering VM
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.disk = disk
        self.iso = iso
        self.memory = memory
        self.proc = None
        self.log = b""
        self.sock = None
        self.allow_copy = allow_copy
        self.stderr_path = os.path.abspath("qemu.log")

    def _base_cmd(self, accel):
        return [
            "qemu-system-x86_64", accel, "-m", self.memory, "-no-reboot",
            "-cdrom", self.iso, "-boot", "d",
            "-drive", f"file={self.disk},cache=writeback",
            "-net", "user", "-net", "nic,model=e1000",
            "-display", "none",
            "-serial", f"tcp:127.0.0.1:{self.port},server,nowait",
        ]

    def start(self):
        accel = "-enable-kvm" if os.path.exists("/dev/kvm") else "-accel tcg"
        with open(self.stderr_path, "wb") as errf:
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
                        reason = self._qemu_died()
                        if (accel == "-enable-kvm"
                                and ("kvm" in reason.lower()
                                     or "could not access" in reason.lower())):
                            print(f"[vm] KVM failed, falling back to TCG:\n{reason}")
                            accel = "-accel tcg"
                            errf.close()
                            return self.start()
                        if "Failed to get" in reason and "lock" in reason \
                                and self.allow_copy:
                            # The image is already used by another QEMU
                            # (typically a running interactive session,
                            # which holds an exclusive write lock - even
                            # a read-only overlay would be refused).
                            # Retry on a private sparse copy of the image,
                            # leaving the other VM untouched.
                            errf.close()
                            return self._retry_with_copy()
                        if "Failed to get" in reason and "lock" in reason:
                            pids = find_running_vms(self.disk)
                            raise RuntimeError(
                                "virtual machine already running: "
                                + (", ".join(f"pid {p}" for p in pids)
                                   if pids else "unknown pid")
                                + " is using " + self.disk
                                + "; close it first or pass --copy")
                        raise RuntimeError("QEMU exited early:\n" + reason)
                    time.sleep(0.5)
            raise RuntimeError("serial port never came up:\n" + self._qemu_tail())

    def _retry_with_copy(self):
        copy = os.path.abspath(
            "copy-" + str(os.getpid()) + ".img")
        print(f"[vm] image is locked by another QEMU; "
              f"booting a private sparse copy: {copy}")
        # plain cp bypasses QEMU's image locking entirely (qemu-img
        # and overlays would take a lock the running VM refuses).
        # NOTE: the source is being written by the running VM, so the
        # copy may be slightly torn; close the other VM and remove
        # the copy for a pristine one if the guest misbehaves.
        print("[vm] note: the source is live; the copy may be torn")
        subprocess.run(
            ["cp", "--sparse=always", os.path.abspath(self.disk), copy],
            check=True)
        self.disk = copy
        return self.start()

    def _qemu_tail(self) -> str:
        try:
            with open(self.stderr_path, "rb") as f:
                return f.read().decode("latin-1", "replace")[-2000:]
        except OSError:
            return "(no QEMU stderr captured)"

    def _qemu_died(self) -> str:
        return self._qemu_tail()

    def read_some(self, wait: float = 5.0) -> bytes:
        self.sock.settimeout(wait)
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return b""
        clean = re.sub(rb"\xff[\xfd\xfc\xfe\xfb].", b"", data)
        self.log += clean
        return clean

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


def main():
    args = [a for a in sys.argv[1:] if a != "--copy"]
    allow_copy = len(args) != len(sys.argv) - 1
    if not args:
        print(__doc__)
        return 2
    disk = args[0]
    script = args[1] if len(args) > 1 else None
    try:
        return run(disk, script, allow_copy)
    except Exception as e:
        print(f"[vm] {e}")
        try:
            with open("serial.log", "rb") as f:
                tail = f.read()[-3000:]
            print("---- serial console (tail) ----")
            print(tail.decode("latin-1", "replace"))
        except OSError:
            pass
        return 1


def run(disk, script, allow_copy=False):

    if not os.path.isfile(disk):
        print(f"[vm] FAIL: no such image: {disk}")
        return 2
    if check_vm_already_running(disk, allow_copy):
        # a VM is already running with the image: boot a private copy
        import shutil
        copy = os.path.abspath("copy-" + str(os.getpid()) + ".img")
        print(f"[vm] copying the image (may be torn: the source is live)")
        shutil.copyfile(disk, copy)
        print(f"[vm] private copy: {copy}")
        disk = copy
    iso = build_boot_iso(os.getcwd())
    vm = SerialVM(iso, disk, allow_copy=allow_copy)
    vm.start()
    rc = 1
    logf = open("serial.log", "wb")
    try:
        print(f"[vm] waiting for the login prompt "
              f"(up to {TIMEOUT_LOGIN}s)...")
        if not vm.wait_for(r"login:", TIMEOUT_LOGIN):
            print("[vm] FAIL: no login prompt on the serial console")
            return 1
        print("[vm] login prompt reached, logging in as root")
        vm.send("root")
        if not vm.wait_for(r"root@|# |\$ ", 120):
            print("[vm] FAIL: no shell prompt after login")
            return 1

        if script is None:
            print("[vm] interactive boot complete; stopping the VM")
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
        if rc == 0:
            vm.send("poweroff")
            vm.wait_for(r"", 30)
    finally:
        logf.write(vm.log)
        logf.close()
        vm.stop()
        print("[vm] serial console saved to serial.log")
    return rc


if __name__ == "__main__":
    sys.exit(main())
