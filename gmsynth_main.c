/****************************************************************************
 * SPDX-License-Identifier: BSL-1.0
 * synthapps/gmsynth/gmsynth_main.c
 *
 * Phase C/D: the real GM synthesizer.
 *
 *   main core : SMF sequencer + global 64-voice ledger (self-steal /
 *               global steal / attack guard, ported from the validated
 *               reference renderer), per-song sample loading from
 *               gmbank.bin, 4-worker block pipeline, final mix +
 *               shared echo, 48kHz audio I/O, GPIO jumper UI
 *   4 subcores: gmvoice workers, 16 voices each (gmvoice.c)
 *
 * Controls (Arduino-compatible pins on the extension board, internal
 * pull-ups; short a pin to GND -- e.g. with a jumper from the GND pin
 * next to D13 -- to "press"; edge-triggered, no auto-repeat):
 *   D0 play   D1 stop   D2 next    D3 prev
 *   D4 vol+   D5 vol-   D6 loop    D7 through (ignore CC111)
 *
 * Install once:
 *   ./tools/flash.sh -c COM3 -w synthapps/gmsynth/worker/gmvoice/gmvoice
 *   ./tools/flash.sh -c COM3 -w gmbank.bin          (from gmsynth/)
 *   ./tools/flash.sh -c COM3 -w <songs>.mid
 ****************************************************************************/

#include <nuttx/config.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <dirent.h>
#include <mqueue.h>
#include <math.h>
#include <sys/ioctl.h>
#include <nuttx/audio/audio.h>

#include <asmp/asmp.h>
#include <asmp/mptask.h>
#include <asmp/mpshm.h>
#include <asmp/mpmq.h>

#include <arch/board/board.h>
#include <arch/chip/pin.h>

#include "gm2_shared.h"
#include "gm_bank.h"
#include "gm_seq.h"

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define AUDIO_DEVFILE   "/dev/audio/pcm0"
#define MSGQ_PATH       "/tmp/gmsynth_mq"
#define WORKER_PATH     "/mnt/spif/gmvoice"
#define MIDI_DIR_SD     "/mnt/sd0"
#define MIDI_DIR_SPIF   "/mnt/spif"

#define NUM_APB         (8)
#define SZ_APB          (4096)
#define BYTES_PER_FRAME (4)
#define APB_FRAMES      (SZ_APB / BYTES_PER_FRAME)

#define MIDI_MAX        (160 * 1024)

#define VOL_DEFAULT     (200)     /* Driver log curve 0-1000; 200 =~ -24dB */
#define VOL_STEP        (50)
#define VOL_MAX         (500)

/* Mix math: workers write voice sums scaled by 0.25 into int16; the
 * reference engine's master gain is 0.45 on the raw voice sum.
 */

#define MIX_SCALE       (0.45f / 0.25f)
#define SEND_SCALE      (1.0f / (0.25f * 32767.0f))

#define ECHO_FRAMES     (GM2_RATE * 80 / 1000)
#define ECHO_FEEDBACK   (0.40f)
#define ECHO_MIX        (0.60f)
#define ECHO_LPF        (0.30f)

#define TOTAL_VOICES    (GM2_NWORKERS * GM2_NVOICES)
#define AGE_GUARD       (GM2_RATE * 60 / 1000)   /* Attack protection */

#define TAIL_FRAMES     (GM2_RATE * 2)

#define MAX_SONGS       (5500)  /* Names live in GNSS RAM (352KB) */

#define DEFAULT_REVERB  (40)

/* UI actions */

#define UI_NONE         (0)
#define UI_PLAY         (1)
#define UI_STOP         (2)
#define UI_NEXT         (3)
#define UI_PREV         (4)
#define UI_RETRY        (5)     /* Underrun: restart the same song */

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct chstate_s
{
  uint8_t prog;
  uint8_t bank_msb;
  uint8_t vol;
  uint8_t exp;
  uint8_t pan;
  uint8_t rev;
  uint8_t rpn_hi;
  uint8_t rpn_lo;
  bool sus;
  int16_t bend;
  float bendrange;
};

struct lvoice_s
{
  bool used;
  bool drum;
  bool sus_held;
  uint8_t worker;
  uint8_t vslot;
  uint8_t ch;
  uint8_t note;
  uint8_t phase;          /* 0 = on, 1 = releasing */
  uint32_t born;
  uint32_t rel_frame;
  float env_at_rel;
  const struct lregion_s *reg;
};

/****************************************************************************
 * Private Data
 ****************************************************************************/

/* Audio */

static struct ap_buffer_s g_apbs[NUM_APB];
static uint8_t g_buff[NUM_APB][SZ_APB];
static int g_volume = VOL_DEFAULT;
static int g_spkfd = -1;

/* MP */

static mptask_t g_task[GM2_NWORKERS];
static mpmq_t g_mq[GM2_NWORKERS];
static mpshm_t g_shm;
static FAR struct gm2_pool_s *g_pool;
static FAR uint8_t *g_pool_samples;
static FAR struct gm2_blkarea_s *g_blkarea[GM2_NWORKERS];

/* Bank / song */

/* Big buffers live in the otherwise-unused 640KB GNSS RAM (the linker
 * script's .gnssram.bss section).  NOT zero-initialized at boot --
 * every user below fully overwrites before reading.
 */

#define GNSSRAM __attribute__((section(".gnssram.bss")))

static struct bank_s g_bank;
static struct loadset_s g_ls;
static struct prescan_s g_ps;
static uint8_t g_midibuf[MIDI_MAX] GNSSRAM;
static uint32_t g_midisz;
static struct seq_s g_seq;
static struct seq_ev_s g_pend;
static bool g_pend_valid;
static bool g_seq_ended;
static uint32_t g_frame_shift;    /* out frame - song frame */
static uint32_t g_end_frame;      /* out frame where tail ends */
static bool g_first_block;        /* Emit initial channel state */

/* Playlist / modes */

/* Song list: NAMES only (paths get assembled against g_mididir when
 * opening), stored in GNSS RAM to allow thousands of entries.
 */

static char g_songs[MAX_SONGS][64] GNSSRAM;
static int g_nsongs;
static int g_cur_song;
static bool g_loop_mode = true;

/* Ledger */

static struct chstate_s g_chs[16];
static struct lvoice_s g_lv[TOTAL_VOICES];
static uint8_t g_wcount[GM2_NWORKERS];
static uint32_t g_fill_frame;     /* First frame of the block being built */
static uint32_t g_steals;
static uint32_t g_self_steals;
static uint32_t g_steals_active;
static uint32_t g_ev_dropped;
static uint32_t g_underruns;

/* Pipeline */

static uint8_t g_blk_ready[GM2_NBLKS];   /* Bitmask of workers done */
static int g_cons_slot;
static int g_rdpos;
static uint64_t g_worker_us[GM2_NWORKERS];
static uint32_t g_load_blocks;

/* Echo bus (main core; buffers in GNSS RAM) */

static float g_echo_l[ECHO_FRAMES] GNSSRAM;
static float g_echo_r[ECHO_FRAMES] GNSSRAM;
static int g_echo_pos;
static float g_lpf_l;
static float g_lpf_r;

/* UI */

static const struct
{
  uint32_t pin;
  const char *name;
} g_btns[8] =
{
  { PIN_UART2_RXD,  "D0 play" },
  { PIN_UART2_TXD,  "D1 stop" },
  { PIN_HIF_IRQ_OUT, "D2 next" },
  { PIN_PWM3,       "D3 prev" },
  { PIN_SPI2_MOSI,  "D4 vol+" },
  { PIN_PWM1,       "D5 vol-" },
  { PIN_PWM0,       "D6 loop" },
  { PIN_SPI3_CS1_X, "D7 through" },
};

static uint8_t g_btn_prev;        /* Bit per button, 1 = up */
static uint8_t g_btn_lock[8];     /* Poll ticks until re-arm */

/****************************************************************************
 * Ledger: block event emission
 ****************************************************************************/

static FAR struct gm2_blk_s *g_build[GM2_NWORKERS];

static FAR struct gm2_ev_s *blk_ev_append(int w, uint16_t offset,
                                          uint8_t type, uint8_t vslot)
{
  FAR struct gm2_blk_s *blk = g_build[w];
  FAR struct gm2_ev_s *ev;
  uint32_t i;

  /* Coalesce channel-state events: keep only the latest per channel */

  if (type == GM2_EV_CHGAIN || type == GM2_EV_CHBEND)
    {
      for (i = 0; i < blk->nevents; i++)
        {
          if (blk->events[i].type == type &&
              blk->events[i].vslot == vslot)
            {
              blk->events[i].offset = offset;
              return &blk->events[i];
            }
        }
    }

  if (blk->nevents >= GM2_MAX_BLK_EVENTS)
    {
      g_ev_dropped++;
      return NULL;
    }

  ev = &blk->events[blk->nevents++];
  ev->offset = offset;
  ev->type = type;
  ev->vslot = vslot;
  return ev;
}

static void emit_all(uint16_t offset, uint8_t type, uint8_t vslot,
                     float f)
{
  int w;

  for (w = 0; w < GM2_NWORKERS; w++)
    {
      FAR struct gm2_ev_s *ev = blk_ev_append(w, offset, type, vslot);

      if (ev)
        {
          ev->u.f = f;
        }
    }
}

static void emit_chgain(uint16_t offset, int ch)
{
  float v = ((float)g_chs[ch].vol / 127.0f) *
            ((float)g_chs[ch].exp / 127.0f);

  emit_all(offset, GM2_EV_CHGAIN, ch, v * v);
}

static void emit_chbend(uint16_t offset, int ch)
{
  float semis = (float)g_chs[ch].bend / 8192.0f * g_chs[ch].bendrange;

  emit_all(offset, GM2_EV_CHBEND, ch, exp2f(semis / 12.0f));
}

/****************************************************************************
 * Ledger: voice bookkeeping
 ****************************************************************************/

static float env_estimate(const struct lvoice_s *v, uint32_t now)
{
  const struct lregion_s *r = v->reg;
  float att_frames = 1.0f / r->att_inc;
  float t = (float)(now - v->born);

  if (v->phase == 1)
    {
      return v->env_at_rel *
             expf(r->rel_lambda * (float)(now - v->rel_frame));
    }

  if (t < att_frames)
    {
      return t * r->att_inc;
    }

  if (r->dec_k > 0.0f)
    {
      return r->sus + (1.0f - r->sus) *
             expf(r->dec_lambda * (t - att_frames));
    }

  return 1.0f;
}

static void lv_release(struct lvoice_s *v, uint16_t offset, bool kill)
{
  if (!v->used || v->phase == 1)
    {
      if (v->used && kill)
        {
          blk_ev_append(v->worker, offset, GM2_EV_KILL, v->vslot);
        }

      return;
    }

  v->env_at_rel = env_estimate(v, g_fill_frame + offset);
  v->phase = 1;
  v->rel_frame = g_fill_frame + offset;
  blk_ev_append(v->worker, offset, kill ? GM2_EV_KILL : GM2_EV_RELEASE,
                v->vslot);
}

/* lv_alloc() : The validated steal policy: same-note self-steal ->
 * free slot on least-loaded worker -> global steal (releasing lowest
 * env; else mature lowest env; else lowest env).
 */

static struct lvoice_s *lv_alloc(int ch, int note, uint32_t now)
{
  struct lvoice_s *best = NULL;
  float bestenv = 1e9f;
  int i;
  int w;

  for (i = 0; i < TOTAL_VOICES; i++)
    {
      if (g_lv[i].used && g_lv[i].ch == ch && g_lv[i].note == note &&
          g_lv[i].phase == 1)
        {
          g_self_steals++;
          return &g_lv[i];
        }
    }

  w = 0;
  for (i = 1; i < GM2_NWORKERS; i++)
    {
      if (g_wcount[i] < g_wcount[w])
        {
          w = i;
        }
    }

  if (g_wcount[w] < GM2_NVOICES)
    {
      for (i = 0; i < TOTAL_VOICES; i++)
        {
          if (!g_lv[i].used)
            {
              uint8_t slot_used[GM2_NVOICES];
              int j;

              /* Find a free vslot on worker w */

              memset(slot_used, 0, sizeof(slot_used));
              for (j = 0; j < TOTAL_VOICES; j++)
                {
                  if (g_lv[j].used && g_lv[j].worker == w)
                    {
                      slot_used[g_lv[j].vslot] = 1;
                    }
                }

              for (j = 0; j < GM2_NVOICES; j++)
                {
                  if (!slot_used[j])
                    {
                      g_lv[i].worker = w;
                      g_lv[i].vslot = j;
                      g_wcount[w]++;
                      return &g_lv[i];
                    }
                }

              break;    /* Inconsistent count; fall through to steal */
            }
        }
    }

  g_steals++;

  for (i = 0; i < TOTAL_VOICES; i++)
    {
      if (g_lv[i].used && g_lv[i].phase == 1)
        {
          float e = env_estimate(&g_lv[i], now);

          if (e < bestenv)
            {
              bestenv = e;
              best = &g_lv[i];
            }
        }
    }

  if (best)
    {
      return best;
    }

  g_steals_active++;

  for (i = 0; i < TOTAL_VOICES; i++)
    {
      if (g_lv[i].used && now - g_lv[i].born > AGE_GUARD)
        {
          float e = env_estimate(&g_lv[i], now);

          if (e < bestenv)
            {
              bestenv = e;
              best = &g_lv[i];
            }
        }
    }

  if (best)
    {
      return best;
    }

  for (i = 0; i < TOTAL_VOICES; i++)
    {
      if (g_lv[i].used)
        {
          float e = env_estimate(&g_lv[i], now);

          if (e < bestenv)
            {
              bestenv = e;
              best = &g_lv[i];
            }
        }
    }

  return best ? best : &g_lv[0];
}

/****************************************************************************
 * Ledger: MIDI event handling
 ****************************************************************************/

static void note_on(uint16_t offset, int ch, int note, int vel)
{
  struct chstate_s *cs = &g_chs[ch];
  struct lvoice_s *v;
  struct lregion_s *lr;
  FAR struct gm2_ev_s *ev;
  bool drum;
  int slot;
  uint32_t now = g_fill_frame + offset;
  float semis;
  float p;
  float ang;

  drum = (ch == 9) || (g_ps.xg && (cs->bank_msb == 120 ||
                                   cs->bank_msb == 126 ||
                                   cs->bank_msb == 127));
  if (drum)
    {
      if (cs->prog == 48 && note >= 41 && note <= 53)
        {
          slot = 47;                  /* Orchestra kit timpani */
        }
      else
        {
          slot = BANK_DRUM_SLOT;
        }
    }
  else
    {
      slot = cs->prog;
    }

  lr = loadset_find(&g_ls, slot, note);
  if (lr == NULL)
    {
      return;
    }

  /* Drum exclusive class choke (open/closed hi-hat) */

  if (drum && lr->br.excl > 0)
    {
      int i;

      for (i = 0; i < TOTAL_VOICES; i++)
        {
          if (g_lv[i].used && g_lv[i].drum && g_lv[i].ch == ch &&
              g_lv[i].reg->br.excl == lr->br.excl)
            {
              lv_release(&g_lv[i], offset, true);
            }
        }
    }

  v = lv_alloc(ch, note, now);
  if (v->used)
    {
      /* Stolen: the START below replaces the voice on its worker */

      g_wcount[v->worker] = g_wcount[v->worker];  /* Count unchanged */
    }

  v->used = true;
  v->drum = drum;
  v->sus_held = false;
  v->ch = ch;
  v->note = note;
  v->phase = 0;
  v->born = now;
  v->reg = lr;

  ev = blk_ev_append(v->worker, offset, GM2_EV_START, v->vslot);
  if (ev == NULL)
    {
      /* Event overflow: voice never starts; free the ledger slot */

      v->used = false;
      g_wcount[v->worker]--;
      return;
    }

  semis = (float)(note - lr->br.root) * ((float)lr->br.scale / 100.0f);

  ev->u.start.adpcm_off = lr->pool_off;
  ev->u.start.len = lr->br.length;
  ev->u.start.loopstart = lr->br.loopstart;
  ev->u.start.loopend =
      (lr->br.flags & BANK_FLAG_LOOPED) ? lr->br.loopend : 0;
  ev->u.start.loop_pred = lr->br.loop_pred;
  ev->u.start.loop_step = lr->br.loop_step;
  ev->u.start.ch = ch;
  ev->u.start.inc =
      (uint32_t)((float)lr->inc_base * exp2f(semis / 12.0f));

  ev->u.start.amp = lr->br.gain *
      powf((float)vel / 127.0f, lr->vel_e);

  p = 64.0f + (float)(cs->pan - 64) + (float)(lr->br.pan - 64);
  if (p < 0.0f)
    {
      p = 0.0f;
    }
  else if (p > 127.0f)
    {
      p = 127.0f;
    }

  ang = p / 127.0f * (float)M_PI_2;
  ev->u.start.pan_l = cosf(ang);
  ev->u.start.pan_r = sinf(ang);
  ev->u.start.send = (float)cs->rev / 127.0f;

  ev->u.start.att_inc = lr->att_inc;
  ev->u.start.dec_k = lr->dec_k;
  ev->u.start.sus = lr->sus;
  ev->u.start.rel_k = lr->rel_k;
}

static void note_off(uint16_t offset, int ch, int note)
{
  int i;

  for (i = 0; i < TOTAL_VOICES; i++)
    {
      struct lvoice_s *v = &g_lv[i];

      if (v->used && !v->drum && v->ch == ch && v->note == note &&
          v->phase == 0)
        {
          if (g_chs[ch].sus)
            {
              v->sus_held = true;
            }
          else
            {
              lv_release(v, offset, false);
            }
        }
    }
}

static void release_channel(uint16_t offset, int ch)
{
  int i;

  for (i = 0; i < TOTAL_VOICES; i++)
    {
      if (g_lv[i].used && g_lv[i].ch == ch)
        {
          lv_release(&g_lv[i], offset, false);
        }
    }
}

static void handle_cc(uint16_t offset, int ch, int num, int val)
{
  struct chstate_s *cs = &g_chs[ch];

  switch (num)
    {
      case 0:
        cs->bank_msb = val;
        break;

      case 7:
        cs->vol = val;
        emit_chgain(offset, ch);
        break;

      case 11:
        cs->exp = val;
        emit_chgain(offset, ch);
        break;

      case 10:
        cs->pan = val;
        break;

      case 91:
        cs->rev = val;
        break;

      case 64:
        {
          bool was = cs->sus;

          cs->sus = (val >= 64);
          if (was && !cs->sus)
            {
              int i;

              for (i = 0; i < TOTAL_VOICES; i++)
                {
                  if (g_lv[i].used && g_lv[i].ch == ch &&
                      g_lv[i].sus_held)
                    {
                      g_lv[i].sus_held = false;
                      lv_release(&g_lv[i], offset, false);
                    }
                }
            }
        }
        break;

      case 101:
        cs->rpn_hi = val;
        break;

      case 100:
        cs->rpn_lo = val;
        break;

      case 98:
      case 99:
        cs->rpn_hi = cs->rpn_lo = 127;
        break;

      case 6:
        if (cs->rpn_hi == 0 && cs->rpn_lo == 0)
          {
            cs->bendrange = (float)val;
            emit_chbend(offset, ch);
          }
        break;

      case 120:
      case 123:
        release_channel(offset, ch);
        break;

      case 121:
        cs->bend = 0;
        cs->exp = 127;
        cs->sus = false;
        cs->rpn_hi = cs->rpn_lo = 127;
        emit_chgain(offset, ch);
        emit_chbend(offset, ch);
        break;

      default:
        break;
    }
}

static void ledger_event(const struct seq_ev_s *sev, uint16_t offset)
{
  switch (sev->kind)
    {
      case SEQ_EV_ON:
        note_on(offset, sev->ch, sev->a, sev->b);
        break;

      case SEQ_EV_OFF:
        note_off(offset, sev->ch, sev->a);
        break;

      case SEQ_EV_CC:
        handle_cc(offset, sev->ch, sev->a, sev->b);
        break;

      case SEQ_EV_PROG:
        g_chs[sev->ch].prog = sev->a;
        break;

      case SEQ_EV_BEND:
        g_chs[sev->ch].bend = sev->bend;
        emit_chbend(offset, sev->ch);
        break;
    }
}

/* ledger_gc() : Retire voices whose envelopes have decayed out, so the
 * ledger tracks the workers' idle transitions.  Estimation-based (the
 * workers do not report voice death); conservative by 20%.
 */

static void ledger_gc(uint32_t now)
{
  int i;

  for (i = 0; i < TOTAL_VOICES; i++)
    {
      struct lvoice_s *v = &g_lv[i];

      if (!v->used)
        {
          continue;
        }

      if (v->phase == 1 && env_estimate(v, now) < 0.0008f)
        {
          v->used = false;
          g_wcount[v->worker]--;
        }
      else if (v->phase == 0 && !v->drum && v->reg->dec_k > 0.0f &&
               v->reg->sus < 0.001f && env_estimate(v, now) < 0.0008f)
        {
          v->used = false;
          g_wcount[v->worker]--;
        }
      else if (v->phase == 0 && v->drum &&
               (v->reg->br.flags & BANK_FLAG_LOOPED) == 0)
        {
          /* One-shot drums die when the sample runs out */

          float spd = (float)v->reg->inc_base / 65536.0f;
          uint32_t dur = (uint32_t)((float)v->reg->br.length / spd);

          if (now - v->born > dur)
            {
              v->used = false;
              g_wcount[v->worker]--;
            }
        }
    }
}

static void ledger_reset_song(void)
{
  int ch;

  memset(g_lv, 0, sizeof(g_lv));
  memset(g_wcount, 0, sizeof(g_wcount));
  g_steals = g_self_steals = g_steals_active = 0;

  for (ch = 0; ch < 16; ch++)
    {
      g_chs[ch].prog = 0;
      g_chs[ch].bank_msb = 0;
      g_chs[ch].vol = 100;
      g_chs[ch].exp = 127;
      g_chs[ch].pan = 64;
      g_chs[ch].rev = DEFAULT_REVERB;
      g_chs[ch].rpn_hi = 127;
      g_chs[ch].rpn_lo = 127;
      g_chs[ch].sus = false;
      g_chs[ch].bend = 0;
      g_chs[ch].bendrange = 2.0f;
    }
}

/****************************************************************************
 * Sequencer -> block pipeline
 ****************************************************************************/

/* loop_rewind() : CC111 loop: rewind the parser and fast-forward the
 * channel STATE (no notes) to the loop point, then resume.
 */

static void loop_rewind(void)
{
  struct seq_ev_s ev;
  uint32_t target = g_ps.has_loop ? g_ps.loop_frame : 0;

  gmseq_open(&g_seq, g_midibuf, g_midisz);
  g_frame_shift = g_fill_frame - target;

  while (gmseq_next(&g_seq, &ev) == 0)
    {
      if (ev.frame >= target)
        {
          g_pend = ev;
          g_pend_valid = true;
          return;
        }

      if (ev.kind == SEQ_EV_CC || ev.kind == SEQ_EV_PROG ||
          ev.kind == SEQ_EV_BEND)
        {
          ledger_event(&ev, 0);
        }
    }

  g_seq_ended = true;
}

static void fill_block(int slot)
{
  uint32_t blk_end = g_fill_frame + GM2_BLK_FRAMES;
  int w;

  for (w = 0; w < GM2_NWORKERS; w++)
    {
      g_build[w] = &g_blkarea[w]->blks[slot];
      g_build[w]->nevents = 0;
    }

  if (g_first_block)
    {
      int ch;

      g_first_block = false;
      for (ch = 0; ch < 16; ch++)
        {
          emit_chgain(0, ch);
          emit_chbend(0, ch);
        }
    }

  while (!g_seq_ended)
    {
      uint32_t out_frame;
      uint16_t offset;

      if (!g_pend_valid)
        {
          if (gmseq_next(&g_seq, &g_pend) < 0)
            {
              /* Loop mode: CC111 marks the loop start; without one
               * the WHOLE song loops.
               */

              if (g_loop_mode)
                {
                  loop_rewind();
                  if (g_seq_ended)
                    {
                      break;
                    }

                  continue;
                }

              g_seq_ended = true;
              g_end_frame = g_fill_frame + TAIL_FRAMES;
              break;
            }

          g_pend_valid = true;
        }

      out_frame = g_pend.frame + g_frame_shift;
      if (out_frame >= blk_end)
        {
          break;
        }

      offset = (out_frame > g_fill_frame) ?
               (uint16_t)(out_frame - g_fill_frame) : 0;
      ledger_event(&g_pend, offset);
      g_pend_valid = false;
    }

  g_fill_frame = blk_end;
  ledger_gc(g_fill_frame);
}

static void dispatch_block(int slot)
{
  int w;

  g_blk_ready[slot] = 0;
  for (w = 0; w < GM2_NWORKERS; w++)
    {
      mpmq_send(&g_mq[w], GM2_MSG_RENDER, slot);
    }
}

static void drain_done(void)
{
  int w;

  for (w = 0; w < GM2_NWORKERS; w++)
    {
      uint32_t data;
      int ret;

      for (; ; )
        {
          ret = mpmq_tryreceive(&g_mq[w], &data);
          if (ret == GM2_MSG_DONE)
            {
              g_blk_ready[data & 0xff] |= (1 << w);
              g_worker_us[w] += data >> 8;
            }
          else if (ret == GM2_MSG_HELLO)
            {
              printf("gmsynth: w%d msg %08lx\n", w, (unsigned long)data);
            }
          else
            {
              break;
            }
        }
    }
}

static void wait_block(int slot)
{
  while (g_blk_ready[slot] != (1 << GM2_NWORKERS) - 1)
    {
      uint32_t data;
      int ret;
      int w;

      for (w = 0; w < GM2_NWORKERS; w++)
        {
          if (g_blk_ready[slot] & (1 << w))
            {
              continue;
            }

          ret = mpmq_timedreceive(&g_mq[w], &data, 2000);
          if (ret == GM2_MSG_DONE)
            {
              g_blk_ready[data & 0xff] |= (1 << w);
              g_worker_us[w] += data >> 8;
            }
          else if (ret < 0)
            {
              /* Rate-limited: endless stall spam corrupts xmodem
               * transfers sharing the console
               */

              static uint32_t stallcnt;

              if ((stallcnt++ & 15) == 0)
                {
                  printf("gmsynth: worker %d stalled (%d, x%lu)\n",
                         w, ret, (unsigned long)stallcnt);
                }
            }
          else
            {
              printf("gmsynth: w%d unexpected msg id=%d data=%08lx\n",
                     w, ret, (unsigned long)data);
            }
        }
    }
}

/****************************************************************************
 * Mixing + audio
 ****************************************************************************/

static void generate_frames(FAR struct ap_buffer_s *apb)
{
  FAR int16_t *out = (FAR int16_t *)apb->samp;
  int n;

  drain_done();

  for (n = 0; n < APB_FRAMES; n++)
    {
      FAR struct gm2_blk_s *b0;
      FAR struct gm2_blk_s *b1;
      FAR struct gm2_blk_s *b2;
      FAR struct gm2_blk_s *b3;
      int32_t dl;
      int32_t dr;
      float sl;
      float sr;
      float el;
      float er;
      float ol;
      float or_;
      int32_t o;

      if (g_rdpos == 0)
        {
          wait_block(g_cons_slot);
        }

      b0 = &g_blkarea[0]->blks[g_cons_slot];
      b1 = &g_blkarea[1]->blks[g_cons_slot];
      b2 = &g_blkarea[2]->blks[g_cons_slot];
      b3 = &g_blkarea[3]->blks[g_cons_slot];

      dl = b0->dry[g_rdpos * 2] + b1->dry[g_rdpos * 2] +
           b2->dry[g_rdpos * 2] + b3->dry[g_rdpos * 2];
      dr = b0->dry[g_rdpos * 2 + 1] + b1->dry[g_rdpos * 2 + 1] +
           b2->dry[g_rdpos * 2 + 1] + b3->dry[g_rdpos * 2 + 1];
      sl = (float)(b0->send[g_rdpos * 2] + b1->send[g_rdpos * 2] +
                   b2->send[g_rdpos * 2] + b3->send[g_rdpos * 2]) *
           SEND_SCALE;
      sr = (float)(b0->send[g_rdpos * 2 + 1] +
                   b1->send[g_rdpos * 2 + 1] +
                   b2->send[g_rdpos * 2 + 1] +
                   b3->send[g_rdpos * 2 + 1]) * SEND_SCALE;

      el = g_echo_l[g_echo_pos];
      er = g_echo_r[g_echo_pos];

      ol = (float)dl * MIX_SCALE + el * (ECHO_MIX * 0.45f * 32767.0f);
      or_ = (float)dr * MIX_SCALE + er * (ECHO_MIX * 0.45f * 32767.0f);

      g_lpf_l += ECHO_LPF * ((sl + el * ECHO_FEEDBACK) - g_lpf_l);
      g_lpf_r += ECHO_LPF * ((sr + er * ECHO_FEEDBACK) - g_lpf_r);
      g_echo_l[g_echo_pos] = g_lpf_l;
      g_echo_r[g_echo_pos] = g_lpf_r;
      g_echo_pos = (g_echo_pos + 1 < ECHO_FRAMES) ? g_echo_pos + 1 : 0;

      o = (int32_t)ol;
      *out++ = (o > 32767) ? 32767 : (o < -32768) ? -32768 : (int16_t)o;
      o = (int32_t)or_;
      *out++ = (o > 32767) ? 32767 : (o < -32768) ? -32768 : (int16_t)o;

      g_rdpos++;
      if (g_rdpos >= GM2_BLK_FRAMES)
        {
          g_rdpos = 0;
          fill_block(g_cons_slot);
          dispatch_block(g_cons_slot);
          g_cons_slot = (g_cons_slot + 1) % GM2_NBLKS;
          g_load_blocks++;
        }
    }

  apb->nbytes = APB_FRAMES * BYTES_PER_FRAME;

  if (g_load_blocks >= 468)     /* ~5 seconds */
    {
      printf("gmsynth: worker %%: %d %d %d %d, voices %d/%d/%d/%d, "
             "steal %lu self %lu act %lu drop %lu udr %lu\n",
             (int)(g_worker_us[0] / (g_load_blocks * 107)),
             (int)(g_worker_us[1] / (g_load_blocks * 107)),
             (int)(g_worker_us[2] / (g_load_blocks * 107)),
             (int)(g_worker_us[3] / (g_load_blocks * 107)),
             g_wcount[0], g_wcount[1], g_wcount[2], g_wcount[3],
             (unsigned long)g_steals, (unsigned long)g_self_steals,
             (unsigned long)g_steals_active,
             (unsigned long)g_ev_dropped, (unsigned long)g_underruns);
      g_worker_us[0] = g_worker_us[1] = g_worker_us[2] =
          g_worker_us[3] = 0;
      g_load_blocks = 0;
    }
}

/****************************************************************************
 * Audio driver plumbing (Phase4 recipe at 48kHz)
 ****************************************************************************/

static void init_apbs(void)
{
  int i;

  for (i = 0; i < NUM_APB; i++)
    {
      g_apbs[i].nmaxbytes = SZ_APB;
      g_apbs[i].nbytes    = 0;
      g_apbs[i].curbyte   = 0;
      g_apbs[i].flags     = 0;
      g_apbs[i].samp      = &g_buff[i][0];
      nxmutex_init(&g_apbs[i].lock);
    }
}

static mqd_t create_messageq(FAR const char *mqname)
{
  struct mq_attr attr;

  attr.mq_maxmsg  = 12;
  attr.mq_msgsize = sizeof(struct audio_msg_s);
  attr.mq_curmsgs = 0;
  attr.mq_flags   = 0;

  return mq_open(mqname, O_RDWR | O_CREAT, 0644, &attr);
}

static void cleanup_messageq(mqd_t mq)
{
  int qnum = 0;
  struct audio_msg_s msg;
  struct mq_attr attr;

  if (!mq_getattr(mq, &attr))
    {
      qnum = (int)attr.mq_curmsgs;
    }

  while (qnum--)
    {
      mq_receive(mq, (FAR char *)&msg, sizeof(msg), NULL);
    }
}

static int configure(int fd, int type, int chnum, int fs, int bps)
{
  struct audio_caps_desc_s cap;

  cap.caps.ac_len             = sizeof(struct audio_caps_s);
  cap.caps.ac_type            = type;
  cap.caps.ac_channels        = chnum;
  cap.caps.ac_chmap           = 0;
  cap.caps.ac_controls.hw[0]  = fs & 0xffff;
  cap.caps.ac_controls.b[2]   = bps;
  cap.caps.ac_controls.b[3]   = (fs >> 16) & 0xff;

  return ioctl(fd, AUDIOIOC_CONFIGURE, (unsigned long)(uintptr_t)&cap);
}

static int set_volume(int fd, int vol)
{
  struct audio_caps_desc_s cap;

  cap.caps.ac_len             = sizeof(struct audio_caps_s);
  cap.caps.ac_type            = AUDIO_TYPE_FEATURE;
  cap.caps.ac_format.hw       = AUDIO_FU_VOLUME;
  cap.caps.ac_controls.hw[0]  = vol;

  return ioctl(fd, AUDIOIOC_CONFIGURE, (unsigned long)(uintptr_t)&cap);
}

static int enqueue_buffer(int fd, FAR struct ap_buffer_s *apb)
{
  struct audio_buf_desc_s desc;

  desc.numbytes = apb->nbytes;
  desc.u.buffer = apb;

  return ioctl(fd, AUDIOIOC_ENQUEUEBUFFER,
               (unsigned long)(uintptr_t)&desc);
}

/****************************************************************************
 * UI: GPIO jumper buttons
 ****************************************************************************/

static void ui_init(void)
{
  int i;

  for (i = 0; i < 8; i++)
    {
      board_gpio_config(g_btns[i].pin, 0, true, false, PIN_PULLUP);
    }

  g_btn_prev = 0xff;
}

/* ui_poll() : Edge-triggered button scan; returns a UI_* action */

static int ui_poll(void)
{
  int action = UI_NONE;
  int i;

  for (i = 0; i < 8; i++)
    {
      int level = board_gpio_read(g_btns[i].pin);
      uint8_t bit = 1 << i;
      bool was_up = (g_btn_prev & bit) != 0;

      if (g_btn_lock[i] > 0)
        {
          g_btn_lock[i]--;
        }

      if (level == 0 && was_up && g_btn_lock[i] == 0)
        {
          g_btn_lock[i] = 8;      /* ~170ms lockout */
          printf("gmsynth: [%s]\n", g_btns[i].name);

          switch (i)
            {
              case 0:
                action = UI_PLAY;
                break;

              case 1:
                action = UI_STOP;
                break;

              case 2:
                action = UI_NEXT;
                break;

              case 3:
                action = UI_PREV;
                break;

              case 4:
                g_volume += VOL_STEP;
                if (g_volume > VOL_MAX)
                  {
                    g_volume = VOL_MAX;
                  }

                set_volume(g_spkfd, g_volume);
                printf("gmsynth: volume %d\n", g_volume);
                break;

              case 5:
                g_volume -= VOL_STEP;
                if (g_volume < 0)
                  {
                    g_volume = 0;
                  }

                set_volume(g_spkfd, g_volume);
                printf("gmsynth: volume %d\n", g_volume);
                break;

              case 6:
                g_loop_mode = true;
                printf("gmsynth: loop mode\n");
                break;

              case 7:
                g_loop_mode = false;
                printf("gmsynth: through mode\n");
                break;
            }
        }

      if (level == 0)
        {
          g_btn_prev &= ~bit;
        }
      else
        {
          g_btn_prev |= bit;
        }
    }

  return action;
}

/****************************************************************************
 * Workers and pool
 ****************************************************************************/

static int pool_setup(void)
{
  int ret;

  ret = mpshm_init(&g_shm, GM2_KEY_SHM, GM2_POOL_SIZE);
  if (ret < 0)
    {
      printf("gmsynth: mpshm_init failed %d\n", ret);
      return ret;
    }

  g_pool = (FAR struct gm2_pool_s *)mpshm_attach(&g_shm, 0);
  if (g_pool == NULL)
    {
      printf("gmsynth: mpshm_attach failed\n");
      return -1;
    }

  g_pool->sample_off = 64;
  g_pool_samples = (FAR uint8_t *)g_pool + g_pool->sample_off;
  g_pool->magic = GM2_MAGIC;

  printf("gmsynth: pool %p, %u KB samples\n", g_pool,
         (unsigned)(GM2_SAMPLE_BYTES / 1024));
  return 0;
}

static int workers_boot(void)
{
  int w;
  int ret;

  for (w = 0; w < GM2_NWORKERS; w++)
    {
      ret = mptask_init(&g_task[w], WORKER_PATH);
      if (ret != 0)
        {
          printf("gmsynth: mptask_init failed %d "
                 "(install: flash.sh -w gmvoice)\n", ret);
          return ret;
        }

      ret = mptask_assign(&g_task[w]);
      if (ret != 0)
        {
          printf("gmsynth: assign w%d failed %d\n", w, ret);
          return ret;
        }

      mpmq_init(&g_mq[w], GM2_KEY_MQ, mptask_getcpuid(&g_task[w]));
      mptask_bindobj(&g_task[w], &g_mq[w]);
      mptask_bindobj(&g_task[w], &g_shm);

      ret = mptask_exec(&g_task[w]);
      if (ret < 0)
        {
          printf("gmsynth: exec w%d failed %d\n", w, ret);
          return ret;
        }
    }

  for (w = 0; w < GM2_NWORKERS; w++)
    {
      uint32_t data;

      ret = mpmq_timedreceive(&g_mq[w], &data, 5000);
      if (ret != GM2_MSG_HELLO || (data >> 28) != 0xa)
        {
          printf("gmsynth: w%d bad HELLO ret=%d data=%08lx\n",
                 w, ret, (unsigned long)data);
          return -1;
        }

      /* HELLO carries the offset of the worker's in-image block area;
       * address it via the load address (main core sees all of RAM).
       */

      g_blkarea[w] = (FAR struct gm2_blkarea_s *)
          (g_task[w].loadaddr + (data & 0xfffff));
      memset(g_blkarea[w], 0, sizeof(struct gm2_blkarea_s));

      /* Verify the supervisor really reaches the worker's block area
       * through this address (write/readback probe)
       */

      g_blkarea[w]->blks[0].nevents = 0x13570000 + w;
      if (g_blkarea[w]->blks[0].nevents != 0x13570000 + (uint32_t)w)
        {
          printf("gmsynth: w%d blkarea readback FAILED at %p\n",
                 w, g_blkarea[w]);
          return -1;
        }

      g_blkarea[w]->blks[0].nevents = 0;

      mpmq_send(&g_mq[w], GM2_MSG_CFG, w);
      printf("gmsynth: worker %d on CPU%d ready, blks %p (load %08lx)\n",
             w, mptask_getcpuid(&g_task[w]), g_blkarea[w],
             (unsigned long)g_task[w].loadaddr);
    }

  return 0;
}

/****************************************************************************
 * Playlist
 ****************************************************************************/

static bool has_mid_ext(const char *name)
{
  size_t n = strlen(name);

  if (n < 4)
    {
      return false;
    }

  return strcasecmp(name + n - 4, ".mid") == 0;
}

static int song_cmp(const void *a, const void *b)
{
  return strcmp((const char *)a, (const char *)b);
}

static const char *g_mididir = MIDI_DIR_SPIF;

static void scan_playlist(void)
{
  DIR *d;
  struct dirent *e;
  int i;

  /* Prefer the SD card (drag-and-drop from a PC); it automounts with
   * ~1s debounce, so give it a moment to appear.
   */

  g_mididir = MIDI_DIR_SPIF;
  for (i = 0; i < 20; i++)
    {
      d = opendir(MIDI_DIR_SD);
      if (d != NULL)
        {
          closedir(d);
          g_mididir = MIDI_DIR_SD;
          break;
        }

      usleep(100 * 1000);
    }

  g_nsongs = 0;
  d = opendir(g_mididir);
  if (d == NULL && strcmp(g_mididir, MIDI_DIR_SD) == 0)
    {
      g_mididir = MIDI_DIR_SPIF;
      d = opendir(g_mididir);
    }

  if (d == NULL)
    {
      return;
    }

  while ((e = readdir(d)) != NULL)
    {
      if (has_mid_ext(e->d_name))
        {
          if (g_nsongs >= MAX_SONGS)
            {
              printf("gmsynth: MAX_SONGS (%d) reached, rest ignored\n",
                     MAX_SONGS);
              break;
            }

          snprintf(g_songs[g_nsongs], sizeof(g_songs[0]), "%.63s",
                   e->d_name);
          g_nsongs++;
        }
    }

  closedir(d);

  /* SD present but empty of songs: fall back to SPI flash */

  if (g_nsongs == 0 && strcmp(g_mididir, MIDI_DIR_SD) == 0)
    {
      g_mididir = MIDI_DIR_SPIF;
      scan_playlist();
      return;
    }

  qsort(g_songs, g_nsongs, sizeof(g_songs[0]), song_cmp);
  printf("gmsynth: %d songs found in %s\n", g_nsongs, g_mididir);

  /* Optional first.txt: substring selecting the start song */

  {
    char path[72];
    int fd;
    char pat[32];
    ssize_t n;

    snprintf(path, sizeof(path), "%s/first.txt", g_mididir);
    fd = open(path, O_RDONLY);
    if (fd < 0)
      {
        fd = open(MIDI_DIR_SPIF "/first.txt", O_RDONLY);
      }

    if (fd >= 0)
      {
        n = read(fd, pat, sizeof(pat) - 1);
        close(fd);
        while (n > 0 && (pat[n - 1] == '\n' || pat[n - 1] == '\r' ||
                         pat[n - 1] == ' '))
          {
            n--;
          }

        if (n > 0)
          {
            pat[n] = '\0';
            for (i = 0; i < g_nsongs; i++)
              {
                if (strstr(g_songs[i], pat) != NULL)
                  {
                    g_cur_song = i;
                    printf("gmsynth: start at %s\n", g_songs[i]);
                    break;
                  }
              }
          }
      }
  }
}

/****************************************************************************
 * Song lifecycle
 ****************************************************************************/

/* workers_reset() : Hard-stop every worker voice and zero their block
 * outputs before the sample pool is overwritten for a new song.  The
 * ack (HELLO 0xC0000000) fences all in-flight RENDERs -- without this
 * the previous song's voices read the NEW song's samples as garbage
 * (audible burst of noise at every track change).
 */

static void workers_reset(void)
{
  int w;

  for (w = 0; w < GM2_NWORKERS; w++)
    {
      mpmq_send(&g_mq[w], GM2_MSG_RESET, 0);
    }

  for (w = 0; w < GM2_NWORKERS; w++)
    {
      uint32_t data;
      int guard;

      for (guard = 0; guard < 64; guard++)
        {
          int ret = mpmq_timedreceive(&g_mq[w], &data, 2000);

          if (ret == GM2_MSG_HELLO && data == 0xc0000000)
            {
              break;
            }

          if (ret < 0)
            {
              printf("gmsynth: w%d reset timeout\n", w);
              break;
            }

          /* Stale DONEs / diag echoes: drain and keep waiting */
        }
    }

  memset(g_blk_ready, 0, sizeof(g_blk_ready));
}

static int song_load(int idx)
{
  int fd;
  ssize_t n;

  char path[96];

  snprintf(path, sizeof(path), "%s/%s", g_mididir, g_songs[idx]);
  printf("gmsynth: loading %s\n", path);
  workers_reset();
  fd = open(path, O_RDONLY);
  if (fd < 0)
    {
      printf("gmsynth: cannot open song\n");
      return -1;
    }

  n = read(fd, g_midibuf, MIDI_MAX);
  close(fd);
  if (n <= 14)
    {
      return -1;
    }

  g_midisz = (uint32_t)n;

  if (gmseq_prescan(g_midibuf, g_midisz, &g_ps) < 0)
    {
      printf("gmsynth: prescan failed\n");
      return -1;
    }

  printf("gmsynth: %lu s, loop %s, xg %d\n",
         (unsigned long)(g_ps.total_frames / GM2_RATE),
         g_ps.has_loop ? "yes" : "no", g_ps.xg);

  if (bank_load_song(&g_bank, &g_ps, g_pool_samples,
                     GM2_SAMPLE_BYTES, &g_ls) < 0)
    {
      return -1;
    }

  if (gmseq_open(&g_seq, g_midibuf, g_midisz) < 0)
    {
      return -1;
    }

  ledger_reset_song();
  g_pend_valid = false;
  g_seq_ended = false;
  g_frame_shift = 0;
  g_end_frame = 0;
  g_fill_frame = 0;
  g_rdpos = 0;
  g_cons_slot = 0;
  memset(g_echo_l, 0, sizeof(g_echo_l));
  memset(g_echo_r, 0, sizeof(g_echo_r));
  g_lpf_l = g_lpf_r = 0.0f;

  /* Prime the pipeline (fill_block emits the initial channel state
   * into the first block via g_first_block)
   */

  {
    int slot;

    g_first_block = true;
    for (slot = 0; slot < GM2_NBLKS; slot++)
      {
        fill_block(slot);
        dispatch_block(slot);
      }
  }

  return 0;
}

/* play_session() : Pump audio until the song ends or the user acts.
 * Returns a UI_* action (UI_NONE = song finished naturally).
 */

static int play_session(mqd_t mq)
{
  struct audio_msg_s msg;
  FAR struct ap_buffer_s *apb;
  int action = UI_NONE;
  bool running = true;
  int i;

  cleanup_messageq(mq);
  ioctl(g_spkfd, AUDIOIOC_REGISTERMQ, (unsigned long)mq);
  configure(g_spkfd, AUDIO_TYPE_OUTPUT, 2, GM2_RATE, 16);
  set_volume(g_spkfd, g_volume);

  for (i = 0; i < NUM_APB; i++)
    {
      generate_frames(&g_apbs[i]);
      enqueue_buffer(g_spkfd, &g_apbs[i]);
    }

  ioctl(g_spkfd, AUDIOIOC_START, 0);
  printf("gmsynth: playing\n");

  while (running)
    {
      ssize_t sz = mq_receive(mq, (FAR char *)&msg, sizeof(msg), NULL);

      if (sz != sizeof(msg))
        {
          continue;
        }

      switch (msg.msg_id)
        {
          case AUDIO_MSG_DEQUEUE:
            apb = (FAR struct ap_buffer_s *)msg.u.ptr;
            generate_frames(apb);
            enqueue_buffer(g_spkfd, apb);

            action = ui_poll();
            if (action != UI_NONE)
              {
                running = false;
              }
            else if (g_seq_ended && g_end_frame > 0 &&
                     g_fill_frame > g_end_frame)
              {
                running = false;
              }
            break;

          case AUDIO_MSG_UNDERRUN:
            g_underruns++;
            printf("gmsynth: underrun (cons=%d ready=%02x)\n",
                   g_cons_slot, g_blk_ready[g_cons_slot]);
            action = UI_RETRY;
            running = false;
            break;

          case AUDIO_MSG_COMPLETE:
          case AUDIO_MSG_IOERROR:
            running = false;
            break;
        }
    }

  ioctl(g_spkfd, AUDIOIOC_STOP, 0);

  /* Drain the driver's remaining messages */

  cleanup_messageq(mq);
  return action;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  mqd_t mq;
  bool stopped = false;

  sleep(2);       /* Let the serial capture attach (pitfall #8) */

  init_apbs();

  /* GNSS RAM accessibility probe: the big buffers live there.  If
   * this hardfaults, the .gnssram placement must be reverted.
   */

  g_midibuf[0] = 0x5a;
  g_midibuf[MIDI_MAX - 1] = 0xa5;
  g_echo_l[0] = 1.0f;
  if (g_midibuf[0] == 0x5a && g_midibuf[MIDI_MAX - 1] == 0xa5 &&
      g_echo_l[0] == 1.0f)
    {
      printf("gmsynth: GNSS RAM ok (%p)\n", g_midibuf);
    }
  else
    {
      printf("gmsynth: GNSS RAM readback FAILED\n");
      return -1;
    }

  /* Audio device first: opening it after a subcore boots blocks the
   * power domain forever (Phase4 pitfall #1).
   */

  g_spkfd = open(AUDIO_DEVFILE, O_RDWR | O_CLOEXEC);
  if (g_spkfd < 0)
    {
      printf("gmsynth: no audio device\n");
      return -1;
    }

  if (pool_setup() < 0 || workers_boot() < 0)
    {
      return -1;
    }

  if (bank_open(&g_bank) < 0)
    {
      return -1;
    }

  ui_init();
  scan_playlist();
  if (g_nsongs == 0)
    {
      printf("gmsynth: no songs on sd0 or spif\n");
      return -1;
    }

  mq = create_messageq(MSGQ_PATH);

  for (; ; )
    {
      int action;

      if (stopped)
        {
          usleep(30 * 1000);
          action = ui_poll();
          if (action == UI_PLAY)
            {
              stopped = false;
            }
          else if (action == UI_NEXT)
            {
              g_cur_song = (g_cur_song + 1) % g_nsongs;
            }
          else if (action == UI_PREV)
            {
              g_cur_song = (g_cur_song + g_nsongs - 1) % g_nsongs;
            }

          continue;
        }

      if (song_load(g_cur_song) < 0)
        {
          g_cur_song = (g_cur_song + 1) % g_nsongs;
          usleep(500 * 1000);
          continue;
        }

      action = play_session(mq);

      switch (action)
        {
          case UI_STOP:
            stopped = true;
            break;

          case UI_RETRY:
            break;              /* Same song again */

          case UI_PREV:
            g_cur_song = (g_cur_song + g_nsongs - 1) % g_nsongs;
            break;

          case UI_NEXT:
          case UI_NONE:
          default:
            if (action == UI_NEXT || !g_loop_mode || !g_ps.has_loop)
              {
                g_cur_song = (g_cur_song + 1) % g_nsongs;
              }

            /* Loop-mode songs with CC111 never end naturally; if one
             * did (no loop point), advance.
             */
            break;
        }
    }

  return 0;
}
