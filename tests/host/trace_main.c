/****************************************************************************
 * SPDX-License-Identifier: BSL-1.0
 * tests/host/trace_main.c
 *
 * Host-side event-trace harness: compiles the REAL supervisor code
 * (gmsynth_main.c is #included below, gm_seq.c / gm_bank.c are linked
 * alongside) against stub NuttX/ASMP headers, drives fill_block() over
 * a fixed SMF, and dumps every per-worker block event as JSON.
 *
 * This is the Layer-3 regression: SMF parsing, channel state, voice
 * allocation/steal, sustain, drum choke, coalescing, event-overflow
 * behavior -- everything the offline audio renderer does NOT cover.
 * Workers, audio and UI are stubbed out; their code only has to build.
 *
 * Build/run via tests/trace_regression.py (needs gmbank.bin in cwd).
 ****************************************************************************/

#define main firmware_main
#include "gmsynth_main.c"
#undef main

/* Host-owned stand-ins for the worker-resident block areas and the
 * mpshm sample pool.
 */

static struct gm2_blkarea_s h_blkarea[GM2_NWORKERS];
static uint8_t h_pool[GM2_SAMPLE_BYTES];

/* JSON goes to the ORIGINAL stdout; everything the firmware printf()s
 * (bank/loader diagnostics) is redirected to stderr in main().
 */

static FILE *g_json;

static const char *ev_name(int type)
{
  switch (type)
    {
      case GM2_EV_START:   return "START";
      case GM2_EV_RELEASE: return "RELEASE";
      case GM2_EV_KILL:    return "KILL";
      case GM2_EV_CHGAIN:  return "CHGAIN";
      case GM2_EV_CHBEND:  return "CHBEND";
      case GM2_EV_ALLOFF:  return "ALLOFF";
      default:             return "?";
    }
}

static int dump_block(int n, int slot, bool *first_blk)
{
  int w;
  int total = 0;
  bool first_ev = true;

  for (w = 0; w < GM2_NWORKERS; w++)
    {
      total += (int)g_blkarea[w]->blks[slot].nevents;
    }

  if (total == 0)
    {
      return 0;
    }

  fprintf(g_json, "%s  {\"n\": %d, \"events\": [\n", *first_blk ? "" : ",\n", n);
  *first_blk = false;

  for (w = 0; w < GM2_NWORKERS; w++)
    {
      FAR struct gm2_blk_s *blk = &g_blkarea[w]->blks[slot];
      uint32_t i;

      for (i = 0; i < blk->nevents; i++)
        {
          FAR struct gm2_ev_s *ev = &blk->events[i];

          fprintf(g_json, "%s   {\"w\": %d, \"i\": %u, \"offset\": %u, "
                 "\"type\": \"%s\", \"vslot\": %u",
                 first_ev ? "" : ",\n",
                 w, (unsigned)i, (unsigned)ev->offset,
                 ev_name(ev->type), (unsigned)ev->vslot);
          first_ev = false;

          if (ev->type == GM2_EV_START)
            {
              fprintf(g_json, ", \"ch\": %u", (unsigned)ev->u.start.ch);
            }
          else if (ev->type == GM2_EV_CHGAIN ||
                   ev->type == GM2_EV_CHBEND)
            {
              /* Value depends on MIDI state only (not bank content),
               * so it is stable across bank recalibrations.
               */

              fprintf(g_json, ", \"value\": %.6g", (double)ev->u.f);
            }

          fprintf(g_json, "}");
        }
    }

  fprintf(g_json, "\n  ]}");
  return total;
}

int main(int argc, char *argv[])
{
  const char *midpath = NULL;
  bool loop = false;
  int max_blocks = 4000;
  int a;
  int fd;
  ssize_t n;
  int nblk;
  bool first_blk = true;

  for (a = 1; a < argc; a++)
    {
      if (strcmp(argv[a], "--loop") == 0)
        {
          loop = true;
        }
      else if (strcmp(argv[a], "--max-blocks") == 0 && a + 1 < argc)
        {
          max_blocks = atoi(argv[++a]);
        }
      else
        {
          midpath = argv[a];
        }
    }

  if (midpath == NULL)
    {
      fprintf(stderr,
              "usage: gmtrace <song.mid> [--loop] [--max-blocks N]\n"
              "       (reads gmbank.bin from the current directory)\n");
      return 2;
    }

  /* Keep the original stdout for the JSON document; everything the
   * firmware sources printf() (bank/loader diagnostics) moves to
   * stderr so the trace stays parseable.
   */

  g_json = fdopen(dup(1), "w");
  if (g_json == NULL)
    {
      return 2;
    }

  dup2(2, 1);

  fd = open(midpath, O_RDONLY);
  if (fd < 0)
    {
      fprintf(stderr, "gmtrace: cannot open %s\n", midpath);
      return 2;
    }

  n = read(fd, g_midibuf, MIDI_MAX);
  close(fd);
  if (n <= 14)
    {
      fprintf(stderr, "gmtrace: short read\n");
      return 2;
    }

  g_midisz = (uint32_t)n;

  if (gmseq_prescan(g_midibuf, g_midisz, &g_ps) < 0)
    {
      fprintf(stderr, "gmtrace: prescan failed\n");
      return 2;
    }

  if (bank_open(&g_bank) < 0)
    {
      fprintf(stderr, "gmtrace: bank_open failed (need gmbank.bin)\n");
      return 2;
    }

  if (bank_load_song(&g_bank, &g_ps, h_pool, GM2_SAMPLE_BYTES,
                     &g_ls) < 0)
    {
      fprintf(stderr, "gmtrace: bank_load_song failed\n");
      return 2;
    }

  /* Same per-song state reset as song_load(), workers replaced by the
   * host block areas.
   */

  for (a = 0; a < GM2_NWORKERS; a++)
    {
      g_blkarea[a] = &h_blkarea[a];
    }

  if (gmseq_open(&g_seq, g_midibuf, g_midisz) < 0)
    {
      fprintf(stderr, "gmtrace: gmseq_open failed\n");
      return 2;
    }

  ledger_reset_song();
  g_loop_mode = loop;
  g_pend_valid = false;
  g_seq_ended = false;
  g_frame_shift = 0;
  g_end_frame = 0;
  g_fill_frame = 0;
  g_first_block = true;

  fprintf(g_json, "{\"midi\": \"%s\", \"loop\": %s,\n \"blocks\": [\n",
          midpath, loop ? "true" : "false");

  for (nblk = 0; nblk < max_blocks; nblk++)
    {
      int slot = nblk % GM2_NBLKS;

      fill_block(slot);
      dump_block(nblk, slot, &first_blk);

      if (g_seq_ended)
        {
          nblk++;
          break;
        }
    }

  fprintf(g_json, "\n ],\n \"summary\": {\"blocks\": %d, \"steals\": %lu, "
          "\"self_steals\": %lu, \"steals_active\": %lu, "
          "\"ev_dropped\": %lu, "
          "\"wcount\": [%d, %d, %d, %d]}\n}\n",
          nblk,
          (unsigned long)g_steals, (unsigned long)g_self_steals,
          (unsigned long)g_steals_active, (unsigned long)g_ev_dropped,
          g_wcount[0], g_wcount[1], g_wcount[2], g_wcount[3]);
  fflush(g_json);
  return 0;
}
