# mistral-vm-debian-hurd

A headless, fully scriptable **Debian GNU/Hurd** virtual machine for
QEMU — the sandbox used by the [GNU AI](https://gnu-ai.org) project
and reusable by any project that needs to build, test or automate
software on real GNU/Hurd.

The official preinstalled Debian GNU/Hurd images
([cdimage](https://cdimage.debian.org/cdimage/ports/latest/)) are
designed for an interactive graphical console.  This tool wraps
them into a **headless** machine driven entirely through a serial
line:

```
┌──────────────┐                    ┌──────────────────────────────┐
│ QEMU (KVM)   │ ── disk boot ────►  │ the image's own GRUB        │
│              │                    │ with a patched grub.cfg:    │
│              │ ◄── serial line ── │ GNU Mach with console=com0  │
└──────────────┘                    └──────────────────────────────┘
       │                                        │
       └──── kernel, boot script and getty all on the serial port ──┘
```

The driver logs in as **root** with no password: that is its CI
contract.  The older preinstalled images shipped an empty root
password; the newer ones set one — so the CI copy (never the
source image) gets root's password hash cleared in `/etc/shadow`
at prepare time, exactly like the `console=com0` patch.  Custom
images with a root password you want to keep can still pass
`--root-pass PW`.  The login goes through the serial TCP socket,
and any script can then run inside the guest.

## Files

| File            | Role |
|-----------------|------|
| `hurd_vm.py`    | The driver: prepares the CI copy of the image, boots QEMU, drives the serial console, runs your script in the guest |
| `guest-httpfs.sh` | Example guest script: build and test [httpfs-translator](https://github.com/gnu-ai/httpfs-translator) inside the VM |

## Requirements (host)

- `qemu-system-x86` (KVM used automatically when `/dev/kvm` is
  available)
- `e2fsprogs` (`e2fsck`, `debugfs` — usually already installed; on
  Debian/Ubuntu: `sudo apt install e2fsprogs`)
- Python 3.8+

No GRUB tools are needed: the image's **own** GRUB is used (see
*Why we do not build a custom GRUB* below).

## Usage

```console
$ wget https://cdimage.debian.org/cdimage/ports/latest/hurd-amd64/debian-hurd-amd64-20260314.img.tar.xz
$ tar xJf debian-hurd-amd64-20260314.img.tar.xz

# Boot to a login prompt, log in as root, stop:
$ python3 hurd_vm.py debian-hurd-amd64-20260314.img

# Boot and run a script inside the guest (as root):
$ python3 hurd_vm.py debian-hurd-amd64-20260314.img my-script.sh

# Options:
#   --ram 2G        guest memory (default 1G)
#   --fresh         rebuild the CI copy even if cached
#   --keep          keep the VM alive after the script
#   --root-pass PW  the image's root password (custom images)
```

On the first run the driver prepares a **CI copy** of the image
(`debian-hurd-ci.img` next to itself) and reuses it afterwards:

1. a sparse copy of the image is made (the source is never touched);
2. the root ext2 partition is extracted and `e2fsck`'ed — a
   previously killed VM leaves it dirty, and the guest would drop
   to a maintenance shell;
3. the image's own `/boot/grub/grub.cfg` is patched with
   `console=com0` (idempotent, verified after the write);
4. the partition is written back.

The CI copy persists between runs: packages installed by earlier
guest scripts stay installed.  Pass `--fresh` to rebuild it (for
instance when the source image changed).

The guest script runs with `sh`; its output arrives on the serial
line, is echoed by the driver and saved to `serial.log`.  The exit
code of the script becomes the exit code of `hurd_vm.py` — suitable
for CI.

The guest has user-mode networking (`10.0.2.15`, gateway `10.0.2.2`,
DNS `10.0.2.3`, host port 2222 forwarded to the guest SSH port), so
`apt-get`, `git clone` etc. work inside the VM.

### Example: build and test httpfs-translator on real Hurd

```console
$ python3 hurd_vm.py debian-hurd-amd64-20260314.img guest-httpfs.sh
```

Or inline:

```sh
#!/bin/sh
set -e
apt-get update
apt-get install -y build-essential autoconf automake pkg-config \
                   libcurl4-openssl-dev libmicrohttpd-dev
git clone https://github.com/gnu-ai/httpfs-translator
cd httpfs-translator
./autogen.sh && ./configure && make && make check
```

## Why we do not build a custom GRUB

Earlier versions built a standalone El Torito GRUB with an
embedded early configuration (`grub-mkimage -O i386-pc-eltorito
-c …`), booted it from a tiny ISO and let it boot the Hurd from the
disk with `console=com0`.  That path is now known to be broken:

**a stock GRUB 2.12 El Torito image reliably breaks this very
boot.**  GNU Mach starts, decompresses, finds ACPI and HPET,
parses its command line, loads *all* the multiboot modules, prints
`task loaded:` five times — and then freezes forever right after
`start pci-arbiter: `.  The CPU falls back to the kernel idle
loop: the first Hurd server starts, blocks on something that
never completes and never resumes the next task, so nothing else
ever runs.  No panic, no message, nothing.

The evidence, gathered by bisecting one parameter at a time
(QEMU machine type, RAM, CD presence, serial console flag, GRUB
module set, embedded-config content — all of them except the
bootloader were innocent):

- the **same image, same QEMU flags, same serial console** boots
  fine when its **own on-disk GRUB** is used (a `console=com0`
  patched `grub.cfg`, no custom GRUB involved) — headless, KVM,
  TCG, 1G RAM, 2G RAM, with or without a CD attached: all fine;
- the stock-GRUB ISO path froze in every single configuration.

Hence the current design: keep the bootloader the image shipped
with and patch its configuration instead.  Simpler, and the guest
boots through exactly the same path as an interactive user.

(An unbalanced apostrophe in the embedded early configuration —
even inside a *comment* — is another way to kill the whole boot
silently: `grub-mkimage -c` breaks without any diagnostic.  That
one cost us a few serial logs full of exactly zero bytes.)

## How it works (for the curious)

- **Finding the root partition.**  The MBR of the image is parsed
  directly (`struct.unpack` on the partition entries) and the
  largest Linux (`0x83`) partition is taken as the Hurd root.
- **Patching without mounting.**  `debugfs` from e2fsprogs edits
  the ext2 filesystem image offline: `dump` extracts the
  configuration, a small rewrite appends `console=com0` to every
  GNU Mach `multiboot` line, and `rm` + `write` put it back.
  `debugfs` always exits 0, even when a command failed, so the
  result is re-read and verified after the write.  Watch out:
  `write` creates files in the *current* debugfs directory, hence
  the `cd /boot/grub` in the command file.
- **`console=com0`.**  GNU Mach selects the serial port as its
  console (see `comcnprobe` in GNU Mach's `i386/i386at/com.c`):
  kernel messages, the boot script and the `console` getty (the
  image's inittab already runs `getty 38400 console`) all use it.
  Kernel panics are printed on it too — a first-class debugging
  channel.
- **Driving the line.**  QEMU exposes the serial port as a raw TCP
  server on localhost (the Telnet chardev's IAC negotiation proved
  flaky); the driver waits for the login prompt, logs in, feeds
  the guest script through a heredoc and watches for its exit
  status, then shuts the guest down cleanly so the CI copy stays
  consistent.

## Troubleshooting

- **`virtual machine already running`** — another QEMU holds the
  CI copy; close it first (the check scans `/proc/*/fd`, no
  external tools needed).
- **A killed VM leaves the root filesystem dirty** — the next
  boot runs a guest fsck and can drop to a maintenance shell.  The
  driver repairs the CI copy at prepare time; with the source
  image, let the guest fsck finish or pass `--fresh`.
- **Boot stalls after `start pci-arbiter: `** — you are not using
  the image's own GRUB (see *Why we do not build a custom GRUB*),
  or your GRUB built the early config with an unbalanced
  apostrophe.  The serial log is empty in the second case.

## License

GPL-3.0-or-later — see `LICENSE`.

```
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Claire Ivanenka <claire@gnu-ai.org>
```
