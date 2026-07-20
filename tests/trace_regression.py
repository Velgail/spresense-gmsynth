#!/usr/bin/env python3
# SPDX-License-Identifier: BSL-1.0
"""Layer-3 regression: run the REAL supervisor C code (event ledger,
sequencer, voice allocation) on fixed MIDI cases via the host trace
harness and compare the emitted per-block worker event traces against
committed expectations.

Everything runs against a bank built from the repo-owned deterministic
fixture font (tests/make_fixture_sf2.py) -- real gmbank.bin files are
never committed (they inherit their SoundFont's license), so they
cannot anchor shared expectations.  The fixture bank's structure is
also pinned (bank_manifest.py vs tests/expected/fixture_manifest.json),
which doubles as the builder's structural regression.

  python3 tests/trace_regression.py            # build, run, compare
  python3 tests/trace_regression.py --update   # bless current output

Needs: gcc, numpy.  No third-party fonts, MIDIs or hardware.
"""

import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, 'tests')
HARNESS = os.path.join(TESTS, 'host', 'gmtrace')
EXPECTED = os.path.join(TESTS, 'expected')
OUTDIR = os.path.join(TESTS, 'out')
MANIFEST = os.path.join(EXPECTED, 'fixture_manifest.json')

# case name -> extra harness args
CASES = {
    '01_cc_order': [],
    '02_bend_order': [],
    '03_sustain': [],
    '04_self_steal': [],
    '05_global_steal': [],
    '06_attack_guard': [],
    '07_drum_choke': [],
    '08_cc111_loop': ['--loop', '--max-blocks', '60'],
    '09_overflow': [],
    '10_tempo_change': [],
    '11_running_status': [],
    '12_multitrack': [],
    '13_rpn_bendrange': [],
    '14_overflow_release': [],
}


def sh(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        sys.exit(f'command failed: {" ".join(cmd)}\n{r.stdout}\n'
                 f'{r.stderr}')
    return r


def build_fixture_bank():
    """Deterministic fixture font -> bank; the harness reads it as
    tests/out/gmbank.bin (BANK_PATH with cwd=tests/out)."""
    os.makedirs(OUTDIR, exist_ok=True)
    sh([sys.executable, os.path.join(TESTS, 'make_fixture_sf2.py')])

    stats = os.path.join(OUTDIR, 'neutral_stats.json')
    sys.path.insert(0, ROOT)
    from bank_calibrate import neutral_stats
    json.dump(neutral_stats(), open(stats, 'w'))

    bank = os.path.join(OUTDIR, 'fixture_bank.bin')
    sh([sys.executable, os.path.join(ROOT, 'gmbank_build.py'),
        os.path.join(TESTS, 'fixture.sf2'), '--stats', stats,
        '--out', bank,
        '--report', os.path.join(OUTDIR, 'fixture_report.txt')],
       cwd=ROOT)
    shutil.copyfile(bank, os.path.join(OUTDIR, 'gmbank.bin'))
    return bank


def check_manifest(bank, update):
    cmd = [sys.executable, os.path.join(ROOT, 'bank_manifest.py'),
           bank, '--manifest', MANIFEST]
    if update:
        sh(cmd + ['--write'])
        print('BLESS fixture_manifest')
        return True
    r = subprocess.run(cmd, capture_output=True, text=True)
    tag = 'PASS ' if r.returncode == 0 else 'FAIL '
    print(f'{tag} fixture_manifest')
    if r.returncode != 0:
        print('  ' + r.stdout.strip().replace('\n', '\n  '))
    return r.returncode == 0


def build_harness():
    cmd = ['gcc', '-O1', '-g', '-Wall', '-Werror', '-D_GNU_SOURCE',
           '-DBANK_PATH="gmbank.bin"',
           '-I', ROOT, '-I', os.path.join(TESTS, 'host', 'stubs'),
           os.path.join(TESTS, 'host', 'trace_main.c'),
           os.path.join(ROOT, 'gm_seq.c'),
           os.path.join(ROOT, 'gm_bank.c'),
           '-lm', '-lrt', '-o', HARNESS]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f'harness build failed:\n{r.stderr}')


def run_case(name, args):
    mid = os.path.join(TESTS, 'midi', f'{name}.mid')
    r = subprocess.run([HARNESS, mid] + args, capture_output=True,
                       text=True, cwd=OUTDIR)
    if r.returncode != 0:
        sys.exit(f'{name}: harness failed rc={r.returncode}\n{r.stderr}')
    try:
        trace = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f'{name}: bad JSON from harness: {e}\n'
                 f'{r.stdout[:2000]}')
    # normalize the midi path so expectations are machine-independent
    trace['midi'] = f'{name}.mid'
    return trace


def check_reset_fence():
    """Failure injection: host mpmq stubs never ack, so the harness's
    song_load() must abort with the sample pool untouched.  A direct
    invariant, not a golden compare."""
    mid = os.path.join(TESTS, 'midi', '01_cc_order.mid')
    r = subprocess.run([HARNESS, mid, '--reset-fence'],
                       capture_output=True, text=True, cwd=OUTDIR)
    ok = r.returncode == 0
    print(f'{"PASS " if ok else "FAIL "} reset_fence: '
          f'{r.stdout.strip()}')
    return ok


def diff_traces(name, exp, cur):
    """Human-oriented first-difference report."""
    msgs = []
    if exp.get('summary') != cur.get('summary'):
        msgs.append(f'  summary: expected {exp.get("summary")}\n'
                    f'           got      {cur.get("summary")}')
    eb = {b['n']: b['events'] for b in exp.get('blocks', [])}
    cb = {b['n']: b['events'] for b in cur.get('blocks', [])}
    for n in sorted(set(eb) | set(cb)):
        if n not in eb:
            msgs.append(f'  block {n}: unexpected ({len(cb[n])} events)')
        elif n not in cb:
            msgs.append(f'  block {n}: missing ({len(eb[n])} events)')
        elif eb[n] != cb[n]:
            for i, (e, c) in enumerate(zip(eb[n], cb[n])):
                if e != c:
                    msgs.append(f'  block {n} event {i}:\n'
                                f'    expected {e}\n    got      {c}')
                    break
            else:
                msgs.append(f'  block {n}: length {len(eb[n])} -> '
                            f'{len(cb[n])}')
        if len(msgs) >= 6:
            msgs.append('  ...')
            break
    return msgs


def main():
    update = '--update' in sys.argv[1:]

    sh([sys.executable, os.path.join(TESTS, 'gen_midi.py')])
    bank = build_fixture_bank()
    build_harness()
    os.makedirs(EXPECTED, exist_ok=True)

    failed = []
    if not check_manifest(bank, update):
        failed.append('fixture_manifest')

    if not check_reset_fence():
        failed.append('reset_fence')

    for name, args in CASES.items():
        trace = run_case(name, args)
        outp = os.path.join(OUTDIR, f'{name}.events.json')
        json.dump(trace, open(outp, 'w'), indent=1)
        expp = os.path.join(EXPECTED, f'{name}.events.json')

        if update:
            json.dump(trace, open(expp, 'w'), indent=1)
            print(f'BLESS {name}: {trace["summary"]}')
            continue

        if not os.path.exists(expp):
            print(f'MISS  {name}: no expectation (run with --update)')
            failed.append(name)
            continue

        exp = json.load(open(expp))
        if exp == trace:
            print(f'PASS  {name}: {trace["summary"]}')
        else:
            print(f'FAIL  {name}:')
            for m in diff_traces(name, exp, trace):
                print(m)
            failed.append(name)

    if update:
        print(f'\nblessed {len(CASES)} traces + manifest -> {EXPECTED}')
        return

    total = len(CASES) + 2          # + manifest + reset fence
    print(f'\n{total - len(failed)}/{total} passed')
    if failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
