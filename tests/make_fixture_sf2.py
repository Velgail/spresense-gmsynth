#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""Generate a tiny, fully deterministic SoundFont fixture for the
regression suite.

gmbank.bin built from a real font is never committed (it inherits the
font's license), so bank-derived expectations (structure manifest,
event traces) need a font the repo can own.  This one is synthesized
from seeded sines/noise: every GM program maps to one of three small
multisample instruments, plus a GM drum kit with per-key zones and the
open/closed hi-hat exclusive class.  Same script, same bytes, forever.

Writes tests/fixture.sf2 (~600 KB).  Only the chunks sf2parse.py reads
are emitted: phdr/pbag/pgen/inst/ibag/igen/shdr + smpl.
"""

import os
import struct
import sys

import numpy as np

RATE = 22050
OUT = os.path.join(os.path.dirname(__file__), 'fixture.sf2')

G_PAN = 17
G_ATTACK = 34
G_DECAY = 36
G_SUSTAIN = 37
G_RELEASE = 38
G_INSTRUMENT = 41
G_KEYRANGE = 43
G_SAMPLEID = 53
G_MODES = 54
G_EXCLCLASS = 57
G_ROOTKEY = 58

GM_DRUM_KEYS = list(range(35, 82))
OPEN_HAT = 46
CLOSED_HATS = (42, 44)


def tc(seconds):
    """Timecents encoding used by SF2 envelope generators."""
    import math
    return max(-12000, min(8000, int(round(1200 *
                                           math.log2(max(seconds,
                                                         1e-4))))))


def synth_tone(root, harmonics, secs=0.35):
    """Looped tone at the MIDI root's frequency: fundamental + a few
    fixed harmonics, faded in one loop-safe period grid."""
    f = 440.0 * 2.0 ** ((root - 69) / 12.0)
    period = RATE / f
    nper = max(1, int((secs * RATE) / period))
    n = int(round(nper * period))
    t = np.arange(n) / RATE
    x = np.zeros(n)
    for k, a in harmonics:
        x += a * np.sin(2 * np.pi * f * k * t)
    x *= 0.55 / max(1e-9, np.max(np.abs(x)))
    loopstart = int(round(period * (nper // 2)))
    return (x * 32767).astype('<i2'), loopstart, n - 1


def synth_drum(key, seed, secs=0.25):
    rng = np.random.default_rng(1000 + seed)
    n = int(secs * RATE)
    x = rng.standard_normal(n) * np.exp(-np.arange(n) / (0.06 * RATE))
    tone = 60.0 * 2.0 ** ((key - 36) / 24.0)
    x += 0.7 * np.sin(2 * np.pi * tone * np.arange(n) / RATE) * \
        np.exp(-np.arange(n) / (0.09 * RATE))
    x *= 0.5 / max(1e-9, np.max(np.abs(x)))
    return (x * 32767).astype('<i2'), 0, 0        # one-shot


def main():
    samples = []          # (name, pcm, loopstart, loopend, root)

    def add_sample(name, pcm, ls, le, root):
        samples.append((name, pcm, ls, le, root))
        return len(samples) - 1

    # Three melodic timbres, two key splits each (multisample zones)

    timbres = [
        ('bright', [(1, 1.0), (2, 0.5), (3, 0.3), (5, 0.15)]),
        ('mellow', [(1, 1.0), (2, 0.2), (3, 0.05)]),
        ('hollow', [(1, 1.0), (3, 0.4), (5, 0.2)]),
    ]
    mel_zones = []        # per timbre: [(lo, hi, root, sidx)]
    for name, harm in timbres:
        zones = []
        for lo, hi, root in ((0, 59, 48), (60, 127, 72)):
            pcm, ls, le = synth_tone(root, harm)
            sidx = add_sample(f'{name}{root}', pcm, ls, le, root)
            zones.append((lo, hi, root, sidx))
        mel_zones.append(zones)

    drum_sidx = {}
    for key in GM_DRUM_KEYS:
        pcm, ls, le = synth_drum(key, key)
        drum_sidx[key] = add_sample(f'dr{key}', pcm, ls, le, key)

    # ---- instruments ----------------------------------------------------
    # inst records + ibag/igen; zone gen order: keyrange first (SF2
    # spec), sampleID last.

    inst = []             # (name, [zone gen dicts])
    envs = [
        {G_ATTACK: tc(0.005), G_DECAY: tc(0.6), G_SUSTAIN: 200,
         G_RELEASE: tc(0.25)},
        {G_ATTACK: tc(0.04), G_DECAY: tc(1.2), G_SUSTAIN: 50,
         G_RELEASE: tc(0.5)},
        {G_ATTACK: tc(0.01), G_DECAY: tc(0.9), G_SUSTAIN: 350,
         G_RELEASE: tc(0.12)},
    ]
    for ti, (name, _) in enumerate(timbres):
        zones = []
        for (lo, hi, root, sidx) in mel_zones[ti]:
            g = {G_KEYRANGE: lo | (hi << 8)}
            g.update(envs[ti])
            g[G_ROOTKEY] = root
            g[G_MODES] = 1                       # loop continuously
            g[G_SAMPLEID] = sidx
            zones.append(g)
        inst.append((name, zones))

    drum_zones = []
    for key in GM_DRUM_KEYS:
        g = {G_KEYRANGE: key | (key << 8)}
        if key == OPEN_HAT or key in CLOSED_HATS:
            g[G_EXCLCLASS] = 1
        g[G_ROOTKEY] = key
        g[G_SAMPLEID] = drum_sidx[key]
        drum_zones.append(g)
    inst.append(('drumkit', drum_zones))
    drum_iidx = len(inst) - 1

    # ---- presets --------------------------------------------------------
    # All 128 GM programs (bank 0) -> timbre prog % 3; bank 128 preset
    # 0 -> drum kit.

    presets = []          # (name, bank, prog, iidx)
    for prog in range(128):
        presets.append((f'p{prog:03d}', 0, prog, prog % 3))
    presets.append(('drums', 128, 0, drum_iidx))

    # ---- serialize ------------------------------------------------------

    smpl = bytearray()
    shdr = bytearray()
    for (name, pcm, ls, le, root) in samples:
        start = len(smpl) // 2
        smpl += pcm.tobytes()
        smpl += b'\x00' * (2 * 46)               # spec: 46-pt guard
        end = start + len(pcm)
        shdr += struct.pack('<20sIIIIIBbHH', name.encode()[:20],
                            start, end, start + ls, start + le,
                            RATE, root, 0, 0, 1)
    shdr += struct.pack('<20sIIIIIBbHH', b'EOS', 0, 0, 0, 0, 0, 0,
                        0, 0, 0)

    pbag = bytearray()
    pgen = bytearray()
    phdr = bytearray()
    for (name, bank, prog, iidx) in presets:
        phdr += struct.pack('<20sHHHIII', name.encode()[:20], prog,
                            bank, len(pbag) // 4, 0, 0, 0)
        pbag += struct.pack('<HH', len(pgen) // 4, 0)
        pgen += struct.pack('<HH', G_INSTRUMENT, iidx)
    phdr += struct.pack('<20sHHHIII', b'EOP', 0, 0, len(pbag) // 4,
                        0, 0, 0)
    pbag += struct.pack('<HH', len(pgen) // 4, 0)

    ibag = bytearray()
    igen = bytearray()
    inst_b = bytearray()
    for (name, zones) in inst:
        inst_b += struct.pack('<20sH', name.encode()[:20],
                              len(ibag) // 4)
        for g in zones:
            ibag += struct.pack('<HH', len(igen) // 4, 0)
            for oper in sorted(g, key=lambda o: (o != G_KEYRANGE,
                                                 o == G_SAMPLEID)):
                igen += struct.pack('<HH', oper, g[oper] & 0xffff)
    inst_b += struct.pack('<20sH', b'EOI', len(ibag) // 4)
    ibag += struct.pack('<HH', len(igen) // 4, 0)

    def chunk(cid, body):
        pad = b'\x00' if len(body) & 1 else b''
        return cid + struct.pack('<I', len(body)) + bytes(body) + pad

    def list_chunk(kind, subchunks):
        body = kind + b''.join(subchunks)
        return chunk(b'LIST', body)

    sdta = list_chunk(b'sdta', [chunk(b'smpl', smpl)])
    pdta = list_chunk(b'pdta', [chunk(b'phdr', phdr),
                                chunk(b'pbag', pbag),
                                chunk(b'pgen', pgen),
                                chunk(b'inst', inst_b),
                                chunk(b'ibag', ibag),
                                chunk(b'igen', igen),
                                chunk(b'shdr', shdr)])
    body = b'sfbk' + sdta + pdta
    with open(OUT, 'wb') as f:
        f.write(b'RIFF' + struct.pack('<I', len(body)) + body)
    print(f'fixture: {len(samples)} samples, {len(inst)} instruments, '
          f'{len(presets)} presets -> {OUT} '
          f'({os.path.getsize(OUT)} bytes)')


if __name__ == '__main__':
    main()
    sys.exit(0)
