#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""Prescan SMF files: programs used, note stats, polyphony demand.

Usage: python3 midi_scan.py <dir-or-files...>

For each file reports:
  - format/tracks/division, duration
  - channels and (channel, program) pairs actually sounding
  - drum keys used (ch10)
  - max instantaneous polyphony (note-on .. note-off)
  - max polyphony with CC64 sustain held notes included
  - pitch bend / CC usage summary (which CCs appear)

This is the offline model of the on-device prescan pass that decides
which gmbank regions to load into the shared sample pool.
"""

import sys
import os
import glob


def read_vlq(data, i):
    v = 0
    while True:
        b = data[i]
        i += 1
        v = (v << 7) | (b & 0x7F)
        if not (b & 0x80):
            return v, i


class Track:
    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.running = 0
        self.done = False
        self.next_tick = 0
        self.advance()  # reads first delta

    def advance(self):
        if self.pos >= len(self.data):
            self.done = True
            return
        d, self.pos = read_vlq(self.data, self.pos)
        self.next_tick += d


def scan(path):
    raw = open(path, 'rb').read()
    if raw[:4] != b'MThd':
        return None
    fmt = int.from_bytes(raw[8:10], 'big')
    ntrk = int.from_bytes(raw[10:12], 'big')
    div = int.from_bytes(raw[12:14], 'big')

    tracks = []
    i = 14
    for _ in range(ntrk):
        assert raw[i:i + 4] == b'MTrk', 'bad track header'
        ln = int.from_bytes(raw[i + 4:i + 8], 'big')
        tracks.append(Track(raw[i + 8:i + 8 + ln]))
        i += 8 + ln

    # Merge tracks by tick, decode events

    programs = {}          # ch -> set(progs)
    cur_prog = {}
    drum_keys = set()
    ccs_seen = set()
    bend_chs = set()
    channels = set()
    nnotes = 0

    active = {}            # (ch, note) -> count
    sustained = set()      # (ch, note) released while pedal down
    pedal = {}             # ch -> bool
    maxpoly = 0
    maxpoly_sus = 0
    maxpoly_tick = 0

    tempo = 500000
    tempo_map = []         # (tick, tempo)
    end_tick = 0

    while True:
        t = None
        tick = None
        for tr in tracks:
            if not tr.done and (tick is None or tr.next_tick < tick):
                tick = tr.next_tick
                t = tr
        if t is None:
            break

        data = t.data
        p = t.pos
        st = data[p]
        if st & 0x80:
            p += 1
            if st < 0xF0:
                t.running = st
        else:
            st = t.running

        if st == 0xFF:
            meta = data[p]
            p += 1
            ln, p = read_vlq(data, p)
            if meta == 0x51:
                tempo = int.from_bytes(data[p:p + 3], 'big')
                tempo_map.append((tick, tempo))
            if meta == 0x2F:
                t.done = True
            p += ln
        elif st in (0xF0, 0xF7):
            ln, p = read_vlq(data, p)
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
                channels.add(ch)
                nnotes += 1
                prog = cur_prog.get(ch, 0)
                programs.setdefault(ch, set()).add(prog)
                if ch == 9:
                    drum_keys.add(a)
                active[(ch, a)] = active.get((ch, a), 0) + 1
                np = sum(active.values())
                if np > maxpoly:
                    maxpoly = np
                    maxpoly_tick = tick
                nps = np + len(sustained)
                if nps > maxpoly_sus:
                    maxpoly_sus = nps
            elif hi == 0x80 or (hi == 0x90 and b == 0):
                k = (ch, a)
                if k in active:
                    if pedal.get(ch):
                        sustained.add(k)
                    active[k] -= 1
                    if active[k] <= 0:
                        del active[k]
                nps = sum(active.values()) + len(sustained)
                if nps > maxpoly_sus:
                    maxpoly_sus = nps
            elif hi == 0xB0:
                ccs_seen.add(a)
                if a == 64:
                    if b >= 64:
                        pedal[ch] = True
                    else:
                        pedal[ch] = False
                        sustained.clear()
            elif hi == 0xC0:
                cur_prog[ch] = a
            elif hi == 0xE0:
                bend_chs.add(ch)

        t.pos = p
        end_tick = max(end_tick, tick)
        t.advance()

    # Duration from tempo map

    dur = 0.0
    last_tick = 0
    last_tempo = 500000
    for tk, tp in sorted(tempo_map):
        dur += (tk - last_tick) * last_tempo / div / 1e6
        last_tick, last_tempo = tk, tp
    dur += (end_tick - last_tick) * last_tempo / div / 1e6

    nprogs = sorted(set(p for s in programs.values()
                        for p in s if True))
    melodic = sorted(set(p for ch, s in programs.items()
                         for p in s if ch != 9))
    return {
        'fmt': fmt, 'ntrk': ntrk, 'div': div, 'dur': dur,
        'channels': sorted(channels), 'melodic_progs': melodic,
        'drum_keys': sorted(drum_keys), 'nnotes': nnotes,
        'maxpoly': maxpoly, 'maxpoly_sus': maxpoly_sus,
        'ccs': sorted(ccs_seen), 'bend': sorted(bend_chs),
    }


def main():
    paths = []
    for a in sys.argv[1:]:
        if os.path.isdir(a):
            paths += sorted(glob.glob(os.path.join(a, '*.mid')) +
                            glob.glob(os.path.join(a, '*.MID')))
        else:
            paths.append(a)

    all_progs = set()
    all_drums = set()
    for path in paths:
        try:
            r = scan(path)
        except Exception as e:
            print(f'{os.path.basename(path)}: PARSE ERROR {e}')
            continue
        if r is None:
            print(f'{os.path.basename(path)}: not SMF')
            continue
        all_progs |= set(r['melodic_progs'])
        all_drums |= set(r['drum_keys'])
        print(f"{os.path.basename(path)}")
        print(f"  fmt{r['fmt']} trk{r['ntrk']} div{r['div']} "
              f"{r['dur']:.1f}s notes={r['nnotes']} "
              f"poly={r['maxpoly']} poly+sus={r['maxpoly_sus']}")
        print(f"  ch={r['channels']}")
        print(f"  progs({len(r['melodic_progs'])})={r['melodic_progs']}")
        print(f"  drums({len(r['drum_keys'])})={r['drum_keys']}")
        print(f"  ccs={r['ccs']} bend_ch={r['bend']}")

    print(f"\nTOTAL distinct melodic programs: {len(all_progs)} {sorted(all_progs)}")
    print(f"TOTAL distinct drum keys: {len(all_drums)} {sorted(all_drums)}")


if __name__ == '__main__':
    main()
