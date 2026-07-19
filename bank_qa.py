#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""Bank QA: render every program (and drum key) through the SAME voice
code as the reference renderer and verify offline:

  - f0 accuracy: autocorrelation vs equal temperament for the MIDI note
    (catches root/tune/scale/rate bugs like the Phase3/4 gen51/52
    incidents) -- pitched families must be within tolerance
  - audibility: RMS level per program (catches the gen48 signed
    attenuation bug class: an instrument that renders silence)
  - gain uniformity: reports outliers vs the median for TRIM tuning

Usage: bank_qa.py <gmbank.bin> [--verbose]
"""

import math
import sys

import numpy as np

from gmbank_format import Bank, DRUM_SLOT
from gmrender import RegionData, Voice, OUT_RATE

# Families where autocorrelation vs equal temperament is not a fair
# test (inharmonic / atonal / noise): report but never fail.

WARN_ONLY_PROGS = set(range(112, 128)) | set(range(96, 104)) | {47}
BELL_PROGS = set(range(8, 16))         # inharmonic-ish: wide tolerance

CENTS_FAIL = 60
CENTS_FAIL_BELL = 120
SILENCE_DBFS = -55.0


def render_note(rd, note, seconds=0.6):
    v = Voice(rd, 0, note, 100, 1.0, False)
    n = int(seconds * OUT_RATE)
    out = np.zeros(n, dtype=np.float32)
    j = 0
    while j < n and not v.dead:
        k = min(512, n - j)
        s = v.render(k)
        if s is None:
            break
        out[j:j + k] = s
        j += k
    return out


def best_window(x, wlen):
    """Highest-energy window of wlen samples, skipping the attack."""
    skip = min(len(x) // 4, int(0.05 * OUT_RATE))
    x = x[skip:]
    if len(x) <= wlen:
        return x
    e = np.cumsum(x.astype(np.float64) ** 2)
    tot = e[wlen:] - e[:-wlen]
    i = int(np.argmax(tot))
    return x[i:i + wlen]


def measure_cents(x, f_expect):
    """Pitch deviation (cents) from f_expect via the spectral peak in a
    +/-100 cent window around the expected fundamental (falls back to
    the 2nd harmonic when the fundamental is weak).  FFT peaks are
    immune to the traps that broke autocorrelation here: inharmonic
    partials (sitar +9.5 semis), leslie modulation sidebands, and tiny
    lags at high notes.  Returns (cents, quality) or (None, reason)."""
    x = x - np.mean(x)
    if float(np.max(np.abs(x))) < 1e-5:
        return None, 'silent'
    n = len(x)
    win = np.hanning(n).astype(np.float32)
    nfft = 1
    while nfft < 8 * n:
        nfft *= 2
    mag = np.abs(np.fft.rfft(x * win, nfft))
    df = OUT_RATE / nfft

    def peak_near(fc):
        lo = int(fc * 2 ** (-100 / 1200.0) / df)
        hi = int(fc * 2 ** (100 / 1200.0) / df) + 1
        if hi >= len(mag) - 1 or hi <= lo + 2 or lo < 1:
            return None, 0.0
        i = int(np.argmax(mag[lo:hi])) + lo
        if i <= lo + 1 or i >= hi - 2:
            return None, 0.0    # pinned to the window edge: not a peak
        y0, y1, y2 = mag[i - 1], mag[i], mag[i + 1]
        denom = (y0 - 2 * y1 + y2)
        d = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-12 else 0.0
        f = (i + max(-0.5, min(0.5, d))) * df
        floor = float(np.median(mag[max(1, int(fc * 0.5 / df)):
                                    int(fc * 3.0 / df)]))
        return f, (float(mag[i]) / floor if floor > 0 else 0.0)

    # Below ~150Hz the FFT lobe is wider than the +/-100c search window
    # (a 55Hz root measured fine while root+2 semis "failed" by 65c),
    # so measure a higher harmonic instead: same cents offset, k times
    # the Hz resolution.

    k = max(1, int(math.ceil(150.0 / f_expect)))
    for mult in (k, 2 * k):
        f, snr = peak_near(mult * f_expect)
        if f is not None and snr >= 4.0:
            return 1200.0 * math.log2(f / (mult * f_expect)), snr
    return None, 'aperiodic'


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        sys.exit(__doc__)

    bank_path = sys.argv[1]
    verbose = '--verbose' in sys.argv
    bank = Bank(bank_path)

    fails = []
    warns = []
    silent = []
    rms_list = []

    print('== melodic programs ==')
    for prog in range(128):
        regs = bank.prog_regions(prog)
        if not regs:
            fails.append(('prog', prog, 'no regions'))
            continue
        worst_cents = 0.0
        worst_note = -1
        prog_rms = -120.0
        aperiodic = 0
        ntested = 0
        for reg in regs:
            rd = RegionData(bank, reg)
            span = reg['hikey'] - reg['lokey']
            notes = {max(24, min(96, reg['root'])),
                     max(24, min(108, reg['lokey'] + span // 2))}
            for note in notes:
                if not (reg['lokey'] <= note <= reg['hikey']):
                    continue
                x = render_note(rd, note, seconds=0.8)
                w = best_window(x, int(0.35 * OUT_RATE))
                rms = float(np.sqrt(np.mean(w.astype(np.float64) ** 2)))
                db = 20 * math.log10(max(rms, 1e-9))
                prog_rms = max(prog_rms, db)

                # The builder auto-retunes every pitched region to land
                # on equal temperament, so the QA expectation is ET
                # plus only the intentional scaleTuning stretch
                # (e.g. woodblock scale=50).

                design_cents = (reg['scale'] - 100) * \
                    (note - reg['root'])
                f_design = 440.0 * 2.0 ** ((note - 69) / 12.0 +
                                           design_cents / 1200.0)
                cents, q = measure_cents(w, f_design)
                ntested += 1
                if cents is None:
                    if q == 'aperiodic':
                        aperiodic += 1
                    continue
                if abs(cents) > abs(worst_cents):
                    worst_cents = cents
                    worst_note = note
        rms_list.append((prog, prog_rms))
        limit = CENTS_FAIL_BELL if prog in BELL_PROGS else CENTS_FAIL
        status = 'ok'
        if prog_rms < SILENCE_DBFS:
            silent.append(prog)
            status = 'SILENT'
        elif abs(worst_cents) > limit or \
                (aperiodic == ntested and ntested > 0):
            reason = '%+.0f cents @note %d' % (worst_cents, worst_note) \
                if abs(worst_cents) > limit else 'no periodicity'
            if prog in WARN_ONLY_PROGS or \
                    (prog in BELL_PROGS and aperiodic == ntested):
                warns.append((prog, worst_cents, worst_note))
                status = 'warn'
            else:
                fails.append(('prog', prog, reason))
                status = 'FAIL'
        if verbose or status not in ('ok',):
            print('prog %3d: worst %+6.1f cents @%3d (aper %d/%d), '
                  'rms %6.1f dBFS %s'
                  % (prog, worst_cents, worst_note, aperiodic, ntested,
                     prog_rms, status))

    print('== drums ==')
    dsilent = []
    for reg in bank.prog_regions(DRUM_SLOT):
        rd = RegionData(bank, reg)
        x = render_note(rd, reg['lokey'], seconds=0.4)
        rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
        db = 20 * math.log10(max(rms, 1e-9))
        if db < SILENCE_DBFS:
            dsilent.append(reg['lokey'])
        if verbose:
            print('drum %3d: rms %6.1f dBFS' % (reg['lokey'], db))

    dbs = sorted(d for _, d in rms_list if d > -119)
    med = dbs[len(dbs) // 2]
    print('\n== summary ==')
    print('median melodic RMS: %.1f dBFS' % med)
    loud = [(p, d) for p, d in rms_list if d - med > 8]
    quiet = [(p, d) for p, d in rms_list if med - d > 8 and d > -119]
    print('gain outliers (>+8dB): %s' %
          [('p%d' % p, round(d - med, 1)) for p, d in loud])
    print('gain outliers (<-8dB): %s' %
          [('p%d' % p, round(d - med, 1)) for p, d in quiet])
    print('silent programs: %s' % silent)
    print('silent drum keys: %s' % dsilent)
    print('pitch FAILs: %s' % [f for f in fails])
    print('pitch warns (atonal families): %s' %
          [('p%d' % p, round(c)) for p, c, _ in warns])
    print('QA %s' % ('PASS' if not fails and not silent and not dsilent
                     else 'NEEDS ATTENTION'))

    # Trim suggestions: pull outliers back to a +/-8dB band around the
    # median (no boost for the naturally-quiet SFX family)

    if '--trims-out' in sys.argv:
        import json
        path = sys.argv[sys.argv.index('--trims-out') + 1]
        trims = {}
        for p, d in rms_list:
            if d <= -119:
                continue
            dev = d - med
            if dev > 8:
                trims[p] = round(10.0 ** (-(dev - 8) / 20.0), 3)
            elif dev < -8 and p < 120:
                trims[p] = round(10.0 ** ((-dev - 8) / 20.0), 3)
        json.dump(trims, open(path, 'w'), indent=1)
        print('trim suggestions -> %s (%d programs)' % (path, len(trims)))


if __name__ == '__main__':
    main()
