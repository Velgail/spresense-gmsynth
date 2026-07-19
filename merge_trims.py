#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""Merge trim tables for gmbank_build.py --trims.

The bank that ref_compare.py measured was built WITH the old trims
already applied, so the reference-derived factor multiplies on top of
whatever the old table had.  The drums aggregate (key "-1") expands to
per-key entries (1000+key).

Usage: merge_trims.py <old_trims.json> <ref_trims.json> <out.json>
"""

import json
import sys


def main():
    old = json.load(open(sys.argv[1])) if sys.argv[1] != '-' else {}
    ref = json.load(open(sys.argv[2]))
    out = {int(k): v for k, v in old.items()}

    for k, f in ref.items():
        k = int(k)
        if k == -1:
            for key in range(24, 88):
                kk = 1000 + key
                out[kk] = round(out.get(kk, 1.0) * f, 3)
        else:
            out[k] = round(out.get(k, 1.0) * f, 3)

    json.dump({str(k): v for k, v in sorted(out.items())},
              open(sys.argv[3], 'w'), indent=1)
    print(f'{len(out)} trim entries -> {sys.argv[3]}')


if __name__ == '__main__':
    main()
