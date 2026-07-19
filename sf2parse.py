#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""SF2 parsing for the gmsynth bank builder.

Generalized from spusynth/sgm_extract.py, keeping all its hard-won
lessons: gen48 initialAttenuation is SIGNED (SGM stores boosts as
negatives), gen51/52 are coarse/fine tune, gen56 scaleTuning, preset
zone generators ADD to instrument zone generators, and global
instrument zones must be merged into every sample zone.
"""

import struct

import numpy as np

G_PAN = 17
G_ATTACK = 34
G_HOLD = 35
G_DECAY = 36
G_SUSTAIN = 37
G_RELEASE = 38
G_INSTRUMENT = 41
G_KEYRANGE = 43
G_VELRANGE = 44
G_ATTEN = 48
G_COARSE = 51
G_FINE = 52
G_SAMPLEID = 53
G_MODES = 54
G_SCALETUNE = 56
G_EXCLCLASS = 57
G_ROOTKEY = 58


class SF2:
    def __init__(self, path):
        data = open(path, 'rb').read()
        assert data[:4] == b'RIFF' and data[8:12] == b'sfbk'
        self.data = data
        self.chunks = {}
        self._walk(12, len(data))
        self.phdr = self._arr('phdr', '<20sHHHIII',
                              ['name', 'preset', 'bank', 'bagidx',
                               'library', 'genre', 'morph'])
        self.pbag = self._arr('pbag', '<HH', ['genidx', 'modidx'])
        self.pgen = self._arr('pgen', '<HH', ['oper', 'amount'])
        self.inst = self._arr('inst', '<20sH', ['name', 'bagidx'])
        self.ibag = self._arr('ibag', '<HH', ['genidx', 'modidx'])
        self.igen = self._arr('igen', '<HH', ['oper', 'amount'])
        self.shdr = self._arr('shdr', '<20sIIIIIBbHH',
                              ['name', 'start', 'end', 'loopstart',
                               'loopend', 'rate', 'origpitch',
                               'correction', 'link', 'type'])
        self.smpl_off, self.smpl_sz = self.chunks['smpl']

    def _walk(self, pos, end):
        while pos + 8 <= end:
            cid = self.data[pos:pos + 4]
            sz = struct.unpack('<I', self.data[pos + 4:pos + 8])[0]
            body = pos + 8
            if cid == b'LIST':
                self._walk(body + 4, body + sz)
            else:
                self.chunks[cid.decode('latin1')] = (body, sz)
            pos = body + sz + (sz & 1)

    def _arr(self, name, fmt, fields):
        off, sz = self.chunks[name]
        rec = struct.calcsize(fmt)
        out = []
        for p in range(off, off + sz - rec + 1, rec):
            out.append(dict(zip(fields,
                                struct.unpack_from(fmt, self.data, p))))
        return out

    def find_preset(self, bank, prog):
        for i, p in enumerate(self.phdr[:-1]):
            if p['bank'] == bank and p['preset'] == prog:
                return i
        return None

    def preset_insts(self, pidx):
        """[(inst_index, preset_zone_gens)] for one preset."""
        lo = self.phdr[pidx]['bagidx']
        hi = self.phdr[pidx + 1]['bagidx']
        out = []
        glob = {}
        for b in range(lo, hi):
            g0 = self.pbag[b]['genidx']
            g1 = self.pbag[b + 1]['genidx']
            gens = {g['oper']: g['amount'] for g in self.pgen[g0:g1]}
            if G_INSTRUMENT in gens:
                merged = dict(glob)
                merged.update(gens)
                out.append((merged[G_INSTRUMENT], merged))
            elif not out:
                glob = gens
        return out

    def inst_zones(self, iidx):
        lo = self.inst[iidx]['bagidx']
        hi = self.inst[iidx + 1]['bagidx']
        zones = []
        glob = {}
        for b in range(lo, hi):
            g0 = self.ibag[b]['genidx']
            g1 = self.ibag[b + 1]['genidx']
            gens = {g['oper']: g['amount'] for g in self.igen[g0:g1]}
            if G_SAMPLEID in gens:
                merged = dict(glob)
                merged.update(gens)
                zones.append(merged)
            elif not zones:
                glob = gens
        return zones

    def pcm(self, sidx):
        """Sample data as float32 numpy array (raw int16 scale)."""
        s = self.shdr[sidx]
        off = self.smpl_off + s['start'] * 2
        n = s['end'] - s['start']
        a = np.frombuffer(self.data, dtype='<i2', count=n, offset=off)
        return a.astype(np.float32)

    def zone_pool(self, bank, prog):
        """All playable zones of a preset, with preset-level keyrange
        intersection applied.  Returns [(lo, hi, zone_gens,
        preset_gens)], right-channel zones of stereo pairs dropped."""
        pidx = self.find_preset(bank, prog)
        if pidx is None:
            return []
        pool = []
        for iidx, pgens in self.preset_insts(pidx):
            pklo, pkhi = keyrange(pgens)
            for z in self.inst_zones(iidx):
                if self.shdr[z[G_SAMPLEID]]['type'] == 2:
                    continue
                zlo, zhi = keyrange(z)
                lo = max(zlo, pklo)
                hi = min(zhi, pkhi)
                if lo > hi:
                    continue
                pool.append((lo, hi, z, pgens))
        return pool


def s16(v):
    return v - 65536 if v >= 32768 else v


def keyrange(gens):
    if G_KEYRANGE in gens:
        v = gens[G_KEYRANGE]
        return (v & 0xff, v >> 8)
    return (0, 127)


def tc2sec(v, default):
    if v is None:
        return default
    return 2.0 ** (s16(v) / 1200.0)


# IMA ADPCM (identical tables to the device decoder) ----------------------

IMA_STEPS = np.array([
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37,
    41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173,
    190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658,
    724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894,
    6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289,
    16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767], dtype=np.int32)
IMA_INDEX = [-1, -1, -1, -1, 2, 4, 6, 8]

_STEPS = IMA_STEPS.tolist()


def adpcm_encode(pcm, snapshot_at=None):
    """Encode int-valued iterable to 4-bit IMA nibbles.  Returns
    (nibbles list, (pred, stepidx) snapshot before sample snapshot_at)."""
    pred = 0
    stepidx = 0
    nibbles = []
    snapshot = (0, 0)
    steps = _STEPS
    idxtab = IMA_INDEX
    for i, s in enumerate(pcm):
        if snapshot_at is not None and i == snapshot_at:
            snapshot = (pred, stepidx)
        step = steps[stepidx]
        diff = int(s) - pred
        nib = 0
        if diff < 0:
            nib = 8
            diff = -diff
        d = step
        if diff >= d:
            nib |= 4
            diff -= d
        d >>= 1
        if diff >= d:
            nib |= 2
            diff -= d
        d >>= 1
        if diff >= d:
            nib |= 1
        delta = step >> 3
        if nib & 4:
            delta += step
        if nib & 2:
            delta += step >> 1
        if nib & 1:
            delta += step >> 2
        if nib & 8:
            pred -= delta
        else:
            pred += delta
        if pred > 32767:
            pred = 32767
        elif pred < -32768:
            pred = -32768
        si = stepidx + idxtab[nib & 7]
        stepidx = 0 if si < 0 else (88 if si > 88 else si)
        nibbles.append(nib)
    return nibbles, snapshot


def adpcm_decode(nibbles, pred=0, stepidx=0):
    """Decode nibbles to a float32 numpy array."""
    out = np.empty(len(nibbles), dtype=np.float32)
    steps = _STEPS
    idxtab = IMA_INDEX
    for i, nib in enumerate(nibbles):
        step = steps[stepidx]
        delta = step >> 3
        if nib & 4:
            delta += step
        if nib & 2:
            delta += step >> 1
        if nib & 1:
            delta += step >> 2
        if nib & 8:
            pred -= delta
        else:
            pred += delta
        if pred > 32767:
            pred = 32767
        elif pred < -32768:
            pred = -32768
        si = stepidx + idxtab[nib & 7]
        stepidx = 0 if si < 0 else (88 if si > 88 else si)
        out[i] = pred
    return out


def unpack_nibbles(data):
    """bytes -> nibble list (low nibble first, device order)."""
    a = np.frombuffer(data, dtype=np.uint8)
    out = np.empty(len(a) * 2, dtype=np.uint8)
    out[0::2] = a & 0x0F
    out[1::2] = a >> 4
    return out.tolist()


def pack_nibbles(nibbles):
    if len(nibbles) & 1:
        nibbles = nibbles + [0]
    a = np.array(nibbles, dtype=np.uint8)
    return (a[0::2] | (a[1::2] << 4)).tobytes()


def measure_pitch_cents(x, sr, f_expect):
    """Deviation (cents) of x's pitch from f_expect, via the spectral
    peak within +/-100c of the expected fundamental (or a higher
    harmonic when the fundamental is too low for the window).  Returns
    None when no confident peak exists (unpitched material)."""
    import math
    x = np.asarray(x, dtype=np.float64)
    x = x - x.mean()
    if len(x) < 256 or float(np.max(np.abs(x))) < 1e-5:
        return None
    n = len(x)
    win = np.hanning(n)
    nfft = 1
    while nfft < 8 * n:
        nfft *= 2
    mag = np.abs(np.fft.rfft(x * win, nfft))
    df = sr / nfft
    k = max(1, int(math.ceil(150.0 / f_expect)))
    for mult in (k, 2 * k):
        fc = mult * f_expect
        lo = int(fc * 2 ** (-100 / 1200.0) / df)
        hi = int(fc * 2 ** (100 / 1200.0) / df) + 1
        if hi >= len(mag) - 1 or lo < 1 or hi <= lo + 2:
            continue
        i = int(np.argmax(mag[lo:hi])) + lo
        if i <= lo + 1 or i >= hi - 2:
            continue    # peak pinned to the window edge: not a peak
        y0, y1, y2 = mag[i - 1], mag[i], mag[i + 1]
        denom = (y0 - 2 * y1 + y2)
        d = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-12 else 0.0
        f = (i + max(-0.5, min(0.5, d))) * df
        floor = float(np.median(mag[max(1, int(fc * 0.5 / df)):
                                    int(fc * 3.0 / df)]))
        if floor > 0 and mag[i] / floor >= 4.0:
            return 1200.0 * math.log2(f / fc)
    return None


def resample(pcm, ratio):
    """Linear-interpolation resample of a float32 array by ratio<=1."""
    if ratio >= 0.999:
        return pcm.copy()
    n = int(len(pcm) * ratio)
    x = np.arange(n, dtype=np.float64) / ratio
    i0 = x.astype(np.int64)
    i1 = np.minimum(i0 + 1, len(pcm) - 1)
    f = (x - i0).astype(np.float32)
    return pcm[i0] * (1.0 - f) + pcm[i1] * f
