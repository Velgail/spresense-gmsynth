/****************************************************************************
 * SPDX-License-Identifier: BSL-1.0
 * synthapps/gmsynth/worker/gmvoice/gmvoice.c
 *
 * gmsynth renderer worker: one of four identical ASMP subcores, 16
 * voices at 48kHz.  Fully commanded by the supervisor's ledger via
 * per-block event lists; renders ADPCM voices with linear
 * interpolation and ADSR into dry L/R + echo-send L/R planes in the
 * shared pool.  Sample data lives in the same pool (attached mpshm;
 * works because this worker image is small -- Phase A spike).
 *
 * No libc / libm: everything transcendental is precomputed by the
 * supervisor into the EV_START parameters.
 ****************************************************************************/

#include <errno.h>

#include <asmp/types.h>
#include <asmp/mpshm.h>
#include <asmp/mpmq.h>

#include "asmp.h"

#include <stdint.h>
#include <stddef.h>

#define FAR

#include "gm2_shared.h"

/* No libc on the worker: GCC emits memset calls for large
 * zero-initialization loops; the pointer must be volatile or -O3
 * turns the loop into a recursive call to itself.
 */

void *memset(void *s, int c, size_t n)
{
  volatile uint8_t *p = (volatile uint8_t *)s;

  while (n--)
    {
      *p++ = (uint8_t)c;
    }

  return s;
}

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define ASSERT(cond) if (!(cond)) wk_abort()

#define DEMCR       (*(volatile uint32_t *)0xe000edfc)
#define DWT_CTRL    (*(volatile uint32_t *)0xe0001000)
#define DWT_CYCCNT  (*(volatile uint32_t *)0xe0001004)
#define CPACR       (*(volatile uint32_t *)0xe000ed88)

#define CPU_MHZ     (156)

/* Voices are summed into int16 planes with this headroom factor; the
 * supervisor compensates when mixing the four workers.
 */

#define WORKER_SCALE (0.25f)

/* Fast release for steals and hi-hat chokes (Phase4's 0.995 at 32kHz,
 * rate-adjusted)
 */

#define KILL_K       (0.99667f)

#define ENV_IDLE     (0)
#define ENV_ATTACK   (1)
#define ENV_DECAY    (2)
#define ENV_SUSTAIN  (3)
#define ENV_RELEASE  (4)

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct voice_s
{
  uint8_t state;
  uint8_t ch;

  const uint8_t *adpcm;
  uint32_t len;
  uint32_t loopstart;
  uint32_t loopend;        /* Inclusive; 0 = one-shot */
  int16_t loop_pred;
  uint8_t loop_step;

  uint32_t inc_base;       /* 16.16 at bend factor 1.0 */
  uint32_t pos_int;
  uint16_t pos_frac;
  uint32_t inc_int;
  uint16_t inc_frac;

  uint32_t dec_idx;
  uint32_t nib_idx;
  int16_t pred;
  uint8_t stepidx;
  int16_t s0;
  int16_t s1;

  float env;
  float att_inc;
  float dec_k;
  float sus;
  float rel_k;
  float cur_rel;

  float amp;
  float pan_l;
  float pan_r;
  float send;
};

/****************************************************************************
 * Private Data
 ****************************************************************************/

static struct gm2_pool_s *g_pool;
static const uint8_t *g_samples;
static int g_myidx;

/* Render blocks live in this worker's own image; the supervisor
 * addresses them as loadaddr + the offset reported in HELLO.
 */

static struct gm2_blkarea_s g_area;

static struct voice_s g_voices[GM2_NVOICES];
static float g_chgain[16];
static float g_chbend[16];

static const int8_t g_ima_index[8] =
{
  -1, -1, -1, -1, 2, 4, 6, 8
};

static const int16_t g_ima_steps[89] =
{
      7,     8,     9,    10,    11,    12,    13,    14,    16,    17,
     19,    21,    23,    25,    28,    31,    34,    37,    41,    45,
     50,    55,    60,    66,    73,    80,    88,    97,   107,   118,
    130,   143,   157,   173,   190,   209,   230,   253,   279,   307,
    337,   371,   408,   449,   494,   544,   598,   658,   724,   796,
    876,   963,  1060,  1166,  1282,  1411,  1552,  1707,  1878,  2066,
   2272,  2499,  2749,  3024,  3327,  3660,  4026,  4428,  4871,  5358,
   5894,  6484,  7132,  7845,  8630,  9493, 10442, 11487, 12635, 13899,
  15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767
};

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static inline int16_t adpcm_next(struct voice_s *v)
{
  uint8_t nib;
  int step;
  int delta;
  int pred;

  if (v->loopend > 0 && v->nib_idx > v->loopend)
    {
      v->nib_idx = v->loopstart;
      v->pred = v->loop_pred;
      v->stepidx = v->loop_step;
    }

  pred = v->pred;

  nib = v->adpcm[v->nib_idx >> 1];
  nib = (v->nib_idx & 1) ? (nib >> 4) : (nib & 0x0f);
  v->nib_idx++;

  step = g_ima_steps[v->stepidx];
  delta = step >> 3;
  if (nib & 4)
    {
      delta += step;
    }

  if (nib & 2)
    {
      delta += step >> 1;
    }

  if (nib & 1)
    {
      delta += step >> 2;
    }

  if (nib & 8)
    {
      pred -= delta;
    }
  else
    {
      pred += delta;
    }

  if (pred > 32767)
    {
      pred = 32767;
    }
  else if (pred < -32768)
    {
      pred = -32768;
    }

  v->pred = (int16_t)pred;

  step = v->stepidx + g_ima_index[nib & 7];
  if (step < 0)
    {
      step = 0;
    }
  else if (step > 88)
    {
      step = 88;
    }

  v->stepidx = (uint8_t)step;
  return (int16_t)pred;
}

static void set_inc(struct voice_s *v)
{
  float f = (float)v->inc_base * g_chbend[v->ch];
  uint32_t inc = (uint32_t)f;

  if (inc > 0x3fffff)
    {
      inc = 0x3fffff;
    }

  v->inc_int = inc >> 16;
  v->inc_frac = inc & 0xffff;
}

static void voice_start(struct voice_s *v, const struct gm2_start_s *s)
{
  v->adpcm     = g_samples + s->adpcm_off;
  v->len       = s->len;
  v->loopstart = s->loopstart;
  v->loopend   = s->loopend;
  v->loop_pred = s->loop_pred;
  v->loop_step = s->loop_step;
  v->ch        = s->ch & 0x0f;
  v->inc_base  = s->inc;

  v->pos_int   = 0;
  v->pos_frac  = 0;
  v->dec_idx   = 0;
  v->nib_idx   = 0;
  v->pred      = 0;
  v->stepidx   = 0;
  v->s0        = 0;
  v->s1        = 0;

  v->env       = 0.0f;
  v->att_inc   = s->att_inc;
  v->dec_k     = s->dec_k;
  v->sus       = s->sus;
  v->rel_k     = s->rel_k;
  v->cur_rel   = s->rel_k;

  v->amp       = s->amp;
  v->pan_l     = s->pan_l;
  v->pan_r     = s->pan_r;
  v->send      = s->send;

  set_inc(v);
  v->state = ENV_ATTACK;
}

static void apply_event(const struct gm2_ev_s *ev)
{
  int i;

  switch (ev->type)
    {
      case GM2_EV_START:
        if (ev->vslot < GM2_NVOICES)
          {
            voice_start(&g_voices[ev->vslot], &ev->u.start);
          }
        break;

      case GM2_EV_RELEASE:
        if (ev->vslot < GM2_NVOICES &&
            g_voices[ev->vslot].state != ENV_IDLE)
          {
            g_voices[ev->vslot].state = ENV_RELEASE;
          }
        break;

      case GM2_EV_KILL:
        if (ev->vslot < GM2_NVOICES &&
            g_voices[ev->vslot].state != ENV_IDLE)
          {
            g_voices[ev->vslot].state = ENV_RELEASE;
            g_voices[ev->vslot].cur_rel = KILL_K;
          }
        break;

      case GM2_EV_CHGAIN:
        g_chgain[ev->vslot & 0x0f] = ev->u.f;
        break;

      case GM2_EV_CHBEND:
        g_chbend[ev->vslot & 0x0f] = ev->u.f;
        for (i = 0; i < GM2_NVOICES; i++)
          {
            if (g_voices[i].state != ENV_IDLE &&
                g_voices[i].ch == (ev->vslot & 0x0f))
              {
                set_inc(&g_voices[i]);
              }
          }
        break;

      case GM2_EV_ALLOFF:
        for (i = 0; i < GM2_NVOICES; i++)
          {
            if (g_voices[i].state != ENV_IDLE)
              {
                g_voices[i].state = ENV_RELEASE;
                g_voices[i].cur_rel = KILL_K;
              }
          }
        break;
    }
}

static inline float render_voice(struct voice_s *v)
{
  uint32_t target;
  uint32_t carry;
  float smp;

  target = v->pos_int;

  if (v->loopend == 0 && target + 1 >= v->len)
    {
      v->state = ENV_IDLE;
      return 0.0f;
    }

  while (v->dec_idx <= target + 1)
    {
      v->s0 = v->s1;
      v->s1 = adpcm_next(v);
      v->dec_idx++;
    }

  smp = ((float)v->s0 +
         ((float)v->s1 - (float)v->s0) *
         ((float)v->pos_frac * (1.0f / 65536.0f))) * (1.0f / 32768.0f);

  carry = (uint32_t)v->pos_frac + v->inc_frac;
  v->pos_frac = carry & 0xffff;
  v->pos_int += v->inc_int + (carry >> 16);

  switch (v->state)
    {
      case ENV_ATTACK:
        v->env += v->att_inc;
        if (v->env >= 1.0f)
          {
            v->env = 1.0f;
            v->state = (v->dec_k > 0.0f) ? ENV_DECAY : ENV_SUSTAIN;
          }
        break;

      case ENV_DECAY:
        v->env = v->sus + (v->env - v->sus) * v->dec_k;
        if (v->env - v->sus < 0.001f)
          {
            v->state = (v->sus < 0.001f) ? ENV_IDLE : ENV_SUSTAIN;
          }
        break;

      case ENV_RELEASE:
        v->env *= v->cur_rel;
        if (v->env < 0.001f)
          {
            v->state = ENV_IDLE;
          }
        break;

      default:
        break;
    }

  return smp * v->env * v->amp;
}

static void render_block(struct gm2_blk_s *blk)
{
  uint32_t ev_i = 0;
  int f;
  int i;

  for (f = 0; f < GM2_BLK_FRAMES; f++)
    {
      float dry_l = 0.0f;
      float dry_r = 0.0f;
      float snd_l = 0.0f;
      float snd_r = 0.0f;
      int32_t o;

      while (ev_i < blk->nevents && blk->events[ev_i].offset <= f)
        {
          apply_event(&blk->events[ev_i]);
          ev_i++;
        }

      for (i = 0; i < GM2_NVOICES; i++)
        {
          struct voice_s *v = &g_voices[i];

          if (v->state != ENV_IDLE)
            {
              float s = render_voice(v);

              if (s != 0.0f)
                {
                  float g = g_chgain[v->ch];
                  float sl = s * g * v->pan_l;
                  float sr = s * g * v->pan_r;

                  dry_l += sl;
                  dry_r += sr;
                  snd_l += sl * v->send;
                  snd_r += sr * v->send;
                }
            }
        }

      o = (int32_t)(dry_l * (WORKER_SCALE * 32767.0f));
      blk->dry[f * 2] =
        (o > 32767) ? 32767 : (o < -32768) ? -32768 : (int16_t)o;
      o = (int32_t)(dry_r * (WORKER_SCALE * 32767.0f));
      blk->dry[f * 2 + 1] =
        (o > 32767) ? 32767 : (o < -32768) ? -32768 : (int16_t)o;
      o = (int32_t)(snd_l * (WORKER_SCALE * 32767.0f));
      blk->send[f * 2] =
        (o > 32767) ? 32767 : (o < -32768) ? -32768 : (int16_t)o;
      o = (int32_t)(snd_r * (WORKER_SCALE * 32767.0f));
      blk->send[f * 2 + 1] =
        (o > 32767) ? 32767 : (o < -32768) ? -32768 : (int16_t)o;
    }
}

/****************************************************************************
 * Fault reporting (send stacked PC once, then park)
 ****************************************************************************/

static mpmq_t g_mq;
static uint32_t g_vectors[128] __attribute__((aligned(512)));

#define VTOR (*(volatile uint32_t *)0xe000ed08)

void fault_report(uint32_t pc)
{
  mpmq_send(&g_mq, GM2_MSG_HELLO, 0xe0000000 | (pc & 0x0fffffff));

  for (; ; )
    {
      __asm__ __volatile__("wfi");
    }
}

__attribute__((naked)) static void fault_handler(void)
{
  __asm__ __volatile__(
    "mrs r0, msp\n"
    "ldr r0, [r0, #24]\n"
    "b fault_report\n");
}

static void install_fault_handlers(void)
{
  const uint32_t *orig = (const uint32_t *)0;
  int i;

  for (i = 0; i < 128; i++)
    {
      g_vectors[i] = orig[i];
    }

  g_vectors[3] = (uint32_t)fault_handler;
  g_vectors[4] = (uint32_t)fault_handler;
  g_vectors[5] = (uint32_t)fault_handler;
  g_vectors[6] = (uint32_t)fault_handler;

  VTOR = (uint32_t)g_vectors;
  __asm__ __volatile__("dsb");
  __asm__ __volatile__("isb");
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(void)
{
  mpshm_t shm;
  uint32_t data;
  void *va;
  int ret;
  int i;

  CPACR |= (0xf << 20);
  __asm__ __volatile__("dsb");
  __asm__ __volatile__("isb");

  ret = mpmq_init(&g_mq, GM2_KEY_MQ, 0);
  ASSERT(ret == 0);

  install_fault_handlers();

  DEMCR |= (1 << 24);
  DWT_CYCCNT = 0;
  DWT_CTRL |= 1;

  ret = mpshm_init(&shm, GM2_KEY_SHM, GM2_POOL_SIZE);
  if (ret != 0)
    {
      mpmq_send(&g_mq, GM2_MSG_HELLO, 0xf1000000 | ((-ret) & 0xffff));
      for (; ; ) __asm__ __volatile__("wfi");
    }

  va = mpshm_attach(&shm, 0);
  if (va == 0)
    {
      mpmq_send(&g_mq, GM2_MSG_HELLO, 0xf2000000);
      for (; ; ) __asm__ __volatile__("wfi");
    }

  g_pool = (struct gm2_pool_s *)va;
  if (g_pool->magic != GM2_MAGIC)
    {
      mpmq_send(&g_mq, GM2_MSG_HELLO,
                0xf3000000 | (g_pool->magic & 0xfffff));
      for (; ; ) __asm__ __volatile__("wfi");
    }

  g_samples = (const uint8_t *)va + g_pool->sample_off;

  for (i = 0; i < GM2_NVOICES; i++)
    {
      g_voices[i].state = ENV_IDLE;
    }

  for (i = 0; i < 16; i++)
    {
      g_chgain[i] = 0.62f;      /* (100/127)^2: GM default CC7 */
      g_chbend[i] = 1.0f;
    }

  mpmq_send(&g_mq, GM2_MSG_HELLO,
            0xa0000000 | ((uint32_t)&g_area & 0xfffff));

  for (; ; )
    {
      static int diag = 6;

      /* Stage marker: 0xD1 = entering receive (sent once) */

      if (diag == 6)
        {
          diag--;
          mpmq_send(&g_mq, GM2_MSG_HELLO, 0xd1000000);
        }

      ret = mpmq_receive(&g_mq, &data);

      /* DIAG: echo the first few received message ids back as HELLOs
       * so the supervisor log shows whether commands arrive at all.
       */

      if (diag > 0)
        {
          diag--;
          mpmq_send(&g_mq, GM2_MSG_HELLO,
                    0xb0000000 | ((uint32_t)ret << 20) |
                    (data & 0xfffff));
        }

      if (ret == GM2_MSG_RENDER)
        {
          uint32_t slot = data % GM2_NBLKS;
          uint32_t t0 = DWT_CYCCNT;
          uint32_t us;

          render_block(&g_area.blks[slot]);

          us = (DWT_CYCCNT - t0) / CPU_MHZ;
          mpmq_send(&g_mq, GM2_MSG_DONE,
                    (slot & 0xff) | ((us & 0xffffff) << 8));
        }
      else if (ret == GM2_MSG_RESET)
        {
          int j;

          for (j = 0; j < GM2_NVOICES; j++)
            {
              g_voices[j].state = ENV_IDLE;
            }

          for (j = 0; j < 16; j++)
            {
              g_chgain[j] = 0.62f;
              g_chbend[j] = 1.0f;
            }

          memset(&g_area, 0, sizeof(g_area));
          mpmq_send(&g_mq, GM2_MSG_HELLO, 0xc0000000);
        }
      else if (ret == GM2_MSG_CFG)
        {
          g_myidx = (int)(data % GM2_NWORKERS);
        }
      else if (ret == GM2_MSG_EXIT)
        {
          break;
        }
    }

  return 0;
}
