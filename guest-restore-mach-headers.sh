#!/bin/sh
# Phase A2: restore the missing hand-written Mach user headers.
#
# Findings so far:
#   - /usr/include/mach/mach_host.h comes from libc0.3-dev (glibc),
#     regenerated 2026-09, and references processor_name_array_t;
#   - /usr/include/mach/mach_types.h (which defines that type
#     upstream, in gnumach's hand-written include/mach/) does not
#     exist on the image: no package owns it, nothing installs it.
#
# This script lists what each package actually ships, then restores
# every missing include/mach header, preferring the distro packages
# (extract them) and falling back to the GNU Mach sources.

echo "=== guest: $(uname -a)"
echo "=== state: files in /usr/include/mach"
ls /usr/include/mach | wc -l
ls /usr/include/mach | head -5

echo "=== what do the packages ship under include/mach?"
mkdir -p /tmp/debcheck && cd /tmp/debcheck
rm -f *.deb
apt-get download libc0.3-dev gnumach-dev > /dev/null 2>&1 || true
for d in *.deb; do
    [ -e "$d" ] || continue
    echo "--- $d"
    dpkg-deb -c "$d" | awk '{print $6}' | grep "include/mach" | sed 's|.*/include/mach/||' | sort | tr '\n' ' ' | head -c 600
    echo
done

echo "=== restoring the missing headers from the packages, then the sources"
cd /tmp/debcheck
for d in *.deb; do
    [ -e "$d" ] || continue
    rm -rf ex && mkdir ex && dpkg-deb -x "$d" ex
    ( cd ex && find usr/include/mach -type f 2>/dev/null ) | while read -r f; do
        rel="/$f"
        if [ ! -e "$rel" ]; then
            mkdir -p "$(dirname "$rel")"
            cp "/tmp/debcheck/ex/$f" "$rel"
            echo "restored from package: $rel"
        fi
    done
done

if [ ! -e /usr/include/mach/mach_types.h ]; then
    echo "=== mach_types.h still missing: fetching the GNU Mach sources"
    cd /tmp/debcheck
    rm -rf gnumach
    git clone -q --depth 1 https://git.savannah.gnu.org/git/hurd/gnumach.git
    cd gnumach
    find include/mach -name '*.h' -type f | while read -r f; do
        rel="/usr/include/${f#include/}"
        if [ ! -e "$rel" ]; then
            mkdir -p "$(dirname "$rel")"
            cp "$f" "$rel"
            echo "restored from gnumach sources: $rel"
        fi
    done
fi

echo "=== verify: the typedef is where mig expects it"
grep -n "processor_name_array_t" /usr/include/mach/mach_types.h || echo "STILL MISSING"

echo "=== retest: include <hurd.h> with -std=c23"
printf '#include <hurd.h>\nint main(void){return 0;}\n' > /tmp/t.c
if gcc -std=c23 -c /tmp/t.c -o /tmp/t.o 2>/tmp/err; then
    echo "HURD_H_C23 OK"
else
    echo "HURD_H_C23 FAIL"
    head -10 /tmp/err
fi
echo "=== done"
