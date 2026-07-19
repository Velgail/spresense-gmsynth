#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""Reference renderer: gmbank.bin + SMF -> 48kHz stereo WAV + stats.

Models the planned device engine exactly where it matters:
  - same pitch math as the device (gmbank_format.py docstring)
  - same ADPCM data (decoded once per region, like the worker decodes
    incrementally), same linear interpolation
  - same envelope shapes (linear attack, exponential decay/release with
    the Phase4 coefficient formulas)
  - same voice topology: 4 workers x 16 voices, least-loaded worker
    allocation, steal lowest-envelope (releasing preferred)
  - same echo bus (80ms, feedback 0.40, one-pole LPF approximated by a
    truncated exponential FIR)

GM handling: CC1 vibrato is not rendered (stats-neutral); CC91 scales
the echo send per channel; CC64/120/121/123, RPN0 bend range, drum
exclusive classes are implemented.

Usage: gmrender.py <gmbank.bin> <song.mid> [--out out.wav]
                   [--json stats.json] [--max-s N]
"""

import json
import math
import sys
import wave

import numpy as np

import smf
from gmbank_format import Bank, DRUM_SLOT, FLAG_LOOPED
from sf2parse import adpcm_decode, unpack_nibbles

OUT_RATE = 48000
NWORKERS = 4
VOICES_PER_WORKER = 16
BLOCK = 512
MASTER_GAIN = 0.45

ECHO_DELAY = int(0.080 * OUT_RATE)
ECHO_FEEDBACK = 0.40
ECHO_MIX = 0.60
ECHO_LPF = 0.30
LPF_TAPS = 24

ENV_FLOOR = 1e-3
CHOKE_K = 0.995 ** (OUT_RATE / 32000.0)   # Phase4 hi-hat choke, rate-scaled

DEFAULT_REVERB = 40


class ChState:
    __slots__ = ('prog', 'vol', 'exp', 'pan', 'bend', 'bendrange',
                 'sus', 'rev', 'rpn', 'bank_msb')

    def __init__(self):
        self.reset()

    def reset(self):
        self.prog = 0
        self.vol = 100
        self.exp = 127
        self.pan = 64
        self.bend = 0
        self.bendrange = 2.0
        self.sus = False
        self.rev = DEFAULT_REVERB
        self.rpn = None
        self.bank_msb = 0

    def gain(self):
        # GM volume law: dB = 40*log10(v/127), i.e. squared in
        # amplitude, for both CC7 and CC11 (linear was measured +8dB
        # hot on quiet channels vs fluidsynth)

        v = (self.vol / 127.0) * (self.exp / 127.0)
        return v * v

    def bend_factor(self):
        return 2.0 ** (self.bend / 8192.0 * self.bendrange / 12.0)


class RegionData:
    """Decoded PCM + derived envelope coefficients for one bank region."""

    def __init__(self, bank, reg):
        self.reg = reg
        nib = unpack_nibbles(bank.adpcm(reg))[:reg['length']]
        pcm = adpcm_decode(nib) * (1.0 / 32768.0)
        self.looped = (reg['flags'] & FLAG_LOOPED) != 0
        self.ls = reg['loopstart']
        self.le = reg['loopend']
        if self.looped:
            pcm = np.append(pcm, pcm[self.ls])
        else:
            pcm = np.append(pcm, 0.0)
        self.pcm = pcm.astype(np.float32)
        self.att_n = max(1, int(reg['attack'] * OUT_RATE))
        d = reg['decay']
        self.dec_k = math.exp(-3.0 / (d * OUT_RATE)) if d > 0.001 else 0.0
        self.sus = reg['sustain']
        self.rel_k = math.exp(-5.0 / (max(reg['release'], 0.01) * OUT_RATE))
        self.base_semis_off = reg['tune'] / 100.0
        self.root = reg['root']
        self.scale = reg['scale'] / 100.0
        self.rate = reg['rate']
        self.gain = reg['gain']
        self.pan = reg['pan']
        self.excl = reg['excl']
        ve = reg.get('vel_exp', 64)
        self.vel_exp = (ve / 32.0) if ve > 0 else 2.0


class Voice:
    __slots__ = ('rd', 'ch', 'note', 'pos', 'base_inc', 'inc', 'vgain',
                 'phase', 'env', 'att_done', 'rel_k', 'sus_held', 'dead',
                 'is_drum', 'born')

    def __init__(self, rd, ch, note, vel, bend_factor, is_drum):
        self.rd = rd
        self.ch = ch
        self.note = note
        self.is_drum = is_drum
        semis = (note - rd.root) * rd.scale + rd.base_semis_off
        self.base_inc = (2.0 ** (semis / 12.0)) * rd.rate / OUT_RATE
        self.inc = self.base_inc * bend_factor
        v = vel / 127.0
        self.vgain = (v ** rd.vel_exp) * rd.gain
        self.pos = 0.0
        self.phase = 'a'
        self.env = 0.0
        self.att_done = 0
        self.rel_k = rd.rel_k
        self.sus_held = False
        self.dead = False
        self.born = 0.0

    def release(self, k=None):
        if self.phase != 'r':
            self.phase = 'r'
            if k is not None:
                self.rel_k = k

    def envelope(self, n):
        rd = self.rd
        out = np.empty(n, dtype=np.float32)
        j = 0
        while j < n:
            k = n - j
            if self.phase == 'a':
                k = min(k, rd.att_n - self.att_done)
                ramp = (self.att_done + np.arange(1, k + 1,
                                                  dtype=np.float32)) \
                    / rd.att_n
                out[j:j + k] = ramp
                self.att_done += k
                self.env = float(ramp[-1])
                if self.att_done >= rd.att_n:
                    self.env = 1.0
                    self.phase = 'd' if rd.dec_k > 0.0 else 's'
            elif self.phase == 'd':
                seq = rd.sus + (self.env - rd.sus) * \
                    np.power(rd.dec_k, np.arange(1, k + 1,
                                                 dtype=np.float32))
                out[j:j + k] = seq
                self.env = float(seq[-1])
                if self.env - rd.sus < ENV_FLOOR:
                    if rd.sus < ENV_FLOOR:
                        self.dead = True
                        out[j + k:] = 0.0
                        return out
                    self.phase = 's'
            elif self.phase == 's':
                out[j:j + k] = self.env
            else:
                seq = self.env * np.power(self.rel_k,
                                          np.arange(1, k + 1,
                                                    dtype=np.float32))
                out[j:j + k] = seq
                self.env = float(seq[-1])
                if self.env < ENV_FLOOR:
                    self.dead = True
                    out[j + k:] = 0.0
                    return out
            j += k
        return out

    def render(self, n):
        """Mono float32 of length n (or None when the voice just died)."""
        rd = self.rd
        pos = self.pos + self.inc * np.arange(n, dtype=np.float64)
        if rd.looped:
            lp = rd.le + 1 - rd.ls
            pos = np.where(pos > rd.le,
                           rd.ls + np.mod(pos - rd.ls, lp), pos)
            self.pos += self.inc * n
        else:
            limit = len(rd.pcm) - 2
            if self.pos > limit:
                self.dead = True
                return None
            pos = np.minimum(pos, limit)
            self.pos += self.inc * n
            if self.pos > limit:
                self.dead = True   # after this segment
        i0 = pos.astype(np.int64)
        frac = (pos - i0).astype(np.float32)
        s = rd.pcm[i0] * (1.0 - frac) + rd.pcm[i0 + 1] * frac
        env = self.envelope(n)
        if not rd.looped and self.dead:
            tail = pos >= (len(rd.pcm) - 2)
            s[tail] = 0.0
        return s * env * self.vgain


class Engine:
    def __init__(self, bank, xg_drums=True):
        self.bank = bank
        self.xg_drums = xg_drums
        self.rdcache = {}
        self.chs = [ChState() for _ in range(16)]
        self.workers = [[] for _ in range(NWORKERS)]
        self.stats = dict(notes=0, no_region=0, steals=0,
                          steals_active=0, self_steals=0, peak_voices=0,
                          peak_worker=[0] * NWORKERS)
        self.now = 0.0
        self.used_regions = set()

    def region_data(self, slot, note):
        reg = self.bank.find_region(slot, note)
        if reg is None:
            return None
        key = (slot, reg['adpcm_off'])
        rd = self.rdcache.get(key)
        if rd is None:
            rd = RegionData(self.bank, reg)
            self.rdcache[key] = rd
        self.used_regions.add((key, reg['adpcm_nbytes']))
        return rd

    def voices(self):
        for w in self.workers:
            yield from w

    def alloc(self, ch=None, note=None):
        # Same-note self-steal first: a releasing instance of the same
        # (ch, note) is transparently reclaimed -- repeated tutti stabs
        # otherwise burn 2x voices on their own 0.3s tails.

        if ch is not None:
            for wi, pool in enumerate(self.workers):
                for v in pool:
                    if v.ch == ch and v.note == note and v.phase == 'r':
                        self.stats['self_steals'] += 1
                        return wi, v

        w = min(range(NWORKERS), key=lambda i: len(self.workers[i]))
        if len(self.workers[w]) < VOICES_PER_WORKER:
            return w, None

        # All 64 busy: steal GLOBALLY (any worker), never a voice still
        # in its attack (<60ms old) while alternatives exist -- min-env
        # stealing otherwise makes the layers of one stab eat each
        # other (attack-phase env is near zero).

        self.stats['steals'] += 1
        allv = [(wi, v) for wi, pool in enumerate(self.workers)
                for v in pool]
        rel = [(wi, v) for wi, v in allv if v.phase == 'r']
        if rel:
            return min(rel, key=lambda x: x[1].env)
        self.stats['steals_active'] += 1
        mature = [(wi, v) for wi, v in allv
                  if self.now - v.born > 0.06]
        pool2 = mature if mature else allv
        return min(pool2, key=lambda x: x[1].env)

    def note_on(self, ch, note, vel):
        self.stats['notes'] += 1
        cs = self.chs[ch]

        # Drum channel selection.  Bank MSB 127 means two OPPOSITE
        # things: in XG files (Yamaha sysex present) it turns any
        # channel into a drum part; without XG context it is the GS
        # MT-32 compatibility map -- a MELODIC bank (battle05ao.mid ch4
        # bank127 prog48 = MT-32 strings).  So drum-izing is gated on
        # the file actually being XG; otherwise the bank falls back to
        # the GM program.  The drum channel's program picks the KIT;
        # kit 48 (GS Orchestra) maps keys 41-53 to chromatic timpani,
        # played as pitched p47.

        is_drum = (ch == 9) or (self.xg_drums and
                                cs.bank_msb in (120, 126, 127))
        if is_drum:
            kit = cs.prog
            if kit == 48 and 41 <= note <= 53:
                rd = self.region_data(47, note)
            else:
                rd = self.region_data(DRUM_SLOT, note)
        else:
            rd = self.region_data(cs.prog, note)
        if rd is None:
            self.stats['no_region'] += 1
            return
        if is_drum and rd.excl:
            for v in self.voices():
                if v.is_drum and v.rd.excl == rd.excl:
                    v.release(CHOKE_K)
        w, victim = self.alloc(ch, note)
        v = Voice(rd, ch, note, vel, cs.bend_factor(), is_drum)
        v.born = self.now
        if victim is None:
            self.workers[w].append(v)
        else:
            self.workers[w][self.workers[w].index(victim)] = v

    def note_off(self, ch, note):
        if ch == 9:
            return
        for v in self.voices():
            if v.ch == ch and v.note == note and v.phase != 'r' and \
                    not v.is_drum:
                if self.chs[ch].sus:
                    v.sus_held = True
                else:
                    v.release()

    def cc(self, ch, num, val):
        cs = self.chs[ch]
        if num == 7:
            cs.vol = val
        elif num == 11:
            cs.exp = val
        elif num == 10:
            cs.pan = val
        elif num == 91:
            cs.rev = val
        elif num == 64:
            was = cs.sus
            cs.sus = val >= 64
            if was and not cs.sus:
                for v in self.voices():
                    if v.ch == ch and v.sus_held:
                        v.release()
        elif num == 0:
            cs.bank_msb = val
        elif num == 101:
            cs.rpn = (val, cs.rpn[1] if cs.rpn else 127)
        elif num == 100:
            cs.rpn = (cs.rpn[0] if cs.rpn else 127, val)
        elif num in (98, 99):
            cs.rpn = None
        elif num == 6:
            if cs.rpn == (0, 0):
                cs.bendrange = float(val)
                self.rebend(ch)
        elif num == 123 or num == 120:
            for v in self.voices():
                if v.ch == ch:
                    v.release()
        elif num == 121:
            cs.bend = 0
            cs.exp = 127
            cs.sus = False
            cs.rpn = None
            self.rebend(ch)

    def rebend(self, ch):
        f = self.chs[ch].bend_factor()
        for v in self.voices():
            if v.ch == ch:
                v.inc = v.base_inc * f

    def event(self, kind, ch, a, b):
        if kind == 'on':
            self.note_on(ch, a, b)
        elif kind == 'off':
            self.note_off(ch, a)
        elif kind == 'cc':
            self.cc(ch, a, b)
        elif kind == 'prog':
            self.chs[ch].prog = a
        elif kind == 'bend':
            self.chs[ch].bend = a
            self.rebend(ch)

    def render_seg(self, dry_l, dry_r, send_l, send_r, j, n):
        self.now = j / OUT_RATE
        total = 0
        for wi, pool in enumerate(self.workers):
            nv = len(pool)
            total += nv
            if nv > self.stats['peak_worker'][wi]:
                self.stats['peak_worker'][wi] = nv
            for v in pool[:]:
                s = v.render(n)
                if s is not None:
                    cs = self.chs[v.ch]
                    p = max(0, min(127,
                                   64 + (cs.pan - 64) + (v.rd.pan - 64)))
                    ang = p / 127.0 * math.pi / 2.0
                    g = cs.gain()
                    sl = s * (g * math.cos(ang))
                    sr = s * (g * math.sin(ang))
                    dry_l[j:j + n] += sl
                    dry_r[j:j + n] += sr
                    rev = cs.rev / 127.0
                    if rev > 0.0:
                        send_l[j:j + n] += sl * rev
                        send_r[j:j + n] += sr * rev
                if v.dead:
                    pool.remove(v)
        if total > self.stats['peak_voices']:
            self.stats['peak_voices'] = total


def render_song(bank_path, mid_path, out_path, max_s=None, dry=False,
                xg_drums=True):
    bank = Bank(bank_path)
    song = smf.parse(mid_path)
    dur = song['duration'] + 2.0
    if max_s:
        dur = min(dur, max_s)
    nsamp = int(dur * OUT_RATE)
    nblocks = (nsamp + BLOCK - 1) // BLOCK
    nsamp = nblocks * BLOCK

    eng = Engine(bank, xg_drums=xg_drums and song['xg'])
    events = [(min(int(t * OUT_RATE), nsamp - 1), k, c, a, b)
              for (t, k, c, a, b) in song['events']]

    out_l = np.zeros(nsamp, dtype=np.float32)
    out_r = np.zeros(nsamp, dtype=np.float32)
    send_l = np.zeros(nsamp, dtype=np.float32)
    send_r = np.zeros(nsamp, dtype=np.float32)

    echo_l = np.zeros(nsamp + ECHO_DELAY, dtype=np.float32)
    echo_r = np.zeros(nsamp + ECHO_DELAY, dtype=np.float32)
    kern = ECHO_LPF * (1.0 - ECHO_LPF) ** np.arange(LPF_TAPS)
    kern = kern.astype(np.float32)

    ev_i = 0
    nev = len(events)
    carry_l = np.zeros(LPF_TAPS - 1, dtype=np.float32)
    carry_r = np.zeros(LPF_TAPS - 1, dtype=np.float32)
    for b in range(nblocks):
        start = b * BLOCK
        j = 0
        while j < BLOCK:
            seg_end = BLOCK
            while ev_i < nev and events[ev_i][0] == start + j:
                _, k, c, a2, b2 = events[ev_i]
                eng.event(k, c, a2, b2)
                ev_i += 1
            if ev_i < nev and events[ev_i][0] < start + BLOCK:
                seg_end = events[ev_i][0] - start
            n = seg_end - j
            if n > 0:
                eng.render_seg(out_l, out_r, send_l, send_r,
                               start + j, n)
            j = seg_end

        # Echo bus for this block: read the delay line, feed back

        sl = slice(start, start + BLOCK)
        eout_l = echo_l[sl]
        eout_r = echo_r[sl]
        fb_l = send_l[sl] + eout_l * ECHO_FEEDBACK
        fb_r = send_r[sl] + eout_r * ECHO_FEEDBACK

        # One-pole LPF approximated by a truncated exponential FIR,
        # with a carry buffer so block boundaries are seamless

        f_l = np.convolve(np.concatenate((carry_l, fb_l)),
                          kern)[LPF_TAPS - 1:LPF_TAPS - 1 + BLOCK]
        f_r = np.convolve(np.concatenate((carry_r, fb_r)),
                          kern)[LPF_TAPS - 1:LPF_TAPS - 1 + BLOCK]
        carry_l = fb_l[-(LPF_TAPS - 1):]
        carry_r = fb_r[-(LPF_TAPS - 1):]
        echo_l[start + ECHO_DELAY:start + ECHO_DELAY + BLOCK] = f_l
        echo_r[start + ECHO_DELAY:start + ECHO_DELAY + BLOCK] = f_r
        if not dry:
            out_l[sl] += eout_l * ECHO_MIX
            out_r[sl] += eout_r * ECHO_MIX

    out_l *= MASTER_GAIN
    out_r *= MASTER_GAIN

    peak = max(float(np.max(np.abs(out_l))), float(np.max(np.abs(out_r))),
               1e-9)
    clip = int(np.sum(np.abs(out_l) > 1.0) + np.sum(np.abs(out_r) > 1.0))

    sig = np.empty(nsamp * 2, dtype=np.float32)
    sig[0::2] = out_l
    sig[1::2] = out_r
    pcm16 = np.clip(sig * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(out_path, 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(OUT_RATE)
        w.writeframes(pcm16.tobytes())

    load_bytes = sum(nb for (_, nb) in eng.used_regions)
    st = eng.stats
    rms = float(np.sqrt(np.mean(sig.astype(np.float64) ** 2)))
    result = dict(
        file=mid_path, duration=dur, notes=st['notes'],
        no_region=st['no_region'],
        steals=st['steals'], steals_active=st['steals_active'],
        self_steals=st['self_steals'],
        peak_voices=st['peak_voices'],
        peak_worker=st['peak_worker'],
        loop_s=song['loop_s'],
        load_kb=load_bytes / 1024.0,
        nregions_used=len(eng.used_regions),
        peak_dbfs=20 * math.log10(peak),
        rms_dbfs=20 * math.log10(max(rms, 1e-9)),
        clip_samples=clip,
    )
    return result


def main():
    argv = sys.argv[1:]
    out_path = 'out.wav'
    json_path = None
    max_s = None
    dry = False
    pos = []
    i = 0
    while i < len(argv):
        if argv[i] == '--out':
            out_path = argv[i + 1]
            i += 2
        elif argv[i] == '--json':
            json_path = argv[i + 1]
            i += 2
        elif argv[i] == '--max-s':
            max_s = float(argv[i + 1])
            i += 2
        elif argv[i] == '--dry':
            dry = True
            i += 1
        else:
            pos.append(argv[i])
            i += 1

    bank_path, mid_path = pos
    r = render_song(bank_path, mid_path, out_path, max_s, dry=dry)
    for k, v in r.items():
        print(f'{k}: {v}')
    if json_path:
        json.dump(r, open(json_path, 'w'))


if __name__ == '__main__':
    main()
