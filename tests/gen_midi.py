#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""Generate the fixed SMF cases for the C event-trace regression
(tests/trace_regression.py).  Self-contained SMF writer: multi-track,
tempo metas, and raw byte events so running status can be exercised
deliberately.

Timing: div=480, default tempo 500000 -> 1 tick = 500000/480 us
= 50 output frames at 48kHz; one 512-frame block is ~10.24 ticks.
"""

import os
import struct
import sys

DIV = 480
OUT = os.path.join(os.path.dirname(__file__), 'midi')


def vlq(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


def on(ch, note, vel=100):
    return bytes([0x90 | ch, note, vel])


def off(ch, note):
    return bytes([0x80 | ch, note, 64])


def cc(ch, num, val):
    return bytes([0xB0 | ch, num, val])


def prog(ch, p):
    return bytes([0xC0 | ch, p])


def bend(ch, val14):
    return bytes([0xE0 | ch, val14 & 0x7F, (val14 >> 7) & 0x7F])


def tempo(usq):
    return b'\xff\x51\x03' + usq.to_bytes(3, 'big')


def track(evs):
    """evs: [(tick, raw event bytes)].  Stable order is preserved for
    equal ticks (merge-order test relies on it)."""
    body = bytearray()
    last = 0
    for tick, ev in sorted(evs, key=lambda e: e[0]):
        body += vlq(tick - last) + ev
        last = tick
    body += b'\x00\xff\x2f\x00'
    return b'MTrk' + struct.pack('>I', len(body)) + bytes(body)


def write(name, tracks, fmt=None):
    if fmt is None:
        fmt = 0 if len(tracks) == 1 else 1
    with open(os.path.join(OUT, name), 'wb') as f:
        f.write(b'MThd' + struct.pack('>IHHH', 6, fmt, len(tracks), DIV))
        for t in tracks:
            f.write(t)
    print(f'  {name}')


def main():
    os.makedirs(OUT, exist_ok=True)

    # 1: CC7 -> NOTE_ON -> CC7 inside one 512-frame block: the note
    # must not be delayed past the later CC (the coalesce bug).
    write('01_cc_order.mid', [track([
        (0, prog(0, 0)), (0, cc(0, 7, 100)),
        (2, on(0, 60)), (4, cc(0, 7, 30)),
        (48, off(0, 60)), (52, cc(0, 7, 110)),
        (100, on(0, 62)), (148, off(0, 62)),
    ])])

    # 2: PITCH_BEND -> NOTE_ON -> PITCH_BEND inside one block.
    write('02_bend_order.mid', [track([
        (0, prog(0, 0)), (0, bend(0, 8192)),
        (2, on(0, 60)), (4, bend(0, 12288)),
        (48, off(0, 60)), (52, bend(0, 8192)),
    ])])

    # 3: NOTE_OFF under sustain is held; pedal-up releases it.
    write('03_sustain.mid', [track([
        (0, prog(0, 0)), (0, on(0, 60)),
        (10, cc(0, 64, 127)), (20, off(0, 60)),
        (40, cc(0, 64, 0)),
        (60, on(0, 62)), (70, off(0, 62)),
    ])])

    # 4: same-note retrigger while releasing -> self-steal.
    write('04_self_steal.mid', [track([
        (0, prog(0, 0)), (0, on(0, 60)),
        (5, off(0, 60)), (8, on(0, 60)),
        (30, off(0, 60)),
    ])])

    # 5: 80 held notes -> global steal beyond 64 voices.
    write('05_global_steal.mid', [track(
        [(0, prog(0, 48))] +
        [(i * 4, on(0, 24 + i)) for i in range(80)])])

    # 6: 80 notes within the 60ms attack guard window.
    write('06_attack_guard.mid', [track(
        [(0, prog(0, 48))] +
        [(i // 2, on(0, 24 + i)) for i in range(80)])])

    # 7: open hi-hat choked by closed hi-hat (exclusive class), then
    # repeated open hats.
    write('07_drum_choke.mid', [track([
        (0, on(9, 46)), (4, on(9, 42)),
        (20, on(9, 46)), (24, on(9, 46)),
    ])])

    # 8: CC111 loop marker; run the trace with --loop to exercise
    # loop_rewind() and its state fast-forward.
    write('08_cc111_loop.mid', [track([
        (0, prog(0, 0)), (0, on(0, 60)), (5, off(0, 60)),
        (10, cc(0, 111, 0)),
        (12, on(0, 64)), (16, off(0, 64)),
        (20, on(0, 67)), (24, off(0, 67)),
    ])])

    # 9: event overflow: 64+ voices held, then a 16-channel CC7 flood
    # at distinct offsets (>96 CHGAINs per worker block) plus note-ons
    # appended after the block is full -- the ledger must stay
    # consistent when STARTs are dropped (summary wcount/ev_dropped).
    write('09_overflow.mid', [track(
        [(0, prog(0, 48))] +
        [(i, on(0, 24 + i)) for i in range(70)] +
        [(100 + t, cc(ch, 7, 20 + (t * 16 + ch) % 100))
         for t in range(20) for ch in range(16)] +
        [(110, on(0, 100)), (111, on(0, 101)), (112, on(0, 102))])])

    # 10: tempo change mid-song: frame timestamps must follow.
    write('10_tempo_change.mid', [track([
        (0, prog(0, 0)),
        (0, on(0, 60)), (24, off(0, 60)),
        (48, on(0, 62)), (72, off(0, 62)),
        (96, tempo(250000)),
        (96, on(0, 64)), (120, off(0, 64)),
        (144, on(0, 65)), (168, off(0, 65)),
    ])])

    # 11: running status (also across a meta event) and NoteOn vel=0
    # as NoteOff.
    write('11_running_status.mid', [track([
        (0, on(0, 60)),
        (10, bytes([0x3E, 0x64])),          # running-status NoteOn 62
        (15, b'\xff\x01\x02hi'),            # meta text between
        (20, bytes([0x3C, 0x00])),          # vel 0 -> NoteOff 60
        (25, bytes([0x3E, 0x00])),          # vel 0 -> NoteOff 62
    ])])

    # 12: format 1, three tracks with events on the same ticks: the
    # k-way merge order must be stable.
    write('12_multitrack.mid', [
        track([(0, tempo(500000)), (0, prog(0, 0)),
               (0, on(0, 60)), (20, off(0, 60))]),
        track([(0, prog(1, 24)), (0, on(1, 64)), (20, off(1, 64))]),
        track([(0, prog(2, 40)), (0, on(2, 67)), (20, off(2, 67))]),
    ], fmt=1)

    # 13: RPN 0,0 sets bend range 12; half-up bend -> factor 2^(6/12).
    write('13_rpn_bendrange.mid', [track([
        (0, prog(0, 0)),
        (0, cc(0, 101, 0)), (1, cc(0, 100, 0)), (2, cc(0, 6, 12)),
        (4, on(0, 60)), (8, bend(0, 12288)),
        (30, off(0, 60)),
    ])])

    # 14: RELEASE/KILL inside a full block: 64+ held voices, an open
    # hat, then a CC flood that overflows the block WHILE note-offs and
    # a hi-hat choke land in it.  The dropped releases must leave the
    # ledger in the playing state (consistent with the worker); the
    # off at tick 130 lands after the flood and must succeed.
    write('14_overflow_release.mid', [track(
        [(0, prog(0, 48))] +
        [(i, on(0, 24 + i)) for i in range(70)] +
        [(90, on(9, 46))] +
        [(100 + t, cc(ch, 7, 20 + (t * 16 + ch) % 100))
         for t in range(20) for ch in range(16)] +
        [(110, off(0, 24)), (110, off(0, 25)), (111, off(0, 26)),
         (111, on(9, 42)),
         (130, off(0, 30))])])


if __name__ == '__main__':
    main()
    print('gen_midi: done ->', OUT)
    sys.exit(0)
