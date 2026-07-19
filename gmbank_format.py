#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""gmbank.bin binary format, shared by the builder and the reference
renderer (and mirrored by the device loader in C).

Layout (little-endian):

  header:
    0x00  magic 'GMB1'
    0x04  u32 version
    0x08  u32 nregions
    0x0C  u32 table_off      -- region table file offset
    0x10  u32 blob_off       -- ADPCM blob file offset
    0x14  u32 total_size
    0x18  u16 prog_index[130]
          regions of program p (0-127; 128 = drum kit) are table rows
          [prog_index[p], prog_index[p+1])  -- rows sorted by lokey
  region row (56 bytes each):
    see REGION_FMT/REGION_FIELDS

Pitch math (device and reference renderer MUST match):
  ratio = 2^(((note - root) * scale/100 + tune/100 + bend_semis) / 12)
  inc   = ratio * rate / OUT_RATE          (16.16 fixed on device)

Envelope: attack/decay/release are seconds, sustain is linear level;
the device precomputes per-sample coefficients at load time
(attack: env += 1/(a*RR); decay/release: exp(-3/(d*RR)) style, same
as Phase4).  loopend == 0 means one-shot.
"""

import struct

MAGIC = b'GMB1'
VERSION = 1
NPROG_SLOTS = 129           # 128 melodic + 1 drum kit
DRUM_SLOT = 128

HDR_FMT = '<4sIIIII'
HDR_SIZE = struct.calcsize(HDR_FMT)
INDEX_FMT = '<%dH' % (NPROG_SLOTS + 1)
INDEX_SIZE = struct.calcsize(INDEX_FMT)

REGION_FMT = '<BBBBhBBHHIIIhBBfffffII'
REGION_SIZE = struct.calcsize(REGION_FMT)
REGION_FIELDS = [
    'lokey', 'hikey', 'root', 'excl',
    'tune',                 # cents, signed
    'scale',                # scaleTuning (100 = normal)
    'pan',                  # 0-127, 64 = center
    'rate',                 # Hz of stored data
    'flags',                # bit0 = looped
    'length',               # samples
    'loopstart', 'loopend', # samples; loopend 0 = one-shot
    'loop_pred', 'loop_step',
    'vel_exp',              # velocity->amplitude exponent x32 (64 = 2.0).
                            # Programs whose soft velocity layers were
                            # collapsed into the loud sample get a
                            # steeper exponent so low velocities drop to
                            # where the soft layer would have been.
    'gain',                 # linear
    'attack', 'decay', 'sustain', 'release',
    'adpcm_off', 'adpcm_nbytes',
]

FLAG_LOOPED = 1


def pack_region(r):
    return struct.pack(REGION_FMT, *[r[f] for f in REGION_FIELDS])


def unpack_region(data, off):
    vals = struct.unpack_from(REGION_FMT, data, off)
    return dict(zip(REGION_FIELDS, vals))


def write_bank(path, prog_regions):
    """prog_regions: dict slot -> [region dict] (slot 0..128).
    Region dicts carry an extra 'adpcm' bytes field; offsets are
    assigned here."""
    rows = []
    index = []
    blob = bytearray()
    for slot in range(NPROG_SLOTS):
        index.append(len(rows))
        for r in sorted(prog_regions.get(slot, []),
                        key=lambda x: x['lokey']):
            r = dict(r)
            r['adpcm_off'] = len(blob)
            r['adpcm_nbytes'] = len(r['adpcm'])
            blob.extend(r['adpcm'])
            rows.append(r)
    index.append(len(rows))

    table_off = HDR_SIZE + INDEX_SIZE
    blob_off = table_off + len(rows) * REGION_SIZE
    total = blob_off + len(blob)

    with open(path, 'wb') as f:
        f.write(struct.pack(HDR_FMT, MAGIC, VERSION, len(rows),
                            table_off, blob_off, total))
        f.write(struct.pack(INDEX_FMT, *index))
        for r in rows:
            f.write(pack_region(r))
        f.write(blob)
    return total, len(rows)


class Bank:
    """Reader used by the reference renderer and QA."""

    def __init__(self, path):
        self.data = open(path, 'rb').read()
        magic, ver, nreg, table_off, blob_off, total = \
            struct.unpack_from(HDR_FMT, self.data, 0)
        assert magic == MAGIC and ver == VERSION
        assert total == len(self.data)
        self.nregions = nreg
        self.blob_off = blob_off
        self.index = struct.unpack_from(INDEX_FMT, self.data, HDR_SIZE)
        self.regions = [unpack_region(self.data,
                                      table_off + i * REGION_SIZE)
                        for i in range(nreg)]

    def prog_regions(self, slot):
        return self.regions[self.index[slot]:self.index[slot + 1]]

    def find_region(self, slot, note):
        for r in self.prog_regions(slot):
            if r['lokey'] <= note <= r['hikey']:
                return r
        return None

    def adpcm(self, r):
        off = self.blob_off + r['adpcm_off']
        return self.data[off:off + r['adpcm_nbytes']]
