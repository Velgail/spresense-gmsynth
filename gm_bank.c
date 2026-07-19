/****************************************************************************
 * SPDX-License-Identifier: BSL-1.0
 * synthapps/gmsynth/gm_bank.c
 *
 * gmbank.bin reader and per-song loader.  The full region table stays
 * resident (~38KB); ADPCM blobs are read from SPI flash into the
 * shared pool per song, restricted to the regions the prescan says the
 * song actually plays.
 ****************************************************************************/

#include <nuttx/config.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <math.h>

#include "gm_bank.h"
#include "gm2_shared.h"

#define BANK_MAGIC   0x31424d47      /* "GMB1" little-endian */
#define BANK_VERSION 1

#define HDR_SIZE     24
#define INDEX_COUNT  (BANK_NPROG_SLOTS + 1)

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int bank_open(struct bank_s *b)
{
  uint32_t hdr[6];
  ssize_t n;

  /* SD card first (drag-and-drop updates), SPI flash as fallback */

  static const char *paths[] =
  {
    "/mnt/sd0/gmbank.bin", BANK_PATH
  };

  int pi;

  memset(b, 0, sizeof(*b));
  b->fd = -1;
  for (pi = 0; pi < 2; pi++)
    {
      b->fd = open(paths[pi], O_RDONLY);
      if (b->fd >= 0)
        {
          printf("gmbank: using %s\n", paths[pi]);
          break;
        }
    }

  if (b->fd < 0)
    {
      printf("gmbank: no gmbank.bin on sd0 or spif\n");
      return -1;
    }

  n = read(b->fd, hdr, sizeof(hdr));
  if (n != sizeof(hdr) || hdr[0] != BANK_MAGIC || hdr[1] != BANK_VERSION)
    {
      printf("gmbank: bad header (magic %08lx ver %lu)\n",
             (unsigned long)hdr[0], (unsigned long)hdr[1]);
      close(b->fd);
      return -1;
    }

  b->nregions = hdr[2];
  b->table_off = hdr[3];
  b->blob_off = hdr[4];

  if (read(b->fd, b->index, sizeof(b->index)) != sizeof(b->index))
    {
      close(b->fd);
      return -1;
    }

  /* The region table lives in GNSS RAM (see gmsynth_main.c) */

  {
    static struct bank_region_s g_table_store[720]
        __attribute__((section(".gnssram.bss")));

    if (b->nregions > 720)
      {
        printf("gmbank: too many regions (%lu > 720)\n",
               (unsigned long)b->nregions);
        close(b->fd);
        return -1;
      }

    b->table = g_table_store;
  }

  lseek(b->fd, b->table_off, SEEK_SET);
  if (read(b->fd, b->table,
           b->nregions * sizeof(struct bank_region_s)) !=
      (ssize_t)(b->nregions * sizeof(struct bank_region_s)))
    {
      free(b->table);
      close(b->fd);
      return -1;
    }

  printf("gmbank: %lu regions, table %uB, ok\n",
         (unsigned long)b->nregions,
         (unsigned)(b->nregions * sizeof(struct bank_region_s)));
  return 0;
}

void bank_close(struct bank_s *b)
{
  b->table = NULL;

  if (b->fd >= 0)
    {
      close(b->fd);
      b->fd = -1;
    }
}

/* region_wanted() : Does the song need this region of this slot? */

static bool region_wanted(const struct prescan_s *ps, int slot,
                          const struct bank_region_s *r)
{
  if (!ps->used[slot])
    {
      return false;
    }

  if (slot == BANK_DRUM_SLOT)
    {
      /* Drum regions are single-key */

      return ps->drumkey[r->lokey & 0x7f] != 0;
    }

  return r->lokey <= ps->keyhi[slot] && r->hikey >= ps->keylo[slot];
}

/* prep_lregion() : Precompute the note-on-time derivations */

static void prep_lregion(struct lregion_s *lr)
{
  const struct bank_region_s *r = &lr->br;
  float d = r->decay;
  float rel = r->release < 0.01f ? 0.01f : r->release;
  float semis = r->tune / 100.0f;

  lr->inc_base = (uint32_t)(powf(2.0f, semis / 12.0f) *
                            (float)r->rate / (float)GM2_RATE * 65536.0f);
  lr->att_inc = 1.0f / (r->attack * GM2_RATE + 1.0f);
  lr->dec_k = (d > 0.001f) ? expf(-3.0f / (d * GM2_RATE)) : 0.0f;
  lr->sus = r->sustain;
  lr->rel_k = expf(-5.0f / (rel * GM2_RATE));
  lr->dec_lambda = (lr->dec_k > 0.0f) ? logf(lr->dec_k) : 0.0f;
  lr->rel_lambda = logf(lr->rel_k);
  lr->vel_e = (r->vel_exp > 0) ? r->vel_exp / 32.0f : 2.0f;
}

int bank_load_song(struct bank_s *b, const struct prescan_s *ps,
                   uint8_t *pool_samples, uint32_t budget,
                   struct loadset_s *ls)
{
  uint32_t total = 0;
  bool drop[BANK_NPROG_SLOTS];
  bool thinned[BANK_NPROG_SLOTS];
  static bool skip[720];
  int slot;
  uint32_t i;

  memset(ls, 0, sizeof(*ls));
  memset(drop, 0, sizeof(drop));
  memset(thinned, 0, sizeof(thinned));
  memset(skip, 0, sizeof(skip));

  /* Budget fitting.  Preferred lever: THIN a many-zone slot (keep
   * every other region; the kept neighbours' key ranges are widened at
   * load time, so notes just pitch-stretch further).  Only when no
   * slot is thinnable does a whole least-played slot get dropped --
   * losing an instrument entirely is the last resort (Boss03 lost its
   * harp and tubular bells to the old policy).
   */

  for (; ; )
    {
      total = 0;
      for (slot = 0; slot < BANK_NPROG_SLOTS; slot++)
        {
          if (drop[slot])
            {
              continue;
            }

          for (i = b->index[slot]; i < b->index[slot + 1]; i++)
            {
              if (!skip[i] && region_wanted(ps, slot, &b->table[i]))
                {
                  total += b->table[i].adpcm_nbytes;
                }
            }
        }

      if (total <= budget)
        {
          break;
        }

      /* Thinning candidate: the not-yet-thinned melodic slot with the
       * largest wanted byte count and >= 4 wanted regions
       */

      {
        int cand = -1;
        uint32_t cbytes = 0;

        for (slot = 0; slot < BANK_DRUM_SLOT; slot++)
          {
            uint32_t bytes = 0;
            int nreg = 0;

            if (drop[slot] || thinned[slot] || !ps->used[slot])
              {
                continue;
              }

            for (i = b->index[slot]; i < b->index[slot + 1]; i++)
              {
                if (!skip[i] && region_wanted(ps, slot, &b->table[i]))
                  {
                    bytes += b->table[i].adpcm_nbytes;
                    nreg++;
                  }
              }

            if (nreg >= 4 && bytes > cbytes)
              {
                cbytes = bytes;
                cand = slot;
              }
          }

        if (cand >= 0)
          {
            int nth = 0;

            for (i = b->index[cand]; i < b->index[cand + 1]; i++)
              {
                if (region_wanted(ps, cand, &b->table[i]))
                  {
                    /* Keep even-numbered, skip odd-numbered regions;
                     * never skip the last one (top-of-range anchor)
                     */

                    if ((nth & 1) && i + 1 < b->index[cand + 1])
                      {
                        skip[i] = true;
                      }

                    nth++;
                  }
              }

            thinned[cand] = true;
            printf("gmbank: budget: thinning prog %d zones\n", cand);
            continue;
          }
      }

      /* Nothing left to thin: drop the least-played melodic slot */

      {
        int victim = -1;
        uint32_t vmin = UINT32_MAX;

        for (slot = 0; slot < BANK_DRUM_SLOT; slot++)
          {
            if (ps->used[slot] && !drop[slot] && ps->notes[slot] < vmin)
              {
                vmin = ps->notes[slot];
                victim = slot;
              }
          }

        if (victim < 0)
          {
            printf("gmbank: cannot fit even drums (%lu > %lu)\n",
                   (unsigned long)total, (unsigned long)budget);
            return -1;
          }

        drop[victim] = true;
        ls->dropped_slots++;
        printf("gmbank: budget: dropping prog %d (%lu notes)\n",
               victim, (unsigned long)ps->notes[victim]);
      }
    }

  /* Second pass: load blobs and build the loadset (slot order) */

  {
    uint32_t off = 0;

    for (slot = 0; slot < BANK_NPROG_SLOTS; slot++)
      {
        struct lregion_s *prev = NULL;

        ls->slot_first[slot] = ls->nregs;
        if (drop[slot])
          {
            continue;
          }

        for (i = b->index[slot]; i < b->index[slot + 1]; i++)
          {
            const struct bank_region_s *r = &b->table[i];
            struct lregion_s *lr;

            if (!region_wanted(ps, slot, r))
              {
                continue;
              }

            if (skip[i])
              {
                /* Thinned out: the previous kept region covers this
                 * key range by pitch-stretching further up
                 */

                if (prev != NULL)
                  {
                    prev->br.hikey = r->hikey;
                  }

                continue;
              }

            if (ls->nregs >= MAX_LREGIONS)
              {
                printf("gmbank: MAX_LREGIONS hit, prog %d truncated\n",
                       slot);
                break;
              }

            lr = &ls->regs[ls->nregs];
            memcpy(&lr->br, r, sizeof(*r));
            lr->pool_off = off;

            lseek(b->fd, b->blob_off + r->adpcm_off, SEEK_SET);
            if (read(b->fd, pool_samples + off, r->adpcm_nbytes) !=
                (ssize_t)r->adpcm_nbytes)
              {
                printf("gmbank: blob read failed (prog %d)\n", slot);
                return -1;
              }

            off += r->adpcm_nbytes;
            prep_lregion(lr);
            prev = lr;
            ls->nregs++;
          }
      }

    ls->slot_first[BANK_NPROG_SLOTS] = ls->nregs;
    ls->used_bytes = off;
  }

  printf("gmbank: song load %lu KB in %d regions (%d slots dropped)\n",
         (unsigned long)(ls->used_bytes / 1024), ls->nregs,
         ls->dropped_slots);
  return 0;
}

struct lregion_s *loadset_find(struct loadset_s *ls, int slot, int note)
{
  int i;

  if (slot < 0 || slot >= BANK_NPROG_SLOTS)
    {
      return NULL;
    }

  for (i = ls->slot_first[slot]; i < ls->slot_first[slot + 1]; i++)
    {
      if (ls->regs[i].br.lokey <= note && note <= ls->regs[i].br.hikey)
        {
          return &ls->regs[i];
        }
    }

  return NULL;
}
