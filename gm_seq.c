/****************************************************************************
 * SPDX-License-Identifier: BSL-1.0
 * synthapps/gmsynth/gm_seq.c
 *
 * SMF streaming parser (C port of the validated smf.py/corpus_scan.py
 * logic) and the prescan pass that drives per-song sample loading.
 ****************************************************************************/

#include <nuttx/config.h>
#include <stdio.h>
#include <string.h>

#include "gm_seq.h"
#include "gm2_shared.h"

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static uint32_t rd32(const uint8_t *p)
{
  return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
         ((uint32_t)p[2] << 8) | p[3];
}

static uint16_t rd16(const uint8_t *p)
{
  return ((uint16_t)p[0] << 8) | p[1];
}

static const uint8_t *find4(const uint8_t *p, const uint8_t *end,
                            const char *tag)
{
  for (; p + 4 <= end; p++)
    {
      if (p[0] == tag[0] && p[1] == tag[1] &&
          p[2] == tag[2] && p[3] == tag[3])
        {
          return p;
        }
    }

  return NULL;
}

static uint32_t read_vlq(const uint8_t **pp, const uint8_t *end)
{
  uint32_t v = 0;
  const uint8_t *p = *pp;

  while (p < end)
    {
      uint8_t b = *p++;

      v = (v << 7) | (b & 0x7f);
      if (!(b & 0x80))
        {
          break;
        }
    }

  *pp = p;
  return v;
}

static void trk_advance(struct seq_trk_s *t)
{
  if (t->p >= t->end)
    {
      t->done = true;
      return;
    }

  t->next_tick += read_vlq(&t->p, t->end);
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int gmseq_open(struct seq_s *s, const uint8_t *buf, uint32_t sz)
{
  const uint8_t *end = buf + sz;
  const uint8_t *h = find4(buf, end, "MThd");
  const uint8_t *p;
  int ntrk;
  int i;

  memset(s, 0, sizeof(*s));
  if (h == NULL || h + 14 > end)
    {
      return -1;
    }

  ntrk = rd16(h + 10);
  s->div = rd16(h + 12);
  if (s->div == 0 || (s->div & 0x8000))
    {
      return -1;
    }

  s->data = buf;
  s->sz = sz;
  s->tempo = 500000;
  s->last_tick = 0;
  s->now_s = 0.0;

  p = h + 14;
  for (i = 0; i < ntrk && i < SEQ_MAX_TRACKS; i++)
    {
      const uint8_t *m = find4(p, end, "MTrk");
      uint32_t ln;

      if (m == NULL || m + 8 > end)
        {
          break;
        }

      ln = rd32(m + 4);
      if (m + 8 + ln > end)
        {
          ln = end - (m + 8);
        }

      s->trk[i].p = m + 8;
      s->trk[i].end = m + 8 + ln;
      s->trk[i].next_tick = 0;
      s->trk[i].run = 0;
      s->trk[i].done = false;
      trk_advance(&s->trk[i]);
      p = m + 8 + ln;
      s->ntrk = i + 1;
    }

  return (s->ntrk > 0) ? 0 : -1;
}

/* gmseq_next() : Merged-stream event pump.  Consumes meta/sysex
 * internally; returns only channel events.
 */

int gmseq_next(struct seq_s *s, struct seq_ev_s *ev)
{
  for (; ; )
    {
      struct seq_trk_s *t = NULL;
      uint32_t tick = 0;
      uint8_t st;
      int i;

      for (i = 0; i < s->ntrk; i++)
        {
          if (!s->trk[i].done &&
              (t == NULL || s->trk[i].next_tick < tick))
            {
              tick = s->trk[i].next_tick;
              t = &s->trk[i];
            }
        }

      if (t == NULL)
        {
          return -1;
        }

      s->now_s += (double)(tick - s->last_tick) *
                  (double)s->tempo / (double)s->div / 1e6;
      s->last_tick = tick;
      if (s->now_s > SEQ_MAX_SECONDS)
        {
          return -1;
        }

      if (t->p >= t->end)
        {
          t->done = true;
          continue;
        }

      st = *t->p;
      if (st & 0x80)
        {
          t->p++;
          if (st < 0xf0)
            {
              t->run = st;
            }
        }
      else
        {
          st = t->run;
          if (st == 0)
            {
              t->done = true;
              continue;
            }
        }

      if (st == 0xff)
        {
          uint8_t meta;
          uint32_t ln;

          if (t->p >= t->end)
            {
              t->done = true;
              continue;
            }

          meta = *t->p++;
          ln = read_vlq(&t->p, t->end);
          if (meta == 0x51 && ln == 3 && t->p + 3 <= t->end)
            {
              s->tempo = ((uint32_t)t->p[0] << 16) |
                         ((uint32_t)t->p[1] << 8) | t->p[2];
              if (s->tempo < 10000)
                {
                  s->tempo = 500000;
                }
            }

          if (meta == 0x2f)
            {
              t->done = true;
            }

          t->p += ln;
          if (t->p > t->end)
            {
              t->done = true;
            }

          if (!t->done)
            {
              trk_advance(t);
            }

          continue;
        }
      else if (st == 0xf0 || st == 0xf7)
        {
          uint32_t ln = read_vlq(&t->p, t->end);

          t->p += ln;
          if (t->p > t->end)
            {
              t->done = true;
            }

          if (!t->done)
            {
              trk_advance(t);
            }

          continue;
        }
      else
        {
          uint8_t ch = st & 0x0f;
          uint8_t hi = st & 0xf0;
          uint8_t a = 0;
          uint8_t b = 0;

          if (hi == 0x80 || hi == 0x90 || hi == 0xa0 ||
              hi == 0xb0 || hi == 0xe0)
            {
              if (t->p + 2 > t->end)
                {
                  t->done = true;
                  continue;
                }

              a = *t->p++ & 0x7f;
              b = *t->p++ & 0x7f;
            }
          else
            {
              if (t->p >= t->end)
                {
                  t->done = true;
                  continue;
                }

              a = *t->p++ & 0x7f;
            }

          trk_advance(t);

          ev->frame = (uint32_t)(s->now_s * GM2_RATE);
          ev->ch = ch;
          ev->bend = 0;

          if (hi == 0x90 && b > 0)
            {
              ev->kind = SEQ_EV_ON;
              ev->a = a;
              ev->b = b;
              return 0;
            }
          else if (hi == 0x80 || (hi == 0x90 && b == 0))
            {
              ev->kind = SEQ_EV_OFF;
              ev->a = a;
              ev->b = 0;
              return 0;
            }
          else if (hi == 0xb0)
            {
              ev->kind = SEQ_EV_CC;
              ev->a = a;
              ev->b = b;
              return 0;
            }
          else if (hi == 0xc0)
            {
              ev->kind = SEQ_EV_PROG;
              ev->a = a;
              ev->b = 0;
              return 0;
            }
          else if (hi == 0xe0)
            {
              ev->kind = SEQ_EV_BEND;
              ev->bend = (int16_t)(((int)a | ((int)b << 7)) - 8192);
              return 0;
            }

          /* Aftertouch etc: skip */

          continue;
        }
    }
}

/* gmseq_prescan() : One pass collecting slot usage, key ranges, drum
 * keys, XG-ness and the CC111 loop point.  Mirrors the reference
 * renderer's channel/drum rules exactly.
 */

int gmseq_prescan(const uint8_t *buf, uint32_t sz, struct prescan_s *ps)
{
  struct seq_s s;
  struct seq_ev_s ev;
  uint8_t prog[16];
  uint8_t bank[16];
  bool xg = false;
  int i;

  memset(ps, 0, sizeof(*ps));
  memset(prog, 0, sizeof(prog));
  memset(bank, 0, sizeof(bank));

  /* Cheap XG sniff first: any Yamaha sysex (vendor 0x43) in the file.
   * (A byte scan can false-positive on 0x43 inside track data, so scan
   * properly during the event pass below instead -- but sysex is
   * consumed inside gmseq_next.  Detect it here with a raw pattern scan
   * for F0 43 which in practice only appears as Yamaha sysex.)
   */

  for (i = 0; i + 1 < (int)sz; i++)
    {
      if (buf[i] == 0xf0 && buf[i + 1] == 0x43)
        {
          xg = true;
          break;
        }
    }

  ps->xg = xg;

  if (gmseq_open(&s, buf, sz) < 0)
    {
      return -1;
    }

  while (gmseq_next(&s, &ev) == 0)
    {
      uint8_t ch = ev.ch;

      if (ev.kind == SEQ_EV_PROG)
        {
          prog[ch] = ev.a;
        }
      else if (ev.kind == SEQ_EV_CC)
        {
          if (ev.a == 0)
            {
              bank[ch] = ev.b;
            }
          else if (ev.a == 111 && !ps->has_loop)
            {
              ps->has_loop = true;
              ps->loop_frame = ev.frame;
            }
        }
      else if (ev.kind == SEQ_EV_ON)
        {
          bool drum = (ch == 9) ||
                      (xg && (bank[ch] == 120 || bank[ch] == 126 ||
                              bank[ch] == 127));

          if (drum)
            {
              uint8_t kit = prog[ch];

              if (kit == 48 && ev.a >= 41 && ev.a <= 53)
                {
                  /* Orchestra kit timpani -> pitched p47 */

                  if (!ps->used[47])
                    {
                      ps->used[47] = true;
                      ps->keylo[47] = ev.a;
                      ps->keyhi[47] = ev.a;
                    }

                  if (ev.a < ps->keylo[47])
                    {
                      ps->keylo[47] = ev.a;
                    }

                  if (ev.a > ps->keyhi[47])
                    {
                      ps->keyhi[47] = ev.a;
                    }

                  ps->notes[47]++;
                }
              else
                {
                  ps->used[BANK_DRUM_SLOT] = true;
                  ps->drumkey[ev.a] = 1;
                  ps->notes[BANK_DRUM_SLOT]++;
                }
            }
          else
            {
              uint8_t p = prog[ch];

              if (!ps->used[p])
                {
                  ps->used[p] = true;
                  ps->keylo[p] = ev.a;
                  ps->keyhi[p] = ev.a;
                }

              if (ev.a < ps->keylo[p])
                {
                  ps->keylo[p] = ev.a;
                }

              if (ev.a > ps->keyhi[p])
                {
                  ps->keyhi[p] = ev.a;
                }

              ps->notes[p]++;
            }
        }
    }

  ps->total_frames = (uint32_t)(s.now_s * GM2_RATE);
  return 0;
}
