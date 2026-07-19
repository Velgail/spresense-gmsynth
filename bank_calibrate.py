#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""Automatic ZONE-level volume calibration against the source
SoundFont: prove (and enforce) that every multisample zone in
gmbank.bin sits at the same level as the font it was built from,
whichever font that is.

Granularity matters: the builder thins zones by tier (one zone can
cover 6..15 semitones), and a single per-program trim cannot fix a
register that came out hot while another came out quiet.  Region rows
carry an individual float gain that both the device and the offline
renderer already honor, so the calibration:

  1. builds gmbank.bin once (corpus stats optional; neutral profile
     when absent), or takes an existing bank via --bank
  2. reads the bank's own region table and generates a calibration SMF
     with ONE representative note per melodic zone (the zone's root
     where possible) plus every drum key -- so a thinned zone set is
     calibrated exactly as built, and NO external MIDI files are needed
  3. renders it through fluidsynth+font (ground truth) and gmrender
     (bit-model of the device), effects off
  4. measures each slot's level over the reference's active frames,
     median-centers across melodic zones so the arbitrary master gains
     cancel, and PATCHES the corrective factor straight into each
     region's gain field in the .bin -- no rebuild, so iterations cost
     seconds
  5. repeats until every audible zone is within --tol dB

Outputs: the calibrated bank (gain fields patched in place), the
per-zone factor recipe (JSON), and a before/after residual report that
is the equivalence proof.  The last iteration's WAV pair stays in
calib_tmp/ for listening (calib_ref.wav vs calib_our.wav).

Usage:
  bank_calibrate.py <font.sf2> [--bank existing.bin]
                    [--fallback fb.sf2] [--stats corpus_stats.json]
                    [--trims-in seed_trims.json] [--out gmbank.bin]
                    [--gains-out zone_gains.json] [--report calib_report.txt]
                    [--iters 6] [--tol 1.0]
"""

import json
import math
import os
import shutil
import struct
import subprocess
import sys

import numpy as np

from ref_compare import write_smf, fluid_render, load_mono, FRAME
from gmrender import render_song
from gmbank_build import GM_DRUM_KEYS
import gmbank_format as fmt

RATE = 48000
TMP = 'calib_tmp'
DRUM_SLOT = 128

NOTE_S = 0.7                 # per calibration note
SLOT_S = 1.3                 # note + settle
DRUM_SLOT_S = 0.9
VEL = 100

CLAMP_LO = 0.25              # per-iteration correction clamp
CLAMP_HI = 4.0
CUM_LO = 0.05                # cumulative clamp (gain is a float region
CUM_HI = 32.0                # field; no fixed-point ceiling to respect)

KEY_MIN, KEY_MAX = 21, 105   # sane audible range for test notes


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
        return path
    print('calibrate: no corpus stats, using neutral flat profile')
    p = f'{TMP}/neutral_stats.json'
    json.dump(neutral_stats(), open(p, 'w'))
    return p


# ---------------------------------------------------------------------------
# Calibration score: one note per melodic zone + every drum key
# ---------------------------------------------------------------------------

def zone_note(r):
    """Representative key for a region: its root when playable (native
    pitch, no shift), else the center of its key range, clamped to the
    audible band where the range allows."""
    lo = max(r['lokey'], min(KEY_MIN, r['hikey']))
    hi = min(r['hikey'], max(KEY_MAX, r['lokey']))
    if lo > hi:
        lo, hi = r['lokey'], r['hikey']
    return min(max(r['root'], lo), hi)


def build_slots(bank):
    """Returns (events, slots); slots = [(label, kind, (slot, regidx),
    note, t0, t1)] where regidx is the absolute region-table index the
    correction will be patched into."""
    events = []
    slots = []
    t = 0.0
    for prog in range(128):
        base = bank.index[prog]
        for zi, r in enumerate(bank.prog_regions(prog)):
            note = zone_note(r)
            if bank.find_region(prog, note) is not r:
                # overlapping ranges: first match wins in the engine,
                # so move the note past the earlier zone
                for n in range(r['lokey'], r['hikey'] + 1):
                    if bank.find_region(prog, n) is r:
                        note = n
                        break
                else:
                    continue               # fully shadowed: unmeasurable
            label = f'p{prog:03d}z{zi}'
            events.append((t, 'prog', 0, prog, 0))
            events.append((t + 0.02, 'on', 0, note, VEL))
            events.append((t + 0.02 + NOTE_S, 'off', 0, note, 0))
            slots.append((label, 'zone', base + zi, note,
                          t, t + SLOT_S - 0.05))
            t += SLOT_S

    base = bank.index[DRUM_SLOT]
    for di, r in enumerate(bank.prog_regions(DRUM_SLOT)):
        key = r['lokey']                     # drum regions are single-key
        events.append((t + 0.02, 'on', 9, key, VEL))
        events.append((t + 0.02 + 0.4, 'off', 9, key, 0))
        slots.append((f'd{key:03d}', 'drum', base + di, key,
                      t, t + DRUM_SLOT_S - 0.05))
        t += DRUM_SLOT_S

    return events, slots


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

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


def measure(bank_path, font_path, events, slots):
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
    for (label, kind, regidx, note, t0, t1) in slots:
        lr, lo = slot_levels(ref, ours, t0, t1)
        rows.append(dict(label=label, kind=kind, regidx=regidx, note=note,
                         ref_db=lr, our_db=lo,
                         delta=(lo - lr) if lr is not None else None))
    return rows


def residuals(rows):
    """Median-center melodic deltas; drums share the same anchor so the
    drums-vs-melodic balance is calibrated too."""
    mel = [r['delta'] for r in rows
           if r['kind'] == 'zone' and r['delta'] is not None]
    med = float(np.median(mel))
    for r in rows:
        r['resid'] = (r['delta'] - med) if r['delta'] is not None else None
    return rows, med


# ---------------------------------------------------------------------------
# Bank building / in-place gain patching
# ---------------------------------------------------------------------------

def build_bank(font, fallback, stats_path, trims_in, out_path):
    cmd = [sys.executable, 'gmbank_build.py', font,
           '--stats', stats_path, '--out', out_path,
           '--report', f'{TMP}/build_report.txt']
    if trims_in:
        cmd += ['--trims', trims_in]
    if fallback:
        cmd += ['--fallback', fallback]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f'gmbank_build failed:\n{r.stdout}\n{r.stderr}')


def patch_gains(path, corrections):
    """corrections: {absolute region index: multiply-factor}.  Rewrites
    only the gain field of the affected region rows."""
    with open(path, 'r+b') as f:
        data = f.read()
        _, _, nreg, table_off, _, _ = struct.unpack_from(fmt.HDR_FMT,
                                                         data, 0)
        for idx, factor in corrections.items():
            off = table_off + idx * fmt.REGION_SIZE
            r = fmt.unpack_region(data, off)
            r['gain'] *= factor
            f.seek(off)
            f.write(fmt.pack_region(r))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    argv = sys.argv[1:]
    font = None
    bank_in = None
    fallback = None
    stats_path = 'corpus_stats.json'
    trims_in = None
    out_path = 'gmbank.bin'
    gains_out = 'zone_gains.json'
    rep_path = 'calib_report.txt'
    iters = 6
    tol = 1.0
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--bank':
            bank_in = argv[i + 1]; i += 2
        elif a == '--fallback':
            fallback = argv[i + 1]; i += 2
        elif a == '--stats':
            stats_path = argv[i + 1]; i += 2
        elif a == '--trims-in':
            trims_in = argv[i + 1]; i += 2
        elif a == '--out':
            out_path = argv[i + 1]; i += 2
        elif a == '--gains-out':
            gains_out = argv[i + 1]; i += 2
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
    refw = f'{TMP}/calib_ref.wav'
    if os.path.exists(refw):               # stale ref from another run
        os.remove(refw)

    if bank_in:
        if os.path.abspath(bank_in) != os.path.abspath(out_path):
            shutil.copyfile(bank_in, out_path)
        print(f'calibrate: starting from {bank_in}')
    else:
        print('calibrate: building initial bank...')
        build_bank(font, fallback, load_stats(stats_path), trims_in,
                   out_path)

    bank = fmt.Bank(out_path)
    events, slots = build_slots(bank)
    nzones = sum(1 for s in slots if s[1] == 'zone')
    print(f'calibrate: {nzones} melodic zones + '
          f'{len(slots) - nzones} drum keys, '
          f'{slots[-1][5]:.0f}s score')

    cum = {}                               # regidx -> cumulative factor
    first = None
    rows = None
    for it in range(iters + 1):
        print(f'-- iteration {it}: rendering & measuring...')
        rows, med = residuals(measure(out_path, font, events, slots))
        if first is None:
            first = {r['label']: r['resid'] for r in rows}

        audible = [r for r in rows if r['resid'] is not None]
        worst = max(abs(r['resid']) for r in audible)
        nbad = sum(1 for r in audible if abs(r['resid']) > tol)
        print(f'   median gain offset {med:+.1f} dB, worst residual '
              f'{worst:.1f} dB, {nbad}/{len(audible)} outside +-{tol} dB')
        if nbad == 0 or it == iters:
            break

        corrections = {}
        for r in audible:
            if abs(r['resid']) <= tol * 0.5:
                continue
            corr = 10.0 ** (-r['resid'] / 20.0)
            corr = min(CLAMP_HI, max(CLAMP_LO, corr))
            old = cum.get(r['regidx'], 1.0)
            new = min(CUM_HI, max(CUM_LO, old * corr))
            if new != old:
                corrections[r['regidx']] = new / old
                cum[r['regidx']] = new
        patch_gains(out_path, corrections)

    # Outputs

    recipe = {r['label']: round(cum.get(r['regidx'], 1.0), 4)
              for r in rows}
    json.dump(recipe, open(gains_out, 'w'), indent=1)

    lines = [f'bank_calibrate: {font} -> {out_path} (zone-level)',
             f'tolerance +-{tol} dB (residual = our-ref level, '
             f'melodic-zone-median centered)',
             '',
             f'{"slot":>8s} {"key":>4s} {"ref_dBFS":>9s} {"factor":>7s} '
             f'{"before":>7s} {"after":>7s}  verdict']
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
        fv = lambda v: f'{v:+7.1f}' if v is not None else '      -'
        ref_db = f'{r["ref_db"]:9.1f}' if r['ref_db'] is not None \
            else '        -'
        lines.append(f'{r["label"]:>8s} {r["note"]:4d} {ref_db} '
                     f'{cum.get(r["regidx"], 1.0):7.3f} {fv(b)} '
                     f'{fv(r["resid"])}  {verdict}')
    lines.append('')
    lines.append(f'RESULT: {npass} within tolerance, {nfail} failing, '
                 f'{nsilent} absent in font')
    open(rep_path, 'w').write('\n'.join(lines) + '\n')

    print(f'\nzone gain factors -> {gains_out} ({len(cum)} patched)')
    print(f'report -> {rep_path}')
    print(lines[-1])
    print(f'listen: {TMP}/calib_ref.wav vs {TMP}/calib_our.wav')
    sys.exit(0 if nfail == 0 else 1)


if __name__ == '__main__':
    main()
