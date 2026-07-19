#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""Compare gmbank+gmrender output against ground truth: the ORIGINAL
SoundFont rendered by fluidsynth, per channel and full mix.

For each song:
  - re-emit the parsed events as a clean format-0 SMF (1 tick = 1 ms),
    once complete and once per active channel
  - render every variant through BOTH engines with effects off
    (fluidsynth -R 0 -C 0 / gmrender --dry) so the comparison sees the
    instruments, not the reverbs
  - metrics per channel:
      level : active-region RMS difference (ours - ref), dB
      env   : Pearson correlation of 50ms RMS envelopes
      spec  : L1 distance of normalized octave-band spectra (0=identical)

Per-program trim factors are derived from the level columns
(note-count weighted, scale-invariant via the full-mix levels) and
written to --trims-out for gmbank_build.py --trims.

Usage:
  ref_compare.py <gmbank.bin> <font.sf2> <song.mid...>
                 [--trims-out trims_ref.json] [--max-s N]
"""

import json
import math
import os
import struct
import subprocess
import sys
import wave
from collections import defaultdict

import numpy as np

import smf
from gmrender import render_song

RATE = 48000
TMP = 'refcmp_tmp'


# ---------------------------------------------------------------------------
# SMF re-emission (1 tick = 1 ms at fixed tempo)
# ---------------------------------------------------------------------------

def vlq(v):
    out = [v & 0x7F]
    v >>= 7
    while v:
        out.append(0x80 | (v & 0x7F))
        v >>= 7
    return bytes(reversed(out))


def write_smf(events, path, ch_filter=None):
    """events: smf.parse() output events.  ch_filter: keep only this
    channel's voice events (tempo is already baked into times)."""
    body = bytearray()
    body += b'\x00\xff\x51\x03' + (500000).to_bytes(3, 'big')
    last_ms = 0
    for (t, kind, ch, a, b) in events:
        if ch_filter is not None and ch != ch_filter:
            continue
        ms = int(round(t * 1000))
        d = ms - last_ms
        last_ms = ms
        if kind == 'on':
            ev = bytes([0x90 | ch, a, b])
        elif kind == 'off':
            ev = bytes([0x80 | ch, a, 64])
        elif kind == 'cc':
            ev = bytes([0xB0 | ch, a, b])
        elif kind == 'prog':
            ev = bytes([0xC0 | ch, a])
        elif kind == 'bend':
            v = a + 8192
            ev = bytes([0xE0 | ch, v & 0x7F, (v >> 7) & 0x7F])
        else:
            continue
        body += vlq(d) + ev
    body += b'\x00\xff\x2f\x00'
    with open(path, 'wb') as f:
        f.write(b'MThd' + struct.pack('>IHHH', 6, 0, 1, 500))
        f.write(b'MTrk' + struct.pack('>I', len(body)) + bytes(body))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def fluid_render(font, mid, out):
    subprocess.run(['fluidsynth', '-ni', '-R', '0', '-C', '0',
                    '-g', '0.4', '-r', str(RATE), '-F', out, font, mid],
                   check=True, capture_output=True)


def load_mono(path, nmax=None):
    with wave.open(path, 'rb') as w:
        n = w.getnframes()
        if nmax:
            n = min(n, nmax)
        raw = w.readframes(n)
        a = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0
        if w.getnchannels() == 2:
            a = 0.5 * (a[0::2] + a[1::2])
    return a


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

FRAME = int(0.05 * RATE)


def envelope(x):
    n = len(x) // FRAME
    return np.sqrt(np.mean(x[:n * FRAME].reshape(n, FRAME) ** 2,
                           axis=1) + 1e-12)


def octave_spectrum(x):
    """Time-averaged energy in octave bands 60..15360 Hz, normalized."""
    nfft = 8192
    n = len(x) // nfft
    if n == 0:
        return None
    frames = x[:n * nfft].reshape(n, nfft) * np.hanning(nfft)
    mag = np.mean(np.abs(np.fft.rfft(frames, axis=1)) ** 2, axis=0)
    freqs = np.fft.rfftfreq(nfft, 1.0 / RATE)
    bands = []
    f0 = 60.0
    while f0 < 16000:
        m = (freqs >= f0) & (freqs < f0 * 2)
        bands.append(float(np.sum(mag[m])))
        f0 *= 2
    v = np.array(bands)
    s = np.sum(v)
    return v / s if s > 0 else v


def compare(ref, ours):
    n = min(len(ref), len(ours))
    ref = ref[:n]
    ours = ours[:n]
    er = envelope(ref)
    eo = envelope(ours)
    active = er > 10 ** (-50 / 20)
    if int(np.sum(active)) < 4:
        return None
    lvl = 20 * math.log10(float(np.sqrt(np.mean(eo[active] ** 2))) /
                          max(float(np.sqrt(np.mean(er[active] ** 2))),
                              1e-9))
    ca = np.corrcoef(er, eo)[0, 1]
    n2 = len(er) * FRAME
    mask = np.repeat(active, FRAME)
    sr = octave_spectrum(ref[:n2][mask])
    so = octave_spectrum(ours[:n2][mask])
    spec = float(np.sum(np.abs(sr - so))) if sr is not None and \
        so is not None else -1.0
    return dict(level_db=lvl, env_corr=float(ca), spec_l1=spec)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    argv = sys.argv[1:]
    trims_out = None
    max_s = 120.0
    pos = []
    i = 0
    while i < len(argv):
        if argv[i] == '--trims-out':
            trims_out = argv[i + 1]
            i += 2
        elif argv[i] == '--max-s':
            max_s = float(argv[i + 1])
            i += 2
        else:
            pos.append(argv[i])
            i += 1
    bank_path, font_path = pos[0], pos[1]
    songs = pos[2:]

    os.makedirs(TMP, exist_ok=True)
    nmax = int(max_s * RATE)

    # trim aggregation: prog -> [(level_db_delta, weight)]
    prog_levels = defaultdict(list)

    for song in songs:
        name = os.path.splitext(os.path.basename(song))[0][:24]
        parsed = smf.parse(song)
        events = [e for e in parsed['events'] if e[0] <= max_s]

        # channel -> (main prog, note count)
        ch_notes = defaultdict(int)
        ch_progs = defaultdict(lambda: defaultdict(int))
        cur = {}
        for (t, kind, ch, a, b) in events:
            if kind == 'prog':
                cur[ch] = a
            elif kind == 'on':
                ch_notes[ch] += 1
                ch_progs[ch][cur.get(ch, 0)] += 1

        variants = [('mix', None)] + \
            [(f'ch{c:02d}', c) for c in sorted(ch_notes)
             if ch_notes[c] >= 20]

        print(f'== {name} ==')
        mix_delta = 0.0
        for label, chf in variants:
            mid = f'{TMP}/{name}_{label}.mid'
            refw = f'{TMP}/{name}_{label}_ref.wav'
            ourw = f'{TMP}/{name}_{label}_our.wav'
            write_smf(events, mid, chf)
            fluid_render(font_path, mid, refw)
            render_song(bank_path, mid, ourw, max_s=max_s + 2, dry=True)
            m = compare(load_mono(refw, nmax), load_mono(ourw, nmax))
            if m is None:
                print(f'  {label}: (silent)')
                continue
            if label == 'mix':
                mix_delta = m['level_db']
            rel = m['level_db'] - mix_delta
            prog = '-'
            if chf is not None:
                pmain = max(ch_progs[chf], key=ch_progs[chf].get)
                prog = 'drums' if chf == 9 else f'p{pmain}'
                key = -1 if chf == 9 else pmain
                prog_levels[key].append((rel, ch_notes[chf]))
            print(f'  {label} {prog:>6s}: level {m["level_db"]:+5.1f} dB '
                  f'(rel {rel:+5.1f}), env_corr {m["env_corr"]:.3f}, '
                  f'spec_l1 {m["spec_l1"]:.3f}')

    if trims_out:
        trims = {}
        for prog, entries in prog_levels.items():
            wsum = sum(w for _, w in entries)
            delta = sum(d * w for d, w in entries) / wsum
            if abs(delta) > 1.5:
                trims[prog] = round(
                    min(4.0, max(0.25, 10.0 ** (-delta / 20.0))), 3)
        json.dump(trims, open(trims_out, 'w'), indent=1)
        print(f'\nref-derived trims -> {trims_out}: {trims}')


if __name__ == '__main__':
    main()
