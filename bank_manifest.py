#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""Structural regression guard for gmbank.bin.

The calibration score in bank_calibrate.py is generated FROM the
bank's own region table, so a region that silently disappears (builder
regression, tier misclassification, key-range shrink) also disappears
from the test -- calibration alone cannot catch structural loss.  This
tool pins the bank's structure to a committed manifest instead:

  region count per program, drum key set, per-program key coverage,
  looped-region count, sample-rate histogram, total size.

Usage:
  bank_manifest.py gmbank.bin --write [--manifest tests/expected/bank_manifest.json]
      extract the manifest from a known-good bank and save it

  bank_manifest.py gmbank.bin [--manifest tests/expected/bank_manifest.json]
                              [--size-tol 0.10]
      compare a freshly built bank against the manifest; exit 1 on any
      structural mismatch.  Counts, keys and ranges must match exactly;
      total_bytes may drift by --size-tol (fraction, default 0.10)
      because ADPCM payload size moves with encoder tweaks.
"""

import json
import os
import sys

import gmbank_format as fmt

DEF_MANIFEST = 'tests/expected/bank_manifest.json'
DRUM_SLOT = 128


def extract(bank_path):
    bank = fmt.Bank(bank_path)
    m = {
        'format_version': 1,
        'nregions': bank.nregions,
        'total_bytes': len(bank.data),
        'program_region_counts': {},
        'drum_keys': [],
        'key_ranges': {},
        'looped_regions': 0,
        'rate_histogram': {},
    }
    for slot in range(fmt.NPROG_SLOTS):
        regs = bank.prog_regions(slot)
        if not regs:
            continue
        m['program_region_counts'][str(slot)] = len(regs)
        if slot == DRUM_SLOT:
            m['drum_keys'] = sorted(r['lokey'] for r in regs)
        else:
            m['key_ranges'][str(slot)] = [min(r['lokey'] for r in regs),
                                          max(r['hikey'] for r in regs)]
    for r in bank.regions:
        if r['flags'] & fmt.FLAG_LOOPED:
            m['looped_regions'] += 1
        k = str(r['rate'])
        m['rate_histogram'][k] = m['rate_histogram'].get(k, 0) + 1
    return m


def compare(cur, exp, size_tol):
    """Returns a list of human-readable mismatch strings (empty = pass)."""
    bad = []

    def exact(key):
        if cur[key] != exp[key]:
            bad.append(f'{key}: manifest {exp[key]} != bank {cur[key]}')

    exact('format_version')
    exact('nregions')
    exact('looped_regions')

    lo = exp['total_bytes'] * (1.0 - size_tol)
    hi = exp['total_bytes'] * (1.0 + size_tol)
    if not lo <= cur['total_bytes'] <= hi:
        bad.append(f'total_bytes: {cur["total_bytes"]} outside '
                   f'{int(lo)}..{int(hi)} '
                   f'(manifest {exp["total_bytes"]} +-{size_tol:.0%})')

    if cur['drum_keys'] != exp['drum_keys']:
        lost = sorted(set(exp['drum_keys']) - set(cur['drum_keys']))
        new = sorted(set(cur['drum_keys']) - set(exp['drum_keys']))
        bad.append(f'drum_keys: lost {lost}, unexpected {new}')

    for name in ('program_region_counts', 'key_ranges', 'rate_histogram'):
        ce, ee = cur[name], exp[name]
        for k in sorted(set(ce) | set(ee), key=int):
            if ce.get(k) != ee.get(k):
                bad.append(f'{name}[{k}]: manifest {ee.get(k)} '
                           f'!= bank {ce.get(k)}')
    return bad


def main():
    argv = sys.argv[1:]
    bank_path = None
    manifest_path = DEF_MANIFEST
    write = False
    size_tol = 0.10
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--write':
            write = True; i += 1
        elif a == '--manifest':
            manifest_path = argv[i + 1]; i += 2
        elif a == '--size-tol':
            size_tol = float(argv[i + 1]); i += 2
        elif a in ('-h', '--help'):
            sys.exit(__doc__)
        else:
            bank_path = a; i += 1
    if bank_path is None:
        sys.exit(__doc__)

    cur = extract(bank_path)

    if write:
        os.makedirs(os.path.dirname(manifest_path) or '.', exist_ok=True)
        json.dump(cur, open(manifest_path, 'w'), indent=1, sort_keys=True)
        print(f'manifest: {bank_path} ({cur["nregions"]} regions, '
              f'{cur["total_bytes"]} bytes) -> {manifest_path}')
        return

    if not os.path.exists(manifest_path):
        sys.exit(f'manifest not found: {manifest_path} '
                 f'(create one with --write)')
    exp = json.load(open(manifest_path))
    bad = compare(cur, exp, size_tol)
    if bad:
        print(f'bank_manifest: {bank_path} FAILS {manifest_path}:')
        for b in bad:
            print(f'  {b}')
        sys.exit(1)
    print(f'bank_manifest: {bank_path} matches {manifest_path} '
          f'({cur["nregions"]} regions, {len(cur["drum_keys"])} drum keys)')


if __name__ == '__main__':
    main()
