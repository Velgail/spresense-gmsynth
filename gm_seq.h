/****************************************************************************
 * SPDX-License-Identifier: BSL-1.0
 * synthapps/gmsynth/gm_seq.h
 *
 * SMF (format 0/1) streaming parser: k-way track merge with tempo map,
 * emitting events time-stamped in 48kHz output frames.  Hardened per
 * the corpus lessons: keys clamped, runaway deltas rejected, MThd
 * searched, running status tolerated across meta events.
 ****************************************************************************/

#ifndef __GM_SEQ_H
#define __GM_SEQ_H

#include <stdint.h>
#include <stdbool.h>

#include "gm_bank.h"

#define SEQ_MAX_TRACKS  (48)
#define SEQ_MAX_SECONDS (3600)

#define SEQ_EV_ON       (1)
#define SEQ_EV_OFF      (2)
#define SEQ_EV_CC       (3)
#define SEQ_EV_PROG     (4)
#define SEQ_EV_BEND     (5)

struct seq_ev_s
{
  uint32_t frame;         /* 48kHz output frame */
  uint8_t kind;
  uint8_t ch;
  uint8_t a;              /* note / cc# / prog */
  uint8_t b;              /* velocity / cc value */
  int16_t bend;           /* BEND: value - 8192 */
};

struct seq_trk_s
{
  const uint8_t *p;
  const uint8_t *end;
  uint32_t next_tick;
  uint8_t run;
  bool done;
};

struct seq_s
{
  const uint8_t *data;
  uint32_t sz;
  int ntrk;
  int div;
  struct seq_trk_s trk[SEQ_MAX_TRACKS];
  uint32_t tempo;         /* us per quarter note */
  uint32_t last_tick;
  double now_s;
};

/* Open/rewind the iterator over an in-RAM SMF image */

int gmseq_open(struct seq_s *s, const uint8_t *buf, uint32_t sz);

/* Next merged event; returns 0, or -1 at end of song */

int gmseq_next(struct seq_s *s, struct seq_ev_s *ev);

/* One full pass collecting what the loader and engine need to know */

int gmseq_prescan(const uint8_t *buf, uint32_t sz, struct prescan_s *ps);

#endif /* __GM_SEQ_H */
