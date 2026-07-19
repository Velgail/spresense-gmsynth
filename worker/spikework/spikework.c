/****************************************************************************
 * SPDX-License-Identifier: BSL-1.0
 * synthapps/gmsynth/worker/spikework/spikework.c
 *
 * Phase A spike worker: small (code-only) ASMP worker that attaches the
 * shared sample pool and runs synthetic voice-rendering benchmarks whose
 * inner loop is copied from the Phase4 gmwork renderer, but fetching
 * ADPCM data from the shared pool instead of the worker image.
 ****************************************************************************/

#include <errno.h>

#include <asmp/types.h>
#include <asmp/mpshm.h>
#include <asmp/mpmq.h>

#include "asmp.h"

#include <stdint.h>
#include <stddef.h>

#define FAR

#include "spike_shared.h"

/* No libc on the worker: GCC emits memset calls for large
 * zero-initialization loops, so provide one.  The pointer must be
 * volatile, otherwise -O3 recognizes the loop as a memset pattern and
 * replaces the body with a call to itself (infinite recursion).
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

/* DWT cycle counter (Cortex-M4) for load measurement */

#define DEMCR       (*(volatile uint32_t *)0xe000edfc)
#define DWT_CTRL    (*(volatile uint32_t *)0xe0001000)
#define DWT_CYCCNT  (*(volatile uint32_t *)0xe0001004)
#define CPACR       (*(volatile uint32_t *)0xe000ed88)

#define CPU_MHZ     (156)

/****************************************************************************
 * Private Types
 ****************************************************************************/

/* Synthetic voice: same field layout and inner loop as the Phase4
 * renderer, but the region is a slice of the shared pool.
 */

struct voice_s
{
  const uint8_t *adpcm;     /* Nibble stream base (shared pool) */
  uint32_t nib_len;         /* Nibbles before wrapping */

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
  float amp;
};

/****************************************************************************
 * Private Data
 ****************************************************************************/

static struct voice_s g_voices[SPIKE_MAX_VOICES];
static struct spike_pool_s *g_pool;
static int g_myidx;

static volatile uint32_t g_sink;    /* Defeats dead-read elimination */

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

  if (v->nib_idx >= v->nib_len)
    {
      v->nib_idx = 0;         /* Benchmark voices loop the whole slice */
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

static void voices_setup(int nvoices, int mode)
{
  uint32_t slice = g_pool->sample_bytes / SPIKE_MAX_VOICES;
  int i;

  for (i = 0; i < nvoices; i++)
    {
      struct voice_s *v = &g_voices[i];

      v->adpcm    = g_pool->samples + (uint32_t)i * slice;
      v->nib_len  = slice * 2;
      v->pos_int  = 0;
      v->pos_frac = 0;

      /* Pitch ratios 0.69 .. 2.48 in 16.16, a realistic spread.
       * mode 1 models 48kHz output: same source rate, so the ratios
       * shrink by 32/48 while the per-frame work count rises 1.5x.
       */

      v->inc_int  = 0;
      v->inc_frac = 0;
      {
        uint32_t inc = 0xb000 + (uint32_t)i * 0x1400;

        if (mode == 1)
          {
            inc = inc * 2 / 3;
          }

        v->inc_int  = inc >> 16;
        v->inc_frac = inc & 0xffff;
      }

      v->dec_idx  = 0;
      v->nib_idx  = 0;
      v->pred     = 0;
      v->stepidx  = 0;
      v->s0       = 0;
      v->s1       = 0;
      v->env      = 1.0f;
      v->amp      = 0.03f;
    }
}

static inline float render_voice(struct voice_s *v)
{
  uint32_t target;
  uint32_t carry;
  float smp;

  target = v->pos_int;

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

  /* Keep the envelope multiply on the frame path like the real
   * renderer, decaying slowly so values stay live.
   */

  v->env *= 0.9999995f;

  return smp * v->env * v->amp;
}

static void render_block(int nvoices)
{
  int16_t *out = g_pool->out[g_myidx];
  int f;
  int i;

  for (f = 0; f < SPIKE_BLK_FRAMES; f++)
    {
      float acc_l = 0.0f;
      float acc_r = 0.0f;
      int32_t o;

      for (i = 0; i < nvoices; i++)
        {
          float s = render_voice(&g_voices[i]);

          acc_l += s;
          acc_r += s * 0.8f;      /* Stand-in for pan gains */
        }

      o = (int32_t)(acc_l * 32767.0f);
      out[f * 2] =
        (o > 32767) ? 32767 : (o < -32768) ? -32768 : (int16_t)o;
      o = (int32_t)(acc_r * 32767.0f);
      out[f * 2 + 1] =
        (o > 32767) ? 32767 : (o < -32768) ? -32768 : (int16_t)o;
    }
}

static uint32_t checksum_samples(void)
{
  const uint32_t *p = (const uint32_t *)g_pool->samples;
  uint32_t n = g_pool->sample_bytes / 4;
  uint32_t sum = 0;

  while (n--)
    {
      sum += *p++;
    }

  return sum;
}

static uint32_t memrd_bench(uint32_t kbytes)
{
  const uint32_t *p = (const uint32_t *)g_pool->samples;
  uint32_t n = (kbytes * 1024) / 4;
  uint32_t sum = 0;
  uint32_t t0;
  uint32_t i;

  if (kbytes * 1024 > g_pool->sample_bytes)
    {
      n = g_pool->sample_bytes / 4;
    }

  t0 = DWT_CYCCNT;
  for (i = 0; i < n; i++)
    {
      sum += p[i];
    }

  g_sink = sum;
  return (DWT_CYCCNT - t0) / CPU_MHZ;
}

/****************************************************************************
 * Fault reporting (Phase4 recipe: send the stacked PC once, then park)
 ****************************************************************************/

static mpmq_t g_mq;
static uint32_t g_vectors[128] __attribute__((aligned(512)));

#define VTOR (*(volatile uint32_t *)0xe000ed08)

void fault_report(uint32_t pc)
{
  mpmq_send(&g_mq, SPIKE_MSG_HELLO, 0xe0000000 | (pc & 0x0fffffff));

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

  g_vectors[3] = (uint32_t)fault_handler;   /* HardFault */
  g_vectors[4] = (uint32_t)fault_handler;   /* MemManage */
  g_vectors[5] = (uint32_t)fault_handler;   /* BusFault */
  g_vectors[6] = (uint32_t)fault_handler;   /* UsageFault */

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

  CPACR |= (0xf << 20);
  __asm__ __volatile__("dsb");
  __asm__ __volatile__("isb");

  ret = mpmq_init(&g_mq, SPIKE_KEY_MQ, 0);
  ASSERT(ret == 0);

  install_fault_handlers();

  DEMCR |= (1 << 24);
  DWT_CYCCNT = 0;
  DWT_CTRL |= 1;

  /* THE spike question: init + attach the big shm from a small worker */

  ret = mpshm_init(&shm, SPIKE_KEY_SHM, SPIKE_POOL_SIZE);
  if (ret != 0)
    {
      mpmq_send(&g_mq, SPIKE_MSG_HELLO, 0xf1000000 | ((-ret) & 0xffff));
      for (; ; ) __asm__ __volatile__("wfi");
    }

  va = mpshm_attach(&shm, 0);
  if (va == 0)
    {
      mpmq_send(&g_mq, SPIKE_MSG_HELLO, 0xf2000000);
      for (; ; ) __asm__ __volatile__("wfi");
    }

  g_pool = (struct spike_pool_s *)va;

  /* Detect the Phase4 silent-mapping failure immediately */

  if (g_pool->magic != SPIKE_MAGIC)
    {
      mpmq_send(&g_mq, SPIKE_MSG_HELLO,
                0xf3000000 | (g_pool->magic & 0xfffff));
      for (; ; ) __asm__ __volatile__("wfi");
    }

  mpmq_send(&g_mq, SPIKE_MSG_HELLO, 0xa0000000 | ((uint32_t)va & 0xfffff));

  for (; ; )
    {
      ret = mpmq_receive(&g_mq, &data);

      if (ret == SPIKE_MSG_CFG)
        {
          g_myidx = (int)(data % SPIKE_NWORKERS);
        }
      else if (ret == SPIKE_MSG_CSUM)
        {
          uint32_t sum = checksum_samples();
          mpmq_send(&g_mq, SPIKE_MSG_CSUM_R,
                    ((sum ^ (sum >> 24)) & 0xffffff) << 8);
        }
      else if (ret == SPIKE_MSG_RENDER)
        {
          uint32_t nvoices = data & 0x3f;
          uint32_t nblocks = (data >> 8) & 0xff;
          uint32_t mode = (data >> 16) & 0xf;
          uint32_t cyc = 0;
          uint32_t b;

          if (nvoices > SPIKE_MAX_VOICES)
            {
              nvoices = SPIKE_MAX_VOICES;
            }

          voices_setup((int)nvoices, (int)mode);

          for (b = 0; b < nblocks; b++)
            {
              uint32_t t0 = DWT_CYCCNT;

              render_block((int)nvoices);
              cyc += DWT_CYCCNT - t0;
            }

          mpmq_send(&g_mq, SPIKE_MSG_DONE,
                    ((cyc / CPU_MHZ / (nblocks ? nblocks : 1)) & 0xffffff)
                    << 8);
        }
      else if (ret == SPIKE_MSG_MEMRD)
        {
          uint32_t us = memrd_bench(data);
          mpmq_send(&g_mq, SPIKE_MSG_MEMRD_R, (us & 0xffffff) << 8);
        }
      else if (ret == SPIKE_MSG_EXIT)
        {
          break;
        }
    }

  return 0;
}
