#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""Per-program timbre distance sweep: render one test note of every GM
program through BOTH engines (fluidsynth+font as truth, gmbank+gmrender
as device) inside a single 128-note SMF, then rank programs by spectral
and envelope distance.  Output drives tier promotions in gmbank_build.

Usage: timbre_sweep.py <gmbank.bin> <font.sf2> [--json out.json]
"""

import json
import math
import sys

import numpy as np

from ref_compare import write_smf, fluid_render, load_mono, \
    octave_spectrum, TMP, FRAME
from gmrender import render_song

RATE = 48000
NOTE_S = 1.2
SLOT_S = 2.0


def seg_metrics(ref, ours):
    er = None
    n = min(len(ref), len(ours))
    ref = ref[:n]
    ours = ours[:n]
    rr = float(np.sqrt(np.mean(ref.astype(np.float64) ** 2)))
    ro = float(np.sqrt(np.mean(ours.astype(np.float64) ** 2)))
    if rr < 1e-5:
        return None
    lvl = 20 * math.log10(max(ro, 1e-9) / rr)
    nf = n // FRAME
    if nf < 4:
        return None
    er = np.sqrt(np.mean(ref[:nf * FRAME].reshape(nf, FRAME) ** 2, axis=1))
    eo = np.sqrt(np.mean(ours[:nf * FRAME].reshape(nf, FRAME) ** 2, axis=1))
    env = float(np.corrcoef(er, eo)[0, 1])
    sr = octave_spectrum(ref)
    so = octave_spectrum(ours)
    spec = float(np.sum(np.abs(sr - so))) if sr is not None and \
        so is not None else -1.0
    return dict(level_db=lvl, env_corr=env, spec_l1=spec)


def main():
    bank_path, font_path = sys.argv[1], sys.argv[2]
    jout = sys.argv[sys.argv.index('--json') + 1] \
        if '--json' in sys.argv else None

    stats = json.load(open('corpus_stats.json'))
    klo = {int(k): v for k, v in stats['prog_key_lo'].items()}
    khi = {int(k): v for k, v in stats['prog_key_hi'].items()}

    notes = {}
    events = []
    t = 0.0
    for prog in range(128):
        lo = max(min(klo.get(prog, 48), 127), 0)
        hi = max(min(khi.get(prog, 72), 127), lo)
        note = max(36, min(84, (lo + hi) // 2))
        notes[prog] = note
        events.append((t, 'prog', 0, prog, 0))
        events.append((t + 0.01, 'on', 0, note, 100))
        events.append((t + 0.01 + NOTE_S, 'off', 0, note, 0))
        t += SLOT_S

    mid = f'{TMP}/timbre.mid'
    write_smf(events, mid)
    fluid_render(font_path, mid, f'{TMP}/timbre_ref.wav')
    render_song(bank_path, mid, f'{TMP}/timbre_our.wav', dry=True)

    ref = load_mono(f'{TMP}/timbre_ref.wav')
    ours = load_mono(f'{TMP}/timbre_our.wav')

    rows = []
    for prog in range(128):
        a = int(prog * SLOT_S * RATE)
        b = int((prog * SLOT_S + SLOT_S - 0.1) * RATE)
        m = seg_metrics(ref[a:b], ours[a:b])
        if m is None:
            rows.append(dict(prog=prog, note=notes[prog], silent=True))
            continue
        rows.append(dict(prog=prog, note=notes[prog], **m))

    scored = [r for r in rows if not r.get('silent')]
    scored.sort(key=lambda r: -(r['spec_l1'] +
                                (1.0 - max(r['env_corr'], 0.0))))
    print('worst programs (spec_l1 + (1-env_corr)):')
    print(f"{'prog':>4s} {'note':>4s} {'spec':>6s} {'env':>6s} {'lvl':>6s}")
    for r in scored[:25]:
        print(f"{r['prog']:4d} {r['note']:4d} {r['spec_l1']:6.3f} "
              f"{r['env_corr']:6.3f} {r['level_db']:+6.1f}")

    silent = [r['prog'] for r in rows if r.get('silent')]
    if silent:
        print('silent in ref (skipped):', silent)

    if jout:
        json.dump(rows, open(jout, 'w'))
        print('->', jout)


if __name__ == '__main__':
    main()
