# Validation status

This file records what has been verified, at which layer, and what has
not — so the guarantees claimed elsewhere (README, code comments) can
be checked against evidence. Entries marked **TBD** are known gaps:
they have either never been measured, or were observed during
development but not captured in a reproducible form.

## Verification layers

| Layer | What it covers | Tooling | Status |
|---|---|---|---|
| 1. Bank structure | Region counts, drum keys, key ranges, rates, size | `bank_manifest.py` vs `tests/expected/fixture_manifest.json` (fixture); `--write` for your own bank | automated, in CI |
| 2. Offline audio | Zone-level volume vs source font; pitch/audibility QA | `bank_calibrate.py --check`, `bank_qa.py` (both exit non-zero on failure) | automated locally (needs your font + fluidsynth); not in CI |
| 3. C event logic | SMF parsing, channel state, voice ledger, steal policy, block event streams | `tests/trace_regression.py` (real C sources on host) | automated, in CI |
| 4. Hardware-in-the-loop | ASMP transport, 16.16 fixed-point playback, saturation, audio pipeline, underruns, RESET fence | device PCM capture vs reference render | **TBD — not implemented** |

## What the offline model does and does not guarantee

`gmrender.py` is a *behavioral* reference: same pitch math, envelope
formulas, voice topology and steal policy as the device (Layer 3
verifies the C side against fixed cases independently). It is **not
bit-accurate**:

- device sample positions are 16.16 fixed-point; the model uses floats
- the device echo LPF is a sequential one-pole IIR; the model uses a
  truncated-FIR approximation
- the supervisor's block/event generation (C) is not executed by the
  Python model — that path is covered by Layer 3 instead
- worker-side saturation and message ordering are out of model scope

Any claim of sample-exact equivalence requires Layer 4.

## Device environment (record what you flash)

| Item | Value |
|---|---|
| Board | Sony Spresense CXD5602 + extension board (audio out, SD) |
| Spresense SDK version / commit | **TBD** |
| Toolchain | **TBD** |
| App config | `CONFIG_SYNTHAPPS_GMSYNTH`, app dir `synthapps/gmsynth` |
| Worker image | `worker/gmvoice/gmvoice` at `/mnt/spif/gmvoice` |
| Bank | `/mnt/sd0/gmbank.bin` or `/mnt/spif/gmbank.bin` (source font + build command: **TBD per bank**) |

## Developer-recorded device observations

These facts are recorded in code comments and commit history from
bring-up (Phase A spike dated 2026-07-19 and later); they were
observed on hardware but there is no committed capture/log artifact
backing them, so treat them as engineering notes, not test results:

- 4 workers x 16 voices at 48 kHz, 512-frame blocks (~10.7 ms), 4-block
  (~42.7 ms) pipeline runs on the board (`gm2_shared.h` topology note).
- `GM2_MAX_BLK_EVENTS`: 48 events/block was measured overflowing on
  tutti sections (audible dropped notes); raised to 96
  (`gm2_shared.h`).
- Audio device must be opened before any subcore boots, or the power
  domain blocks forever (`gmsynth_main.c`, "Phase4 pitfall #1").
- Large worker images exhaust address-converter tags and break the big
  mpshm pool attach; workers are kept code-only ~10 KB (`gm2_shared.h`).
- Without the RESET fence, a track change makes old voices read the
  new song's samples — audible noise burst (`workers_reset()` comment).
- Supervisor prints a periodic load line
  (`worker %: ... steal ... drop ... udr ...`); representative captured
  values: **TBD**.

## Measurements to capture (Layer 4 backlog)

- [ ] Worker CPU utilization per block on a dense reference MIDI
- [ ] Underrun count over a fixed playlist run
- [ ] Event drops (`g_ev_dropped`) on the densest corpus files
- [ ] Steal counts vs the offline model's on identical input
- [ ] Device PCM (I2S or line capture) vs `gmrender.py` on fixed MIDI:
      onset error, per-note RMS delta, pitch delta, silence windows,
      cross-correlation / spectral distance
- [x] RESET-failure injection at the supervisor-logic level: the host
      harness's mpmq stubs never ack, and `gmtrace --reset-fence`
      asserts `song_load()` aborts with the sample pool untouched
      (in CI via `tests/trace_regression.py`).  The device-side half
      (a worker that really hangs mid-RENDER) still needs Layer 4.

## Known limitations

- CC1 vibrato is not rendered (device and model, both).
- Loop-mode songs with CC111 never end naturally by design.
- The ledger estimates worker envelope state (workers do not report
  voice death); estimation drift shows up as slightly conservative
  voice retirement, not as hangs.
- SMF parsing caps: 48 tracks, 160 KB file, 1 hour (`gm_seq.h`,
  `gmsynth_main.c`).
