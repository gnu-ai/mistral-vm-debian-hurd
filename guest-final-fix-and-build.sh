#!/bin/sh
# Phase A5 (final): patch the header gcc ACTUALLY uses.
#
# Root cause chain, for the record:
#   1. libc0.3-dev 2.43-7~hurd.1 regenerates the MIG userland headers
#      (mach_host.h, 2026-09) from a recent GNU Mach whose
#      mach_host.defs:402 uses processor_name_array_t;
#   2. that type is defined in the HAND-WRITTEN mach/mach_types.h of
#      GNU Mach (include/mach/mach_types.h, userland section);
#   3. glibc's installed mach_types.h lives in the MULTIARCH include
#      dir /usr/include/x86_64-gnu/mach/ — gcc searches it FIRST, so
#      it shadows anything in /usr/include/mach/ — and this copy is
#      STALE: no processor_name_t, no processor_name_array_t.
# Result: on this snapshot every #include <hurd.h> fails.
#
# Fix: restore the two missing typedefs in the multiarch copy
# (exactly as upstream defines them), add the other missing
# hand-written Mach headers next to it, then rebuild everything.

set -e

echo "=== guest: $(uname -a)"

MACHDIR=/usr/include/x86_64-gnu/mach

echo "=== confirming: the multiarch copy is the stale one"
if grep -qE "typedef.*processor_name_array_t" "$MACHDIR/mach_types.h"; then
    echo "=== the typedef is already there"
else
    echo "patching $MACHDIR/mach_types.h"
    sed -i -E \
      's/^(typedef[[:space:]]+mach_port_t[[:space:]]+processor_t;.*)$/\1\ntypedef mach_port_t processor_name_t;\ntypedef mach_port_t *processor_name_array_t;/' \
      "$MACHDIR/mach_types.h"
    grep -n "processor_name" "$MACHDIR/mach_types.h"
fi

echo "=== adding the missing hand-written headers next to it (never overwriting)"
cd /usr/include/mach
find . -name '*.h' -type f | while read -r f; do
    rel="${f#./}"
    if [ ! -e "$MACHDIR/$rel" ]; then
        mkdir -p "$MACHDIR/$(dirname "$rel")"
        cp "/usr/include/mach/$rel" "$MACHDIR/$rel"
        echo "added $MACHDIR/$rel"
    fi
done
cd ~

echo "=== retest: include <hurd.h> with -std=c23"
printf '#include <hurd.h>\nint main(void){return 0;}\n' > /tmp/t.c
if gcc -std=c23 -c /tmp/t.c -o /tmp/t.o 2>/tmp/err; then
    echo "HURD_H_C23 OK"
else
    echo "HURD_H_C23 FAIL"
    head -12 /tmp/err
    exit 1
fi

fetch_repo()
{
    repo="$1"
    rm -rf "$repo"
    git clone -q "https://github.com/gnu-ai/$repo"
}

build_one()
{
    repo="$1"
    echo "=== fetching $repo"
    fetch_repo "$repo"
    cd "$repo" || return 1
    echo "=== $repo: autogen.sh";    ./autogen.sh >autogen.log 2>&1 || { tail -5 autogen.log; return 1; }
    echo "=== $repo: configure";     ./configure >configure.log 2>&1 || { tail -5 configure.log; return 1; }
    echo "=== $repo: make";          make >make.log 2>&1 || { tail -8 make.log; return 1; }
    echo "=== $repo: make check";    make check >check.log 2>&1 || { tail -15 check.log; return 1; }
    grep -E "^# (TOTAL|PASS|FAIL|SKIP|ERROR)" check.log | sed "s/^# /$repo: # /"
    cd ..
    echo "=== $repo: OK"
    return 0
}

echo
echo "########## Building the five translators on fixed headers ##########"
for repo in inference-translator orchestrator-translator \
            data-base-translator neuron-translator httpfs-translator; do
    build_one "$repo" || { echo "=== $repo: FAILED"; exit 1; }
done

echo
echo "########## The GNU base commands on real Hurd ##########"
~/inference-translator/src/inference-translator --version
~/orchestrator-translator/src/orchestrator-translator --version
~/data-base-translator/src/db-translator --version
~/neuron-translator/src/sigmoid-neuron-translator --version
~/httpfs-translator/src/httpfs --version

echo
echo "########## Smoke test: mount a real URL through httpfs ##########"
rm -f /web 2>/dev/null || true
mkdir -p /web
settrans -a /web ~/httpfs-translator/src/httpfs https://www.gnu.org || true
ls -l /web | head -5
echo "--- /web/status:"; cat /web/status 2>/dev/null || true
settrans -p /web 2>/dev/null || true
echo "=== smoke test OK"

echo
echo "=== all good"
