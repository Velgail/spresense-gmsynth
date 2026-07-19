#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""Event-level SMF parser for the reference renderer: merged tracks,
tempo map applied, times in seconds.  Hardened against the corpus's
broken files (keys clamped, runaway durations rejected, MThd searched).

parse(path) -> {
  'events': [(t, kind, ch, a, b)]  kind in 'on off prog cc bend'
             (bend: a = value-8192, b = 0)
  'duration': seconds
  'loop_s': CC111 time or None (RPG Maker loop start)
  'xg': True if Yamaha sysex present.  Decides what bank MSB 127
        means: XG files use it for drum parts, but WITHOUT XG sysex
        it is the GS "MT-32 compatibility map" (battle05ao.mid: ch4
        bank127 prog48 = MT-32 Str Sect, NOT a drum kit).
  'gs': True if Roland sysex present.
}
"""

from collections import namedtuple

MAX_DURATION = 3600.0


def _read_vlq(data, i):
    v = 0
    while True:
        b = data[i]
        i += 1
        v = (v << 7) | (b & 0x7F)
        if not (b & 0x80):
            return v, i


class _Track:
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
        d, self.pos = _read_vlq(self.data, self.pos)
        self.next_tick += d


def parse(path):
    raw = open(path, 'rb').read()
    off = raw.find(b'MThd')
    if off < 0:
        raise ValueError('no MThd')
    ntrk = int.from_bytes(raw[off + 10:off + 12], 'big')
    div = int.from_bytes(raw[off + 12:off + 14], 'big')
    if div == 0 or div & 0x8000:
        raise ValueError('unsupported division')

    tracks = []
    i = off + 14
    for _ in range(ntrk):
        j = raw.find(b'MTrk', i)
        if j < 0:
            break
        ln = int.from_bytes(raw[j + 4:j + 8], 'big')
        tracks.append(_Track(raw[j + 8:j + 8 + ln]))
        i = j + 8 + ln
    if not tracks:
        raise ValueError('no tracks')

    tempo = 500000
    last_tick = 0
    now = 0.0
    events = []
    loop_s = None
    xg = False
    gs = False

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
        if now > MAX_DURATION:
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
            ln, p = _read_vlq(data, p)
            if meta == 0x51:
                tempo = int.from_bytes(data[p:p + 3], 'big')
            if meta == 0x2F:
                t.done = True
            p += ln
        elif st in (0xF0, 0xF7):
            ln, p = _read_vlq(data, p)
            if ln > 0:
                if data[p] == 0x43:
                    xg = True
                elif data[p] == 0x41:
                    gs = True
            p += ln
        else:
            ch = st & 0x0F
            hi = st & 0xF0
            if hi in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                a = data[p] & 0x7F
                b = data[p + 1] & 0x7F
                p += 2
            else:
                a = data[p] & 0x7F
                b = 0
                p += 1

            if hi == 0x90 and b > 0:
                events.append((now, 'on', ch, a, b))
            elif hi == 0x80 or (hi == 0x90 and b == 0):
                events.append((now, 'off', ch, a, 0))
            elif hi == 0xB0:
                if a == 111 and loop_s is None:
                    loop_s = now
                events.append((now, 'cc', ch, a, b))
            elif hi == 0xC0:
                events.append((now, 'prog', ch, a, 0))
            elif hi == 0xE0:
                events.append((now, 'bend', ch, (a | (b << 7)) - 8192, 0))

        t.pos = p
        t.advance()

    return {'events': events, 'duration': now, 'loop_s': loop_s,
            'xg': xg, 'gs': gs}
