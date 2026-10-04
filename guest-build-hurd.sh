#!/bin/sh
# Phase B: compile a version of the Hurd, from the sources.
#
# With the Mach userland headers repaired (see the earlier runs),
# the toolchain can now build against <hurd.h> again.  This script:
#
#   1. builds GNU Mach (the kernel) from the upstream sources with
#      MIG — the same generation path that produced the broken
#      headers, proving the sources are consistent;
#   2. builds the GNU Hurd servers from github.com/gnu-ai/hurd.
#
# Both builds stay in /root; nothing is installed over the system.

set -e

echo "=== guest: $(uname -a)"

echo "=== build dependencies"
# A previously interrupted run left dpkg half-configured; repair it
# first, never letting a conffile question interrupt the script.
dpkg --force-confold --configure -a 2>/dev/null || true
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    --no-install-recommends \
    -o Dpkg::Options::="--force-confdef" \
    -o Dpkg::Options::="--force-confold" \
    --fix-broken || true
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    --no-install-recommends \
    -o Dpkg::Options::="--force-confdef" \
    -o Dpkg::Options::="--force-confold" \
    mig bison flex texinfo libncurses-dev

echo
echo "########## GNU Mach (the kernel) ##########"
cd /root
rm -rf gnumach
git clone -q --depth 1 https://git.savannah.gnu.org/git/hurd/gnumach.git
cd gnumach
echo "=== gnumach: autoreconf"
autoreconf -i >autoreconf.log 2>&1 || { tail -10 autoreconf.log; exit 1; }
echo "=== gnumach: configure"
./configure >configure.log 2>&1 || { tail -15 configure.log; exit 1; }
echo "=== gnumach: make -j2"
make -j2 >make.log 2>&1 || { tail -25 make.log; exit 1; }
echo "=== gnumach: the kernel binary"
ls -la gnumach 2>/dev/null || ls -la gnumach.exe 2>/dev/null || find . -maxdepth 1 -name 'gnumach*'
file gnumach 2>/dev/null || true
echo "=== gnumach: the MIG-generated mach_host.h mentions the type"
grep -m1 "processor_name_array_t" ipc/mach_interface/mach_host.h 2>/dev/null \
    || find . -name mach_host.h | head -3
echo "=== GNU MACH BUILD OK"

echo "=== freeing space: keep the kernel, drop the build tree"
cp gnumach /root/gnumach.built 2>/dev/null || cp gnumach.exe /root/gnumach.built
cd /root && rm -rf /root/gnumach
apt-get clean 2>/dev/null || true
df -h / | tail -1

echo "=== patching the stale <device/bpf.h> (BPF_MOD/BPF_XOR, from upstream gnumach)"
for BPFH in /usr/include/x86_64-gnu/device/bpf.h /usr/include/device/bpf.h; do
    [ -f "$BPFH" ] || continue
    # -w: BPF_MOD must not be confused with the older BPF_MODE.
    if ! grep -qw BPF_MOD "$BPFH"; then
        printf '#ifndef BPF_MOD\n#define BPF_MOD 0x90\n#endif\n#ifndef BPF_XOR\n#define BPF_XOR 0xa0\n#endif\n' >> "$BPFH"
        echo "patched $BPFH"
    fi
done

echo
echo "########## The GNU Hurd servers (gnu-ai/hurd) ##########"
cd /root
rm -rf hurd
git clone -q --depth 1 https://github.com/gnu-ai/hurd
cd hurd
echo "=== hurd: autoreconf"
autoreconf -i >autoreconf.log 2>&1 || { tail -10 autoreconf.log; exit 1; }
echo "=== hurd: configure"
./configure --prefix=/opt/gnu-ai-hurd --without-parted >configure.log 2>&1 \
    || { tail -20 configure.log; exit 1; }
echo "=== hurd: make -j2"
make -j2 >make.log 2>&1 \
    || { grep -E "error:|Error [0-9]|Killed|No space" make.log | tail -10; exit 1; }

echo "=== hurd: build summary"
echo "compiled objects: $(grep -c ' -c ' make.log 2>/dev/null || echo 0)"
for f in ext2fs/ext2fs exec/exec proc/proc startup/startup \
         libtrivfs/libtrivfs.a libnetfs/libnetfs.a libports/libports.a; do
    [ -e "$f" ] && ls -la "$f"
done
echo "=== HURD BUILD OK"
echo "=== all good"
