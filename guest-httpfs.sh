#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Claire Ivanenka <claire@gnu-ai.org>

# Example guest script for hurd_vm.py: build and test the
# httpfs-translator (https://github.com/gnu-ai/httpfs-translator)
# on real GNU/Hurd, then exercise it through a real translator.
#
#   python3 hurd_vm.py <image> guest-httpfs.sh
#
# Runs as root inside the guest; the exit code of this script
# becomes the exit code of the driver.

set -e

echo "=== guest: $(uname -a)"

echo "=== installing the build dependencies"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential autoconf automake pkg-config \
    libcurl4-openssl-dev libmicrohttpd-dev git ca-certificates

echo "=== fetching the sources"
rm -rf httpfs-translator
git clone https://github.com/gnu-ai/httpfs-translator
cd httpfs-translator

echo "=== building"
./autogen.sh
./configure
make

echo "=== unit tests"
make check

echo "=== smoke test: mount a real URL through the translator"
settrans -a /web ~/httpfs-translator/src/httpfs https://www.gnu.org
ls -l /web | head -5
settrans -a /web
echo "=== all good"
