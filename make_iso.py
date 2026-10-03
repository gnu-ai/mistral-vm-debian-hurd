#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Claire Ivanenka <claire@gnu-ai.org>

"""Minimal El Torito bootable ISO writer (pure stdlib).

Layout (2048-byte sectors):
  0..15   system area (zeros)
  16      Primary Volume Descriptor
  17      Boot Record VD ("EL TORITO SPECIFICATION")
  18      Volume Descriptor Set Terminator
  19..20  El Torito boot catalog (validation + initial entry)
  21..    the boot image, padded to a sector
  N       root directory (. and ..)
  N+1     path tables L and M
"""
import struct, sys

SECT = 2048
boot_image = open(sys.argv[1], 'rb').read()
out = sys.argv[2]

img_sects = (len(boot_image) + SECT - 1) // SECT
CAT_LBA = 19
IMG_LBA = 21
ROOT_LBA = IMG_LBA + img_sects
PT_LBA = ROOT_LBA + 1
TOTAL = PT_LBA + 2 + 1

def be16(v): return struct.pack('<H', v) + struct.pack('>H', v)
def be32(v): return struct.pack('<I', v) + struct.pack('>I', v)

def dir_record(ext_lba, size, flags=2, name=b'\x00'):
    r = bytearray(34)
    r[0] = 34
    r[2:10] = be32(ext_lba)
    r[10:18] = be32(size)
    r[25] = flags                      # file flags: 2 = directory
    r[28] = 0                          # interleave unit
    r[29] = 0                          # interleave gap
    r[30:32] = struct.pack('<H', 1)    # volume sequence number
    r[32] = len(name)                  # name length
    r[33:33 + len(name)] = name
    return bytes(r)

# ---- Primary Volume Descriptor --------------------------------------
pvd = bytearray(SECT)
pvd[0:7] = b'\x01CD001\x01'
pvd[7:39] = b' '.ljust(32, b' ')            # system identifier
pvd[39:71] = b'HTTPFS_CI'.ljust(32, b' ')   # volume identifier
pvd[80:88] = be32(TOTAL)                    # volume space size
pvd[120:124] = be16(1)                      # volume set size
pvd[124:128] = be16(1)                     # volume sequence number
pvd[128:132] = be16(SECT)                   # logical block size
pvd[132:140] = be32(10)                     # path table size
pvd[144:148] = struct.pack('<I', PT_LBA)    # L path table location
pvd[152:156] = struct.pack('>I', PT_LBA + 1)  # M path table location
pvd[160:194] = dir_record(ROOT_LBA, SECT)   # root directory record

# ---- Boot Record Volume Descriptor ----------------------------------
br = bytearray(SECT)
br[0:7] = b'\x00CD001\x01'
br[7:39] = b'EL TORITO SPECIFICATION'.ljust(32, b'\x00')
br[71:79] = be32(CAT_LBA)                   # boot catalog location

# ---- Volume Descriptor Set Terminator -------------------------------
term = bytearray(SECT)
term[0:7] = b'\xffCD001\x01'

# ---- El Torito boot catalog -----------------------------------------
cat = bytearray(2 * SECT)
# Byte-for-byte copy of a proven working validation entry (from an
# official Debian ISO): platform 80x86, empty id string, zero
# checksum, key word 0x55AA little-endian at offset 28.
val = bytearray.fromhex(
    '01000000000000000000000000000000000000000000000000000000aa5555aa')
cat[0:32] = bytes(val)

ini = bytearray(32)
ini[0] = 0x88                               # bootable
ini[1] = 0                                  # media: no emulation
ini[2:4] = struct.pack('<H', 0)             # load segment (0 -> 0x7C00)
ini[4] = 0                                  # system type
ini[6:8] = struct.pack('<H', 4)             # sectors to load (512-byte)
ini[8:12] = struct.pack('<I', IMG_LBA)      # boot image LBA
cat[32:64] = bytes(ini)

# ---- Root directory --------------------------------------------------
root = bytearray(SECT)
root[0:34] = dir_record(ROOT_LBA, SECT)
root[34:68] = dir_record(ROOT_LBA, SECT, name=b'\x01')

# ---- Path tables ------------------------------------------------------
ptl = bytearray(SECT)
ptl[0] = 1                                  # name length
ptl[1] = 1                                  # extended attribute length
ptl[2:6] = struct.pack('<I', ROOT_LBA)
ptl[6:8] = struct.pack('<H', 1)             # parent: root
ptm = bytearray(SECT)
ptm[0] = 1
ptm[1] = 1
ptm[2:6] = struct.pack('>I', ROOT_LBA)
ptm[6:8] = struct.pack('>H', 1)

# ---- El Torito Boot Information Table (patched into the image) ----
# GRUB's cdboot reads this at offset 8 of the boot image (the space
# is reserved there); without it cdboot fails with "no boot info".
img = bytearray(boot_image)
struct.pack_into('<I', img, 8, 16)           # bi_pvd: LBA of the PVD
struct.pack_into('<I', img, 12, IMG_LBA)     # bi_file: LBA of the image
struct.pack_into('<I', img, 16, len(img))   # bi_length
struct.pack_into('<I', img, 20, 0)           # bi_csum (computed below)
img[24:64] = bytes(40)                      # reserved (code resumes at offset 64!)
csum = 0
for i in range(0, len(img) - 3, 4):
    csum = (csum + struct.unpack_from('<I', img, i)[0]) & 0xFFFFFFFF
struct.pack_into('<I', img, 20, csum)
boot_image = bytes(img)
img_sects = (len(boot_image) + SECT - 1) // SECT

iso = bytearray()
iso += bytes(16 * SECT)                     # system area
iso += pvd + br + term                      # LBA 16, 17, 18
iso += cat                                  # LBA 19, 20
iso += boot_image + bytes(img_sects * SECT - len(boot_image))  # LBA 21..
iso += root                                 # root directory
iso += ptl + ptm                            # path tables

open(out, 'wb').write(iso)
print(f"ISO {out}: {len(iso) // SECT} sectors, image at LBA {IMG_LBA}")
