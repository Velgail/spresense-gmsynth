/****************************************************************************
 * SPDX-License-Identifier: BSL-1.0
 * synthapps/gmsynth/gm_bank.h
 *
 * gmbank.bin access + per-song region loading into the shared pool.
 * Binary format: see gmbank_format.py (56-byte region rows).
 ****************************************************************************/

#ifndef __GM_BANK_H
#define __GM_BANK_H

#include <stdint.h>
#include <stdbool.h>

#ifndef BANK_PATH               /* Host trace harness overrides via -D */
#define BANK_PATH        "/mnt/spif/gmbank.bin"
#endif
#define BANK_NPROG_SLOTS (129)      /* 0-127 melodic + 128 drum kit */
#define BANK_DRUM_SLOT   (128)

#define BANK_FLAG_LOOPED (1)

/* One region row, exactly as stored in gmbank.bin */

struct bank_region_s
{
  uint8_t lokey;
  uint8_t hikey;
  uint8_t root;
  uint8_t excl;
  int16_t tune;            /* cents */
  uint8_t scale;           /* scaleTuning, 100 = normal */
  uint8_t pan;             /* 0-127 */
  uint16_t rate;           /* Hz of stored data */
  uint16_t flags;
  uint32_t length;         /* samples */
  uint32_t loopstart;
  uint32_t loopend;        /* inclusive; 0 = one-shot */
  int16_t loop_pred;
  uint8_t loop_step;
  uint8_t vel_exp;         /* velocity exponent x32; 0 -> 2.0 */
  float gain;
  float attack;            /* seconds */
  float decay;
  float sustain;           /* level */
  float release;           /* seconds */
  uint32_t adpcm_off;      /* into blob */
  uint32_t adpcm_nbytes;
} __attribute__((packed));

/* A region resident in the pool for the current song, with everything
 * precomputed that the ledger needs at note-on time.
 */

struct lregion_s
{
  struct bank_region_s br;
  uint32_t pool_off;       /* offset within the pool sample area */
  uint32_t inc_base;       /* 16.16 increment at key = root (48kHz) */
  float att_inc;
  float dec_k;
  float sus;
  float rel_k;
  float dec_lambda;        /* ln(dec_k), for env estimation */
  float rel_lambda;        /* ln(rel_k) */
  float vel_e;
};

#define MAX_LREGIONS  (96)

struct loadset_s
{
  struct lregion_s regs[MAX_LREGIONS];
  int nregs;
  int slot_first[BANK_NPROG_SLOTS + 1];  /* regs sorted by slot */
  uint32_t used_bytes;
  int dropped_slots;
};

/* Prescan results (filled by seq_prescan) */

struct prescan_s
{
  bool used[BANK_NPROG_SLOTS];
  uint8_t keylo[BANK_NPROG_SLOTS];
  uint8_t keyhi[BANK_NPROG_SLOTS];
  uint32_t notes[BANK_NPROG_SLOTS];
  uint8_t drumkey[128];    /* nonzero = used */
  bool xg;
  bool has_loop;
  uint32_t loop_frame;
  uint32_t total_frames;
};

struct bank_s
{
  int fd;
  uint32_t nregions;
  uint32_t table_off;
  uint32_t blob_off;
  uint16_t index[BANK_NPROG_SLOTS + 1];
  struct bank_region_s *table;    /* static GNSS RAM storage, nregions rows */
};

int bank_open(struct bank_s *b);
void bank_close(struct bank_s *b);

/* Load the regions a song needs into pool_samples (budget bytes).
 * Least-played slots are dropped whole when over budget.
 */

int bank_load_song(struct bank_s *b, const struct prescan_s *ps,
                   uint8_t *pool_samples, uint32_t budget,
                   struct loadset_s *ls);

struct lregion_s *loadset_find(struct loadset_s *ls, int slot, int note);

#endif /* __GM_BANK_H */
