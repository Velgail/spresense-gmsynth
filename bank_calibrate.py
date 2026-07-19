#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""Automatic per-program volume calibration against the source
SoundFont: prove (and enforce) that gmbank.bin has the same program
balance as the font it was built from, whichever font that is.

The tool closes the whole loop by itself, with NO external MIDI files:

  1. build gmbank.bin from the font (corpus stats optional; a neutral
     flat profile is used when absent)
  2. generate a calibration SMF covering every melodic program (three
     notes across its key range) and every drum key in the bank
  3. render it through BOTH engines with effects off: fluidsynth+font
     as ground truth, gmrender (bit-model of the device) as ours
  4. measure the active-region RMS level of every slot, subtract the
     melodic median so the comparison is invariant to the arbitrary
     master gains, and turn residuals into per-program trim factors
     (drum keys use the 1000+key convention)
  5. rebuild with the merged trims and repeat until every audible
     program sits within --tol dB of the reference balance

Outputs: the calibrated bank, the cumulative trim table (JSON, the
reproducible "recipe"), and a before/after report that is the
equivalence proof.  The last iteration's WAV pair stays in calib_tmp/
for listening (calib_ref.wav vs calib_our.wav).

Usage:
  bank_calibrate.py <font.sf2> [--fallback fb.sf2] [--stats corpus_stats.json]
                    [--trims-in seed_trims.json] [--out gmbank.bin]
                    [--trims-out trims_cal.json] [--report calib_report.txt]
                    [--iters 4] [--tol 1.0]
"""

import json
import math
import os
import subprocess
import sys

import numpy as np

from ref_compare import write_smf, fluid_render, load_mono, FRAME
from gmrender import render_song
from gmbank_build import GM_DRUM_KEYS

RATE = 48000
TMP = 'calib_tmp'

NOTE_S = 0.6                 # per calibration note
GAP_S = 0.1
SLOT_S = 3 * (NOTE_S + GAP_S) + 0.4   # 3 notes + settle = 2.5 s
DRUM_SLOT_S = 0.9
VEL = 100

CLAMP_LO = 0.25              # per-iteration correction clamp
CLAMP_HI = 4.0


# ---------------------------------------------------------------------------
# Stats: use the corpus file when present, else a neutral flat profile
# ---------------------------------------------------------------------------

def neutral_stats():
    """No-corpus default: every program tier B, generous key ranges,
    full GM drum set at moderate quality."""
    return {
        'files': ['neutral'] * 100,
        'prog_files': {str(p): 6 for p in range(128)},      # freq 0.06 -> B
        'prog_key_lo': {str(p): 36 for p in range(128)},
        'prog_key_hi': {str(p): 84 for p in range(128)},
        'drum_files': {str(k): 10 for k in sorted(GM_DRUM_KEYS)},
    }


def load_stats(path):
    if path and os.path.exists(path):
        print(f'calibrate: corpus stats from {path}')
        return json.load(open(path)), path
    print('calibrate: no corpus stats, using neutral flat profile')
    s = neutral_stats()
    p = f'{TMP}/neutral_stats.json'
    json.dump(s, open(p, 'w'))
    return s, p


# ---------------------------------------------------------------------------
# Calibration score
# ---------------------------------------------------------------------------

def build_events(stats):
    """One slot per melodic program (3 notes: low/mid/high of its key
    range) then one slot per drum key.  Returns (events, slots) where
    slots = [(label, kind, id, t0, t1)]."""
    klo = {int(k): v for k, v in stats['prog_key_lo'].items()}
    khi = {int(k): v for k, v in stats['prog_key_hi'].items()}
    drum_keys = sorted({k for k in GM_DRUM_KEYS} |
                       {int(k) for k in stats['drum_files']
                        if 24 <= int(k) <= 87})

    events = []
    slots = []
    t = 0.0
    for prog in range(128):
        lo = max(0, min(127, klo.get(prog, 48)))
        hi = max(lo, min(127, khi.get(prog, 72)))
        mid = (lo + hi) // 2
        notes = sorted({max(24, lo + 2), mid, min(96, max(hi - 2, lo))})
        events.append((t, 'prog', 0, prog, 0))
        tn = t + 0.02
        for n in notes:
            events.append((tn, 'on', 0, n, VEL))
            events.append((tn + NOTE_S, 'off', 0, n, 0))
            tn += NOTE_S + GAP_S
        slots.append((f'p{prog:03d}', 'prog', prog, t, t + SLOT_S - 0.1))
        t += SLOT_S

    for key in drum_keys:
        events.append((t + 0.02, 'on', 9, key, VEL))
        events.append((t + 0.02 + 0.4, 'off', 9, key, 0))
        slots.append((f'd{key:03d}', 'drum', key, t, t + DRUM_SLOT_S - 0.05))
        t += DRUM_SLOT_S

    return events, slots


def slot_levels(ref, ours, t0, t1):
    """RMS (dBFS) of both signals over the REFERENCE's active frames in
    t0..t1, so short/quiet slots (one-shot drums, noise programs) are
    measured over the same window in both engines.  Returns (ref_db,
    our_db); (None, None) when the reference itself is silent.  A
    truly silent 'ours' floors at -120 dB instead of disappearing."""
    a, b = int(t0 * RATE), int(t1 * RATE)
    r = ref[a:b]
    o = ours[a:b]
    nf = min(len(r), len(o)) // FRAME
    if nf < 1:
        return None, None
    re = np.sqrt(np.mean(r[:nf * FRAME].reshape(nf, FRAME) ** 2, axis=1))
    oe = np.sqrt(np.mean(o[:nf * FRAME].reshape(nf, FRAME) ** 2, axis=1))
    active = re > 10 ** (-55 / 20)
    if int(np.sum(active)) < 1:
        return None, None
    lr = 20 * math.log10(float(np.sqrt(np.mean(re[active] ** 2))))
    lo = 20 * math.log10(max(float(np.sqrt(np.mean(oe[active] ** 2))),
                             1e-6))
    return lr, lo


def measure(bank_path, font_path, events, slots, tag):
    mid = f'{TMP}/calib.mid'
    refw = f'{TMP}/calib_ref.wav'
    ourw = f'{TMP}/calib_our.wav'
    write_smf(events, mid)
    if not os.path.exists(refw):           # reference never changes
        fluid_render(font_path, mid, refw)
    render_song(bank_path, mid, ourw, dry=True)
    ref = load_mono(refw)
    ours = load_mono(ourw)

    rows = []
    for (label, kind, ident, t0, t1) in slots:
        lr, lo = slot_levels(ref, ours, t0, t1)
        rows.append(dict(label=label, kind=kind, id=ident,
                         ref_db=lr, our_db=lo,
                         delta=(lo - lr) if lr is not None else None))
    return rows


def residuals(rows):
    """Median-center melodic deltas; drums share the same anchor so the
    drums-vs-melodic balance is calibrated too.  Returns (rows-with-
    residual, median)."""
    mel = [r['delta'] for r in rows
           if r['kind'] == 'prog' and r['delta'] is not None]
    med = float(np.median(mel))
    for r in rows:
        r['resid'] = (r['delta'] - med) if r['delta'] is not None else None
    return rows, med


# ---------------------------------------------------------------------------
# Bank building
# ---------------------------------------------------------------------------

def build_bank(font, fallback, stats_path, trims, out_path):
    tpath = f'{TMP}/cur_trims.json'
    json.dump({str(k): round(v, 4) for k, v in trims.items()},
              open(tpath, 'w'))
    cmd = [sys.executable, 'gmbank_build.py', font,
           '--stats', stats_path, '--out', out_path,
           '--report', f'{TMP}/build_report.txt', '--trims', tpath]
    if fallback:
        cmd += ['--fallback', fallback]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f'gmbank_build failed:\n{r.stdout}\n{r.stderr}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    argv = sys.argv[1:]
    font = None
    fallback = None
    stats_path = 'corpus_stats.json'
    trims_in = None
    out_path = 'gmbank.bin'
    trims_out = 'trims_cal.json'
    rep_path = 'calib_report.txt'
    iters = 4
    tol = 1.0
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--fallback':
            fallback = argv[i + 1]; i += 2
        elif a == '--stats':
            stats_path = argv[i + 1]; i += 2
        elif a == '--trims-in':
            trims_in = argv[i + 1]; i += 2
        elif a == '--out':
            out_path = argv[i + 1]; i += 2
        elif a == '--trims-out':
            trims_out = argv[i + 1]; i += 2
        elif a == '--report':
            rep_path = argv[i + 1]; i += 2
        elif a == '--iters':
            iters = int(argv[i + 1]); i += 2
        elif a == '--tol':
            tol = float(argv[i + 1]); i += 2
        else:
            font = a; i += 1
    if font is None:
        sys.exit(__doc__)

    os.makedirs(TMP, exist_ok=True)
    for f in ('calib_ref.wav',):           # stale reference from other font
        p = f'{TMP}/{f}'
        if os.path.exists(p):
            os.remove(p)

    stats, stats_path = load_stats(stats_path)
    events, slots = build_events(stats)

    trims = {}
    if trims_in:
        trims = {int(k): float(v) for k, v in
                 json.load(open(trims_in)).items()}
        print(f'calibrate: seeded {len(trims)} trims from {trims_in}')

    first = None
    rows = None
    for it in range(iters + 1):
        print(f'-- iteration {it}: building bank...')
        build_bank(font, fallback, stats_path, trims, out_path)
        print(f'-- iteration {it}: rendering & measuring...')
        rows, med = residuals(measure(out_path, font, events, slots, it))
        if first is None:
            first = {r['label']: r['resid'] for r in rows}

        audible = [r for r in rows if r['resid'] is not None]
        worst = max(abs(r['resid']) for r in audible)
        nbad = sum(1 for r in audible if abs(r['resid']) > tol)
        print(f'   median gain offset {med:+.1f} dB, worst residual '
              f'{worst:.1f} dB, {nbad}/{len(audible)} outside +-{tol} dB')
        if nbad == 0 or it == iters:
            break

        for r in audible:
            if abs(r['resid']) <= tol * 0.5:
                continue
            corr = 10.0 ** (-r['resid'] / 20.0)
            corr = min(CLAMP_HI, max(CLAMP_LO, corr))
            key = r['id'] if r['kind'] == 'prog' else 1000 + r['id']
            trims[key] = min(32.0, max(0.05, trims.get(key, 1.0) * corr))

    # Outputs

    json.dump({str(k): round(v, 4) for k, v in sorted(trims.items())},
              open(trims_out, 'w'), indent=1)

    lines = [f'bank_calibrate: {font} -> {out_path}',
             f'tolerance +-{tol} dB (residual = our-ref level, '
             f'melodic-median centered)',
             '',
             f'{"slot":>6s} {"ref_dBFS":>9s} {"before":>7s} '
             f'{"after":>7s}  verdict']
    npass = nfail = nsilent = 0
    for r in rows:
        b = first.get(r['label'])
        if r['resid'] is None:
            verdict = 'no ref'
            nsilent += 1
        elif abs(r['resid']) <= tol:
            verdict = 'ok'
            npass += 1
        else:
            verdict = 'SILENT (bug?)' if r['our_db'] is not None and \
                r['our_db'] <= -100 else 'FAIL'
            nfail += 1
        fmt = lambda v: f'{v:+7.1f}' if v is not None else '      -'
        ref_db = f'{r["ref_db"]:9.1f}' if r['ref_db'] is not None \
            else '        -'
        lines.append(f'{r["label"]:>6s} {ref_db} {fmt(b)} '
                     f'{fmt(r["resid"])}  {verdict}')
    lines.append('')
    lines.append(f'RESULT: {npass} within tolerance, {nfail} failing, '
                 f'{nsilent} absent in font')
    open(rep_path, 'w').write('\n'.join(lines) + '\n')

    print(f'\ntrims -> {trims_out} ({len(trims)} entries)')
    print(f'report -> {rep_path}')
    print(lines[-1])
    print(f'listen: {TMP}/calib_ref.wav vs {TMP}/calib_our.wav')
    sys.exit(0 if nfail == 0 else 1)


if __name__ == '__main__':
    main()
