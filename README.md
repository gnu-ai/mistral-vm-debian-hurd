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
┌────────────┐  El Torito ISO   ┌────────────────────────────┐
│  QEMU      │ ────────────────► │ GNU GRUB (serial terminal) │
│  -no KVM   │                   │ boots GNU Mach with        │
│  needed    │ ◄──────────────── │ console=com0              │
└────────────┘   serial line    └────────────────────────────┘
       │                                        │
       └──── kernel, boot script and getty all on the serial port ──┘
```

The driver logs in as **root** (the preinstalled images have no root
password) over the Telnet serial socket and can run any script inside
the guest.

## Files

| File            | Role |
|-----------------|------|
| `make_iso.py`   | Minimal El Torito bootable ISO writer (pure stdlib — no mkisofs/xorriso needed), including the boot information table GRUB's `cdboot` requires |
| `hurd-boot.cfg` | GRUB early configuration: serial terminal + the Hurd boot entry with `console=com0` |
| `hurd_vm.py`    | The driver: builds the ISO, boots QEMU, drives the serial console, runs your script in the guest |

## Requirements (host)

- `qemu-system-x86` (KVM used automatically when `/dev/kvm` is available)
- `grub-mkimage` with the i386-pc modules — on Debian/Ubuntu:
  `sudo apt install grub-pc-bin grub-common`
- Python 3.8+

## Usage

```console
$ wget https://cdimage.debian.org/cdimage/ports/latest/hurd-amd64/debian-hurd-amd64-20260314.img.tar.xz
$ tar xJf debian-hurd-amd64-20260314.img.tar.xz

# Boot to a login prompt, log in as root, stop:
$ python3 hurd_vm.py debian-hurd-amd64-20260314.img

# Boot and run a script inside the guest (as root):
$ python3 hurd_vm.py debian-hurd-amd64-20260314.img my-script.sh
```

The guest script runs with `sh`; its output arrives on the serial
line, is echoed by the driver and saved to `serial.log`.  The exit
code of the script becomes the exit code of `hurd_vm.py` — suitable
for CI.

The guest has user-mode networking (`10.0.2.15`, gateway `10.0.2.2`,
DNS `10.0.2.3`), so `apt-get`, `git clone` etc. work inside the VM.

### Example guest script

```sh
#!/bin/sh
set -e
apt-get update
apt-get install -y build-essential
uname -a                    # GNU/Mach...
ls /                        # the translators are here
```

## Adapting to another image

`hurd_vm.py` carries three constants describing the preinstalled
image: `ROOT_PART` (the msdos partition holding the root
filesystem), `FS_UUID` and `KERNEL` (the GNU Mach path).  They
match the official `20260314` amd64 image; for another image, read
its `/boot/grub/grub.cfg` (from inside a normal graphical boot, or
with a GRUB `cat` probe) and update the constants.

## How it works (for the curious)

- **El Torito without ISO tools.** `make_iso.py` writes the
  Primary Volume Descriptor, the Boot Record, the El Torito boot
  catalog (validation + initial entry), the directory and path
  tables, and patches the *boot information table* into the GRUB
  image — the table that GRUB's `cdboot` reads to locate itself on
  the CD (without it: `no boot info`).
- **A standalone GRUB.** `grub-mkimage -O i386-pc-eltorito`
  produces a GRUB with the serial terminal enabled and an embedded
  early configuration that boots the Hurd from the attached disk
  image — the same multiboot entry the image ships, plus
  `console=com0`.
- **`console=com0`.** GNU Mach selects the serial port as its
  console (see `comcnprobe` in GNU Mach's `i386/i386at/com.c`):
  kernel messages, the boot script and the `console` getty all use
  it.  Kernel panics are printed on it too — a first-class
  debugging channel.
- **Driving the line.** QEMU exposes the serial port as a Telnet
  server on localhost; the driver negotiates, waits for the login
  prompt, logs in, feeds the guest script through a heredoc and
  watches for its exit status.

## Known issues

- In some virtualized hosts the boot has been observed to stall at
  `start pci-arbiter:` (the first PCI-touching Hurd server), on
  the same images that boot fine on other QEMU/KVM hosts.  If you
  hit it, first test the raw image with its own graphical boot on
  your host; the stall appears to be host-environment-dependent
  rather than image-dependent.

## License

GPL-3.0-or-later — see `LICENSE`.

```
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Claire Ivanenka <claire@gnu-ai.org>
```
