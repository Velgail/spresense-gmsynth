#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""Build gmbank.bin: all 128 GM programs + the GM drum kit, extracted
from a SoundFont with a size/quality budget allocated from corpus
statistics (corpus_stats.json produced by corpus_scan.py).

Tiering: programs that appear in more files get more multisample zones,
higher sample rates and longer loops.  Key ranges are limited to what
the corpus actually plays (edge regions stretch to 0/127 so any note
still sounds, just pitch-shifted further).

Usage:
  gmbank_build.py <main.sf2> [--fallback other.sf2] [--stats corpus_stats.json]
                  [--out gmbank.bin] [--report gmbank_report.txt]
                  [--budget 3.5M]   (closed-loop: scale quality until it fits)
"""

import json
import math
import os
import sys

import numpy as np

from sf2parse import (SF2, s16, keyrange, tc2sec, adpcm_encode,
                      adpcm_decode, pack_nibbles, resample,
                      measure_pitch_cents,
                      G_PAN, G_ATTACK, G_DECAY, G_SUSTAIN, G_RELEASE,
                      G_VELRANGE, G_ATTEN, G_COARSE, G_FINE, G_SAMPLEID,
                      G_MODES, G_SCALETUNE, G_EXCLCLASS, G_ROOTKEY)
import gmbank_format as fmt

# ---------------------------------------------------------------------------
# Budget tables
# ---------------------------------------------------------------------------

# tier -> (zone_step_semitones, max_sustain_s, rate_scale)

TIERS = {
    'A': (6, 1.00, 1.15),
    'B': (8, 0.85, 1.00),
    'C': (11, 0.70, 0.90),
    'D': (15, 0.60, 0.80),
}


def tier_of(freq):
    if freq >= 0.15:
        return 'A'
    if freq >= 0.05:
        return 'B'
    if freq >= 0.01:
        return 'C'
    return 'D'


# Base sample rate per GM family (Hz), scaled by tier, clamped below.
# Bright timbres need the top octave; basses and pads do not.

FAMILY_RATE = {
    0: 18000,    # piano
    8: 22000,    # chromatic percussion (bells)
    16: 15000,   # organ
    24: 16000,   # guitar
    32: 13000,   # bass
    40: 16000,   # solo strings
    48: 15000,   # ensemble
    56: 18000,   # brass
    64: 16000,   # reed
    72: 16000,   # pipe
    80: 17000,   # synth lead
    88: 14000,   # synth pad
    96: 15000,   # synth FX
    104: 16000,  # ethnic
    112: 19000,  # percussive
    120: 16000,  # sound FX
}

RATE_MIN = 11000
RATE_MAX = 26000

# Release clamp per family (Phase4 lesson: long tails hog voices and
# cause audible stealing; 0.15-0.4s reads as natural on this engine).

FAMILY_RELEASE = {
    0: 0.30, 8: 0.35, 16: 0.15, 24: 0.30, 32: 0.20,
    40: 0.40, 48: 0.40, 56: 0.30, 64: 0.30, 72: 0.30,
    80: 0.30, 88: 0.40, 96: 0.40, 104: 0.30, 112: 0.25, 120: 0.40,
}

DECAY_CAP = 4.0

# Attack cap: Phase4's blanket 0.08s protected against voice stealing,
# but it guts slow swells (p49 slow strings measured env_corr 0.637 vs
# fluidsynth).  With 64 voices + attack-guarded stealing the sustained
# families can keep their swell.

ATTACK_CAP_FAST = 0.08
ATTACK_CAP_SLOW = 0.30
SLOW_ATTACK_FAMS = {16, 40, 48, 88, 96}

# One-step tier promotion for the programs the fluidsynth timbre sweep
# ranked worst (timbre_sweep.py): mostly organs, pads, FX and the weak
# solo strings/brass.

TIER_PROMOTE = {5, 7, 17, 44, 49, 51, 52, 58, 69, 96, 104, 107}
TIER_UP = {'D': 'C', 'C': 'B', 'B': 'A', 'A': 'A'}

# Per-program overrides for instruments whose character needs it.
# Timpani (47): the RING is the note -- keep 2.2s of natural decay and
# let it ring past note-off (Boss03's low timpani read as hollow with
# the generic 1.0s loop + 0.4s release).

PROG_MAXS = {47: 2.2}
PROG_RELEASE = {47: 1.2}

# Per-program linear mix trims (auto-suggested by bank_qa.py
# --trims-out, refined by listening passes like Phase4's PITCHED trims;
# drum keys use 1000+key).  Loaded from --trims JSON when given.

TRIM = {}

# Drum keys: full GM set plus everything the corpus plays

GM_DRUM_KEYS = set(range(35, 82))

CYMBAL_KEYS = {49, 51, 52, 53, 55, 57, 59}
TOM_KEYS = {41, 43, 45, 47, 48, 50}
OPEN_HAT = {46}


def drum_plan(key, freq):
    if freq >= 0.25:
        rate = 20000
    elif freq >= 0.08:
        rate = 16000
    else:
        rate = 13000
    if key in CYMBAL_KEYS:
        max_s = 1.0
    elif key in OPEN_HAT:
        max_s = 0.8
    elif key in TOM_KEYS:
        max_s = 0.5
    else:
        max_s = 0.35
    return rate, max_s


# ---------------------------------------------------------------------------
# Zone selection
# ---------------------------------------------------------------------------

def pick_zones(sf, pool, lo, hi, step):
    """pool: [(zlo, zhi, zone, pgens)].  Choose zones ~step semitones
    apart covering [lo,hi], loudest velocity layer per key range, and
    return [[klo, khi, zone, pgens, tune_override]] with contiguous
    coverage.

    tune_override: fonts often stack chorus layers detuned +/-N cents
    over the same key range (SGM's charang: -69c/+69c pair).  Keeping
    one layer verbatim leaves the whole program N cents off, so the
    picked zone gets the MEAN tune of its layer group -- unless the
    spread is so large (octave stacks) that averaging is nonsense."""
    byrange = {}
    tunes = {}
    layers = {}
    velhi = {}
    for zlo, zhi, z, pg in pool:
        if zhi < lo or zlo > hi:
            continue

        # Effective velocity range: SGM splits layers at the PRESET
        # level (velRange in the preset zone), so intersect both.

        vr_z = z.get(G_VELRANGE, 0x7f00)
        vr_p = pg.get(G_VELRANGE, 0x7f00)
        vlo = max(vr_z & 0xff, vr_p & 0xff)
        vhi = min(vr_z >> 8, vr_p >> 8)
        if vhi < vlo:
            continue
        s = sf.shdr[z[G_SAMPLEID]]
        t = s['correction'] + s16(z.get(G_COARSE, 0)) * 100 + \
            s16(z.get(G_FINE, 0))
        tunes.setdefault((zlo, zhi), []).append(t)
        layers.setdefault((zlo, zhi), []).append((vlo, vhi, z))
        if vhi > velhi.get((zlo, zhi), -1):
            velhi[(zlo, zhi)] = vhi
            byrange[(zlo, zhi)] = (z, pg)
    ranges = sorted(byrange.keys())
    picked = []
    for kr in ranges:
        if not picked:
            picked.append((kr, byrange[kr]))
        elif kr[0] - picked[-1][0][0] >= step:
            picked.append((kr, byrange[kr]))
    if not picked:
        return []
    out = []
    for i, (kr, zpg) in enumerate(picked):
        klo = lo if i == 0 else out[-1][1] + 1
        khi = hi if i == len(picked) - 1 else \
            (kr[1] + picked[i + 1][0][0]) // 2
        if khi < klo:
            khi = klo
        ts = tunes[kr]
        tune_ov = None
        if len(ts) > 1 and max(ts) - min(ts) <= 200:
            tune_ov = sum(ts) / len(ts)
        out.append([klo, khi, zpg[0], zpg[1], tune_ov,
                    layer_exponent(sf, layers[kr])])
    return out


def layer_exponent(sf, layers):
    """Velocity->amplitude exponent for a keyrange whose velocity
    layers get collapsed into the loudest one.

    fluidsynth's within-layer velocity curve measured as (v/127)^2 to
    within 0.4dB, so the missing dynamics are the LAYER loudness steps:
    at the soft layer's velocity midpoint the reference plays the soft
    sample.  Choose e with a_loud*(v/127)^e == a_soft*(v/127)^2 there.
    Single-layer ranges keep e=2."""
    if len(layers) < 2:
        return 2.0

    def amp(z):
        pcm = sf.pcm(z[G_SAMPLEID])[:30000].astype(np.float64)
        rms = float(np.sqrt(np.mean(pcm ** 2)) + 1e-9)
        att = max(-240, min(s16(z.get(G_ATTEN, 0)), 1440))
        return rms * 10.0 ** (-(att * 0.4) / 200.0)

    soft = min(layers, key=lambda l: l[1])
    loud = max(layers, key=lambda l: l[1])
    if soft[1] == loud[1]:
        return 2.0
    vmid = max(1, (soft[0] + soft[1]) // 2)
    a_soft = amp(soft[2])
    a_loud = amp(loud[2])
    if a_soft <= 0 or a_loud <= 0:
        return 2.0
    denom = math.log(vmid / 127.0)
    if denom > -1e-3:
        return 2.0
    e = 2.0 + math.log(a_soft / a_loud) / denom
    return max(1.5, min(4.5, e))


def autoloop(pcm, maxn, loopn, faden):
    """Truncate to maxn samples with a crossfaded loop of loopn samples
    in the tail.  The caller snaps loopn to an integer number of
    waveform periods: an arbitrary splice length leaves a phase jump at
    every wrap that PULLS THE AVERAGE PITCH (measured -60..-100 cents
    on low notes with 14-period loops)."""
    if len(pcm) <= maxn:
        return pcm, None, None
    body = pcm[:maxn].copy()
    ls = maxn - loopn
    n = min(faden, len(pcm) - maxn)
    if n > 0:
        f = np.arange(n, dtype=np.float32) / faden
        body[ls:ls + n] = pcm[maxn:maxn + n] * (1 - f) + body[ls:ls + n] * f
    return body, ls, maxn - 1


def zone_to_region(sf, klo, khi, z, pgens, rate, max_s, rel_clamp,
                   trim, pan_from_font=False, excl_from_font=False,
                   tune_override=None, retune=True, vel_exp=2.0,
                   attack_cap=ATTACK_CAP_FAST):
    """Extract one zone into a bank region dict (with 'adpcm' bytes)."""
    s = sf.shdr[z[G_SAMPLEID]]
    pcm = sf.pcm(z[G_SAMPLEID])
    srate = s['rate']

    root = z.get(G_ROOTKEY, s['origpitch'])
    if root > 127:
        root = 60

    own_tune = s['correction'] + s16(z.get(G_COARSE, 0)) * 100 + \
        s16(z.get(G_FINE, 0))
    if tune_override is not None:
        own_tune = tune_override
    tune = own_tune + s16(pgens.get(G_COARSE, 0)) * 100 + \
        s16(pgens.get(G_FINE, 0))
    scale = z.get(G_SCALETUNE, 100)

    att_cb = s16(z.get(G_ATTEN, 0)) + s16(pgens.get(G_ATTEN, 0))
    att_cb = max(-240, min(att_cb, 1440))
    gain = trim * 10.0 ** (-(att_cb * 0.4) / 200.0)

    pan = 64
    if pan_from_font:
        pan = max(0, min(127, 64 + s16(z.get(G_PAN, 0)) * 64 // 500))
    excl = z.get(G_EXCLCLASS, 0) if excl_from_font else 0

    looped = (z.get(G_MODES, 0) & 1) == 1
    ls = s['loopstart'] - s['start']
    le = s['loopend'] - s['start']   # SF2: one past the last loop sample

    ratio = min(rate / srate, 1.0)
    outrate = int(round(srate * ratio))

    # Content pitch at native rate (SF2 tune is a playback correction,
    # so the recorded material sits at -tune from ET)

    f_claim = 440.0 * 2.0 ** ((root - 69) / 12.0 - tune / 1200.0)

    if looped and le - ls > 3 and le <= len(pcm):
        if le / srate > max_s:
            # Long sustain: truncate with our own crossfaded loop,
            # snapped to an integer number of measured periods.

            maxn = int(max_s * srate)
            loopn = int(max_s * 0.4 * srate)
            seg = pcm[max(0, le - int(0.4 * srate)):le]
            dev = measure_pitch_cents(seg, srate, f_claim)
            if dev is not None:
                period = srate / (f_claim * 2.0 ** (dev / 1200.0))
                if period < loopn / 2:
                    loopn = int(round(round(loopn / period) * period))
            pcm2, nls, nle = autoloop(pcm[:le], maxn, loopn,
                                      int(max_s * 0.1 * srate))
            if nls is not None:
                pcm = resample(pcm2, ratio)
                ls = int(nls * ratio)
                le = min(int(nle * ratio), len(pcm) - 1)
            else:
                pcm = resample(pcm2, ratio)
                ls = 0
                le = len(pcm) - 1
        else:
            # Font loop kept: single-cycle wavetable loops (organs,
            # synth leads) define the pitch, so the loop LENGTH must
            # stay exact.  Round the loop length to an integer first
            # and resample with exactly that ratio; the stored rate
            # absorbs the difference.  (The old int(index*ratio) path
            # was off by up to 150 cents on 8-sample loops.)

            loop0 = le - ls
            loop1 = max(4, int(round(loop0 * ratio)))
            r2 = loop1 / loop0
            if r2 > 1.0:
                r2 = 1.0
                loop1 = loop0
            outrate = int(round(srate * r2))
            ls2 = int(round(ls * r2))
            pcm = resample(pcm[:le], r2)
            if len(pcm) < ls2 + loop1:
                pcm = np.append(pcm, np.zeros(ls2 + loop1 - len(pcm),
                                              dtype=np.float32))
            pcm = pcm[:ls2 + loop1]
            ls = ls2
            le = ls2 + loop1 - 1     # inclusive last loop sample
        if le - ls < 3:
            looped = False
    else:
        looped = False
        pcm = pcm[:int(max_s * srate)]
        fn = max(1, len(pcm) // 7)
        fade = 1.0 - np.arange(1, fn + 1, dtype=np.float32) / fn
        pcm = pcm.copy()
        pcm[-fn:] *= fade
        pcm = resample(pcm, ratio)
        ls = le = 0

    # Auto-retune: measure the STORED data's actual pitch against the
    # root+tune claim and fold the deviation into tune.  Catches both
    # pipeline residue and font mis-claims (SGM's guitar harmonics is
    # +83c off its own root, banjo +99c) so notes land on equal
    # temperament.  Unpitched material returns None and is skipped.

    if retune:
        if looped and le > ls:
            seg = pcm[ls:le + 1]
            reps = int(np.ceil(0.35 * outrate / len(seg)))
            seg = np.tile(seg, max(1, reps))
        else:
            seg = pcm[len(pcm) // 8:len(pcm) // 8 + int(0.5 * outrate)]
        # SF2 semantics: tune is a correction ADDED at playback, so the
        # sample content itself sits at ET(root) * 2^(-tune/1200).
        # Measure the content there; any deviation m means the engine
        # will land m cents off ET, so subtract it from tune.

        f_content = 440.0 * 2.0 ** ((root - 69) / 12.0 - tune / 1200.0)
        dev = measure_pitch_cents(seg, outrate, f_content)
        if dev is not None and 6.0 < abs(dev) <= 160.0:
            tune -= dev

    attack = min(tc2sec(z.get(G_ATTACK), 0.002), attack_cap)
    decay = min(tc2sec(z.get(G_DECAY), 0.001), DECAY_CAP) \
        if G_DECAY in z else 0.0
    sustain = 10.0 ** (-min(z.get(G_SUSTAIN, 0), 1000) / 200.0)
    release = min(tc2sec(z.get(G_RELEASE), 0.25), rel_clamp)

    nib, snap = adpcm_encode(pcm.astype(np.int32),
                             snapshot_at=ls if looped else None)

    return dict(
        lokey=klo, hikey=khi, root=root, excl=excl,
        tune=int(round(max(-32768, min(32767, tune)))), scale=scale,
        pan=pan,
        rate=outrate, flags=fmt.FLAG_LOOPED if looped else 0,
        length=len(pcm), loopstart=ls if looped else 0,
        loopend=le if looped else 0,
        loop_pred=snap[0], loop_step=snap[1],
        vel_exp=int(round(vel_exp * 32)),
        gain=gain, attack=attack, decay=decay, sustain=sustain,
        release=release,
        adpcm=pack_nibbles(nib), _nib=nib, _pcm=pcm)


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

def parse_size(s):
    s = s.strip().upper()
    if s.endswith('M'):
        return int(float(s[:-1]) * 1048576)
    if s.endswith('K'):
        return int(float(s[:-1]) * 1024)
    return int(s)


def main():
    argv = sys.argv[1:]
    font_path = None
    fb_path = None
    stats_path = 'corpus_stats.json'
    out_path = 'gmbank.bin'
    rep_path = 'gmbank_report.txt'
    budget = None
    i = 0
    while i < len(argv):
        if argv[i] == '--fallback':
            fb_path = argv[i + 1]
            i += 2
        elif argv[i] == '--budget':
            budget = parse_size(argv[i + 1])
            i += 2
        elif argv[i] == '--stats':
            stats_path = argv[i + 1]
            i += 2
        elif argv[i] == '--out':
            out_path = argv[i + 1]
            i += 2
        elif argv[i] == '--report':
            rep_path = argv[i + 1]
            i += 2
        elif argv[i] == '--trims':
            TRIM.update({int(k): v for k, v in
                         json.load(open(argv[i + 1])).items()})
            i += 2
        else:
            if argv[i] in ('-h', '--help'):
                sys.exit(__doc__)
            font_path = argv[i]
            i += 1

    if font_path is None:
        sys.exit(__doc__)

    if os.path.exists(stats_path):
        stats = json.load(open(stats_path))
    else:
        # No corpus scan: neutral flat profile (every program tier B,
        # generous key ranges, full GM drum set) -- same default the
        # calibrator uses, so --stats really is optional.
        print(f'gmbank_build: no {stats_path}, using neutral profile')
        stats = {
            'files': ['neutral'] * 100,
            'prog_files': {str(p): 6 for p in range(128)},
            'prog_key_lo': {str(p): 36 for p in range(128)},
            'prog_key_hi': {str(p): 84 for p in range(128)},
            'drum_files': {str(k): 10 for k in sorted(GM_DRUM_KEYS)},
        }
    nfiles = len(stats['files'])
    prog_files = {int(k): v for k, v in stats['prog_files'].items()}
    key_lo = {int(k): v for k, v in stats['prog_key_lo'].items()}
    key_hi = {int(k): v for k, v in stats['prog_key_hi'].items()}
    drum_files = {int(k): v for k, v in stats['drum_files'].items()}

    sf = SF2(font_path)
    fb = None

    def get_pool(bank, prog):
        nonlocal fb
        pool = sf.zone_pool(bank, prog)
        src = 'main'
        if not pool and fb_path:
            if fb is None:
                fb = SF2(fb_path)
            pool = fb.zone_pool(bank, prog)
            src = 'fallback'
        return pool, (sf if src == 'main' else fb), src

    total = run_build(1.0, get_pool, stats, nfiles, prog_files, key_lo,
                      key_hi, drum_files, out_path, rep_path)
    if budget is not None and total > budget:
        solve_budget(total, budget, get_pool, stats, nfiles, prog_files,
                     key_lo, key_hi, drum_files, out_path, rep_path)


def solve_budget(size1, budget, *build_args):
    """Closed loop: scale the global quality knob down until the bank
    fits.  Size scales roughly as qscale^2.5 (rate x loop length x zone
    density), so start from the analytic guess and correct."""
    hi = 1.0                               # known too big
    lam = (budget / size1) ** (1 / 2.5)
    best = None
    last = (1.0, size1)
    for _ in range(6):
        lam = min(max(lam, 0.25), hi - 0.005)
        size = run_build(lam, *build_args)
        last = (lam, size)
        print('budget: qscale %.3f -> %.2f MB (target %.2f MB)' %
              (lam, size / 1048576.0, budget / 1048576.0))
        if size <= budget:
            if best is None or lam > best[0]:
                best = (lam, size)
            if size >= 0.90 * budget:
                break
            lam = min(lam * (budget / size) ** (1 / 2.5),
                      (lam + hi) / 2)
        else:
            hi = lam
            lam = lam * (budget / size) ** (1 / 2.5)
    if best is None:
        sys.exit('gmbank_build: cannot reach budget %d' % budget)
    if last != best:
        run_build(best[0], *build_args)
    print('budget: final qscale %.3f, %.2f MB' %
          (best[0], best[1] / 1048576.0))


def run_build(qscale, get_pool, stats, nfiles, prog_files, key_lo,
              key_hi, drum_files, out_path, rep_path):
    prog_regions = {}
    report = []
    snrs = []
    if qscale != 1.0:
        report.append('quality scale %.3f (size budget)' % qscale)

    # Melodic programs

    for prog in range(128):
        freq = prog_files.get(prog, 0) / nfiles
        tier = tier_of(freq)
        if prog in TIER_PROMOTE:
            tier = TIER_UP[tier]

        step, max_s, rscale = TIERS[tier]
        step = max(2, int(round(step / qscale)))
        fam = (prog // 8) * 8
        rate = int(min(max(FAMILY_RATE[fam] * rscale * qscale, RATE_MIN),
                       RATE_MAX))
        rel_clamp = PROG_RELEASE.get(prog, FAMILY_RELEASE[fam])
        max_s = PROG_MAXS.get(prog, max_s) * qscale
        trim = TRIM.get(prog, 1.0)
        att_cap = ATTACK_CAP_SLOW if fam in SLOW_ATTACK_FAMS \
            else ATTACK_CAP_FAST
        if prog in (45, 46, 47):
            att_cap = ATTACK_CAP_FAST      # percussive members of fam40

        lo = max(min(key_lo.get(prog, 36), 127) - 4, 0)
        hi = min(max(key_hi.get(prog, 96), lo, 0) + 4, 127)

        pool, font, src = get_pool(0, prog)
        if not pool:
            report.append('prog %3d: NO PRESET ANYWHERE' % prog)
            continue

        zones = pick_zones(font, pool, lo, hi, step)
        zones[0][0] = 0
        zones[-1][1] = 127

        regs = []
        for klo, khi, z, pg, tune_ov, vel_e in zones:
            r = zone_to_region(font, klo, khi, z, pg, rate, max_s,
                               rel_clamp, trim, tune_override=tune_ov,
                               vel_exp=vel_e, attack_cap=att_cap)
            regs.append(r)
        prog_regions[prog] = regs

        sz = sum(len(r['adpcm']) for r in regs)
        deco = adpcm_decode(regs[0]['_nib'][:4000])
        orig = regs[0]['_pcm'][:4000]
        n = min(len(deco), len(orig))
        sig = float(np.sum(orig[:n] ** 2)) + 1e-9
        err = float(np.sum((orig[:n] - deco[:n]) ** 2)) + 1e-9
        snr = 10 * math.log10(sig / err)
        snrs.append((prog, snr))
        report.append('prog %3d tier %s %-6s: %d zones %6.1f KB '
                      '@%5dHz keys %d-%d snr %.1fdB' %
                      (prog, tier, src, len(regs), sz / 1024.0, rate,
                       lo, hi, snr))

    # Drum kit

    dkeys = sorted(GM_DRUM_KEYS |
                   {k for k in drum_files if 24 <= k <= 87})
    dpool, dfont, dsrc = get_pool(128, 0)
    dregs = []
    missing = []
    for key in dkeys:
        freq = drum_files.get(key, 0) / nfiles
        rate, max_s = drum_plan(key, freq)
        rate = int(max(rate * qscale, 8000))
        max_s *= qscale
        hit = None
        for zlo, zhi, z, pg in dpool:
            if zlo <= key <= zhi:
                hit = (z, pg)
                break
        if hit is None:
            missing.append(key)
            continue
        r = zone_to_region(dfont, key, key, hit[0], hit[1], rate, max_s,
                           0.30, TRIM.get(1000 + key, 1.0),
                           pan_from_font=True, excl_from_font=True,
                           retune=False)
        # Drums: one-shot, envelope pinned open (cut is excl/steal's job)
        r['flags'] = 0
        r['loopstart'] = r['loopend'] = 0
        r['attack'] = 0.001
        r['decay'] = 0.0
        r['sustain'] = 1.0
        dregs.append(r)
    prog_regions[fmt.DRUM_SLOT] = dregs
    dsz = sum(len(r['adpcm']) for r in dregs)
    report.append('drums (%s): %d keys %6.1f KB, missing %s' %
                  (dsrc, len(dregs), dsz / 1024.0, missing))

    for slot in prog_regions:
        for r in prog_regions[slot]:
            r.pop('_nib', None)
            r.pop('_pcm', None)

    total, nrows = fmt.write_bank(out_path, prog_regions)
    report.append('TOTAL: %d regions, %.2f MB (%s)' %
                  (nrows, total / 1048576.0, out_path))
    worst = sorted(snrs, key=lambda x: x[1])[:8]
    report.append('worst SNR: ' +
                  ' '.join('p%d=%.1fdB' % w for w in worst))

    with open(rep_path, 'w') as f:
        f.write('\n'.join(report) + '\n')
    print('\n'.join(report[-14:]))
    return total


if __name__ == '__main__':
    main()
