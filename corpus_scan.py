#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""Corpus statistics over a large SMF collection, to drive gmsynth
design decisions:

  - program usage frequency (by file and by note) -> gmbank size/quality
    allocation across the 128 GM programs
  - per-program key ranges -> multisample region planning
  - drum key frequency -> drum kit content
  - voice demand: max simultaneous notes per file, computed in REAL TIME
    (tempo map applied) with sustain pedal held notes and a configurable
    release tail added -> validates the 64-voice budget
  - CC / pitch bend / RPN bend-range usage -> feature priority list
  - GS/XG sysex presence -> how far beyond plain GM the corpus goes

Usage: python3 corpus_scan.py <dir> [--tail 0.3] [--json out.json]
"""

import sys
import os
import glob
import json
from collections import defaultdict

TAIL = 0.3


def read_vlq(data, i):
    v = 0
    while True:
        b = data[i]
        i += 1
        v = (v << 7) | (b & 0x7F)
        if not (b & 0x80):
            return v, i


class Track:
    __slots__ = ('data', 'pos', 'running', 'done', 'next_tick')

    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.running = 0
        self.done = False
        self.next_tick = 0
        self.advance()

    def advance(self):
        if self.pos >= len(self.data):
            self.done = True
            return
        d, self.pos = read_vlq(self.data, self.pos)
        self.next_tick += d


def scan(path, tail):
    raw = open(path, 'rb').read()
    off = raw.find(b'MThd')
    if off < 0:
        raise ValueError('no MThd')
    fmt = int.from_bytes(raw[off + 8:off + 10], 'big')
    ntrk = int.from_bytes(raw[off + 10:off + 12], 'big')
    div = int.from_bytes(raw[off + 12:off + 14], 'big')
    if div == 0 or div & 0x8000:
        raise ValueError('SMPTE/zero division')

    tracks = []
    i = off + 14
    for _ in range(ntrk):
        j = raw.find(b'MTrk', i)
        if j < 0:
            break
        ln = int.from_bytes(raw[j + 4:j + 8], 'big')
        tracks.append(Track(raw[j + 8:j + 8 + ln]))
        i = j + 8 + ln
    if not tracks:
        raise ValueError('no tracks')

    tempo = 500000
    last_tick = 0
    now = 0.0

    cur_prog = {}
    pedal = {}
    rpn = {}
    active = {}            # (ch,note) -> [start_time,...]
    held = defaultdict(list)  # ch -> [(start,ch,note,prog)] pedal-held
    intervals = []         # (start, end, is_drum, prog_or_key)

    prog_notes = defaultdict(int)
    prog_keys = {}
    drum_notes = defaultdict(int)
    progs_used = set()
    ccs = set()
    cc64_press = 0
    bend_used = False
    bendranges = set()
    sysex_gs = False
    sysex_xg = False
    nnotes = 0

    while True:
        t = None
        tick = None
        for tr in tracks:
            if not tr.done and (tick is None or tr.next_tick < tick):
                tick = tr.next_tick
                t = tr
        if t is None:
            break

        now += (tick - last_tick) * tempo / div / 1e6
        last_tick = tick
        if now > 3600:
            raise ValueError('runaway duration')

        data = t.data
        p = t.pos
        st = data[p]
        if st & 0x80:
            p += 1
            if st < 0xF0:
                t.running = st
        else:
            st = t.running
            if st == 0:
                raise ValueError('running status w/o status')

        if st == 0xFF:
            meta = data[p]
            p += 1
            ln, p = read_vlq(data, p)
            if meta == 0x51:
                tempo = int.from_bytes(data[p:p + 3], 'big')
            if meta == 0x2F:
                t.done = True
            p += ln
        elif st in (0xF0, 0xF7):
            ln, p = read_vlq(data, p)
            if ln > 0:
                vendor = data[p]
                if vendor == 0x41:
                    sysex_gs = True
                elif vendor == 0x43:
                    sysex_xg = True
            p += ln
        else:
            ch = st & 0x0F
            hi = st & 0xF0
            if hi in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                a = data[p]
                b = data[p + 1]
                p += 2
            else:
                a = data[p]
                b = 0
                p += 1

            if hi == 0x90 and b > 0:
                nnotes += 1
                if ch == 9:
                    drum_notes[a] += 1
                else:
                    prog = cur_prog.get(ch, 0)
                    progs_used.add(prog)
                    prog_notes[prog] += 1
                    k = prog_keys.get(prog)
                    if k is None:
                        prog_keys[prog] = [a, a]
                    else:
                        if a < k[0]:
                            k[0] = a
                        if a > k[1]:
                            k[1] = a
                active.setdefault((ch, a), []).append(now)
            elif hi == 0x80 or (hi == 0x90 and b == 0):
                k = (ch, a)
                starts = active.get(k)
                if starts:
                    start = starts.pop()
                    if not starts:
                        del active[k]
                    if pedal.get(ch):
                        held[ch].append(start)
                    else:
                        intervals.append((start, now))
            elif hi == 0xB0:
                ccs.add(a)
                if a == 64:
                    if b >= 64:
                        if not pedal.get(ch):
                            cc64_press += 1
                        pedal[ch] = True
                    else:
                        pedal[ch] = False
                        for start in held[ch]:
                            intervals.append((start, now))
                        held[ch].clear()
                elif a == 101:
                    rpn[ch] = (b, rpn.get(ch, (127, 127))[1])
                elif a == 100:
                    rpn[ch] = (rpn.get(ch, (127, 127))[0], b)
                elif a == 6:
                    if rpn.get(ch) == (0, 0):
                        bendranges.add(b)
            elif hi == 0xC0:
                cur_prog[ch] = a
            elif hi == 0xE0:
                bend_used = True

        t.pos = p
        t.advance()

    # Close dangling notes / held notes at EOF

    for k, starts in active.items():
        for start in starts:
            intervals.append((start, now))
    for ch, lst in held.items():
        for start in lst:
            intervals.append((start, now))

    # Max overlap with release tail

    ev = []
    for start, end in intervals:
        ev.append((start, 1))
        ev.append((end + tail, -1))
    ev.sort()
    cur = 0
    peak = 0
    for _, d in ev:
        cur += d
        if cur > peak:
            peak = cur

    # Raw (no tail) peak

    ev = []
    for start, end in intervals:
        ev.append((start, 1))
        ev.append((end, -1))
    ev.sort()
    cur = 0
    peak_raw = 0
    for _, d in ev:
        cur += d
        if cur > peak_raw:
            peak_raw = cur

    return {
        'fmt': fmt, 'div': div, 'dur': now, 'size': len(raw),
        'nnotes': nnotes,
        'progs': sorted(progs_used),
        'prog_notes': dict(prog_notes),
        'prog_keys': {k: v for k, v in prog_keys.items()},
        'drum_notes': dict(drum_notes),
        'peak_raw': peak_raw, 'peak_tail': peak,
        'ccs': sorted(ccs), 'cc64_press': cc64_press,
        'bend': bend_used, 'bendranges': sorted(bendranges),
        'gs': sysex_gs, 'xg': sysex_xg,
    }


def pct(sorted_vals, q):
    if not sorted_vals:
        return 0
    i = min(len(sorted_vals) - 1, int(len(sorted_vals) * q))
    return sorted_vals[i]


def main():
    argv = sys.argv[1:]
    tail = TAIL
    jout = None
    paths = []
    i = 0
    while i < len(argv):
        if argv[i] == '--tail':
            tail = float(argv[i + 1])
            i += 2
        elif argv[i] == '--json':
            jout = argv[i + 1]
            i += 2
        else:
            d = argv[i]
            if os.path.isdir(d):
                paths += sorted(
                    p for p in glob.glob(os.path.join(d, '*'))
                    if p.lower().endswith(('.mid', '.midi', '.rmi')))
            else:
                paths.append(d)
            i += 1

    ok = 0
    failed = []
    sizes = []
    durs = []
    nprogs_per_file = []
    peaks_raw = []
    peaks_tail = []
    over64 = []
    prog_files = defaultdict(int)
    prog_notes_total = defaultdict(int)
    prog_key_lo = {}
    prog_key_hi = {}
    drum_files = defaultdict(int)
    drum_notes_total = defaultdict(int)
    cc_files = defaultdict(int)
    cc64_files = 0
    bend_files = 0
    bendrange_hist = defaultdict(int)
    gs_files = 0
    xg_files = 0
    results = []

    if not paths:
        sys.exit(__doc__)

    for path in paths:
        try:
            r = scan(path, tail)
        except Exception as e:
            failed.append((os.path.basename(path), str(e)))
            continue
        ok += 1
        sizes.append(r['size'])
        durs.append(r['dur'])
        nprogs_per_file.append(len(r['progs']))
        peaks_raw.append(r['peak_raw'])
        peaks_tail.append(r['peak_tail'])
        if r['peak_tail'] > 64:
            over64.append((os.path.basename(path), r['peak_tail']))
        for p1 in r['progs']:
            prog_files[p1] += 1
        for p1, n in r['prog_notes'].items():
            prog_notes_total[p1] += n
        for p1, (lo, hi) in r['prog_keys'].items():
            prog_key_lo[p1] = min(prog_key_lo.get(p1, 127), lo)
            prog_key_hi[p1] = max(prog_key_hi.get(p1, 0), hi)
        for k, n in r['drum_notes'].items():
            drum_files[k] += 1
            drum_notes_total[k] += n
        for c in r['ccs']:
            cc_files[c] += 1
        if r['cc64_press'] > 0:
            cc64_files += 1
        if r['bend']:
            bend_files += 1
        for br in r['bendranges']:
            bendrange_hist[br] += 1
        if r['gs']:
            gs_files += 1
        if r['xg']:
            xg_files += 1
        results.append({'file': os.path.basename(path), **{
            k: r[k] for k in ('dur', 'size', 'nnotes', 'peak_raw',
                              'peak_tail')},
            'nprogs': len(r['progs'])})

    n = ok
    print(f'files: {len(paths)} scanned, {ok} ok, {len(failed)} failed')
    if failed:
        for f, e in failed[:10]:
            print(f'  FAIL {f}: {e}')

    sizes.sort()
    durs.sort()
    nprogs_per_file.sort()
    peaks_raw.sort()
    peaks_tail.sort()

    print(f'\nsize KB: p50={pct(sizes,0.5)//1024} p95={pct(sizes,0.95)//1024} '
          f'p99={pct(sizes,0.99)//1024} max={sizes[-1]//1024}')
    print(f'duration s: p50={pct(durs,0.5):.0f} p95={pct(durs,0.95):.0f} '
          f'max={durs[-1]:.0f}')
    print(f'programs/file: p50={pct(nprogs_per_file,0.5)} '
          f'p95={pct(nprogs_per_file,0.95)} max={nprogs_per_file[-1]}')
    print(f'\npeak poly (raw): p50={pct(peaks_raw,0.5)} '
          f'p90={pct(peaks_raw,0.9)} p99={pct(peaks_raw,0.99)} '
          f'max={peaks_raw[-1]}')
    print(f'peak poly (+{tail}s tail): p50={pct(peaks_tail,0.5)} '
          f'p90={pct(peaks_tail,0.9)} p99={pct(peaks_tail,0.99)} '
          f'max={peaks_tail[-1]}')
    print(f'files needing >64 voices (with tail): {len(over64)} '
          f'({100.0*len(over64)/max(n,1):.1f}%)')
    for f, p in sorted(over64, key=lambda x: -x[1])[:10]:
        print(f'  {p:4d} {f}')

    print(f'\nGM program usage (top 40 by file count, {n} files):')
    top = sorted(prog_files.items(), key=lambda x: -x[1])[:40]
    for p1, c in top:
        print(f'  prog {p1:3d}: {c:5d} files ({100.0*c/n:.0f}%) '
              f'{prog_notes_total[p1]:8d} notes  '
              f'keys {prog_key_lo[p1]}-{prog_key_hi[p1]}')
    unused = [p1 for p1 in range(128) if prog_files.get(p1, 0) == 0]
    print(f'programs never used: {unused}')

    print(f'\ndrum keys (top 30 by file count):')
    for k, c in sorted(drum_files.items(), key=lambda x: -x[1])[:30]:
        print(f'  key {k:3d}: {c:5d} files ({100.0*c/n:.0f}%) '
              f'{drum_notes_total[k]:8d} notes')

    print(f'\nCC usage (files, top 25):')
    for c, cnt in sorted(cc_files.items(), key=lambda x: -x[1])[:25]:
        print(f'  CC{c:3d}: {cnt:5d} ({100.0*cnt/n:.0f}%)')
    print(f'CC64 actually pressed: {cc64_files} files '
          f'({100.0*cc64_files/max(n,1):.0f}%)')
    print(f'pitch bend used: {bend_files} files '
          f'({100.0*bend_files/max(n,1):.0f}%)')
    print(f'bend ranges seen: '
          f'{dict(sorted(bendrange_hist.items()))}')
    print(f'GS sysex: {gs_files} ({100.0*gs_files/max(n,1):.0f}%)  '
          f'XG sysex: {xg_files} ({100.0*xg_files/max(n,1):.0f}%)')

    if jout:
        with open(jout, 'w') as f:
            json.dump({
                'tail': tail,
                'prog_files': dict(prog_files),
                'prog_notes': dict(prog_notes_total),
                'prog_key_lo': prog_key_lo,
                'prog_key_hi': prog_key_hi,
                'drum_files': dict(drum_files),
                'drum_notes': dict(drum_notes_total),
                'cc_files': dict(cc_files),
                'bendranges': dict(bendrange_hist),
                'files': results,
            }, f, ensure_ascii=False)
        print(f'\nJSON written to {jout}')


if __name__ == '__main__':
    main()
