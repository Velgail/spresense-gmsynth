/****************************************************************************
 * SPDX-License-Identifier: BSL-1.0
 * synthapps/gmsynth/gm2_shared.h
 *
 * Protocol between the gmsynth supervisor (main core) and the four
 * gmvoice renderer workers (ASMP subcores).
 *
 * Topology (validated by the Phase A spike, 2026-07-19):
 *   main core : SMF sequencer, global 64-voice ledger, per-song sample
 *               loading, final mix + shared echo, 48kHz audio I/O, UI
 *   4 subcores: 16 voices each, ADPCM + linear interp + ADSR at 48kHz,
 *               reading sample data from one shared 640KB mpshm pool,
 *               writing dry L/R + echo-send L/R blocks back to it
 *
 * The workers are code-only (~10KB): small worker images keep the
 * address-converter tags free so the big pool attach works (the
 * Phase4 "large worker mpshm" failure mode).
 ****************************************************************************/

#ifndef __GM2_SHARED_H
#define __GM2_SHARED_H

#include <stdint.h>

/* MP object keys (same keys for every worker: bindings are per-task) */

#define GM2_KEY_SHM      (1)
#define GM2_KEY_MQ       (2)

#define GM2_NWORKERS     (4)
#define GM2_NVOICES      (16)      /* per worker; 16x4 = 64 total */

#define GM2_RATE         (48000)
#define GM2_BLK_FRAMES   (512)     /* 10.67ms per block */
#define GM2_NBLKS        (4)       /* ~42.7ms pipeline */

#define GM2_POOL_SIZE    (512 * 1024)
#define GM2_MAGIC        (0x474d5332)  /* "GMS2" */

/* Messages.  RENDER/DONE carry the block slot in the low byte; DONE
 * adds the render time in us (DWT cycle counter) in bits 8-31.
 */

#define GM2_MSG_RENDER   (1)
#define GM2_MSG_DONE     (2)
#define GM2_MSG_EXIT     (3)
#define GM2_MSG_HELLO    (4)   /* 0xA00xxxxx = pool VA; 0xExxxxxxx = fault */
#define GM2_MSG_CFG      (5)   /* data = worker index */
#define GM2_MSG_RESET    (6)   /* Song switch: kill voices, reset channel
                                * state, zero outputs; ack = HELLO
                                * 0xC0000000.  Processed in order, so the
                                * ack also fences all earlier RENDERs --
                                * only then may the supervisor overwrite
                                * the sample pool. */

/* Per-block worker events (supervisor -> worker).  The supervisor's
 * ledger decides everything: voice slots, stealing, envelope params;
 * the worker just executes.
 */

#define GM2_EV_NONE      (0)
#define GM2_EV_START     (1)   /* vslot: start voice with u.start */
#define GM2_EV_RELEASE   (2)   /* vslot: enter release */
#define GM2_EV_KILL      (3)   /* vslot: fast release (steal/choke) */
#define GM2_EV_CHGAIN    (4)   /* vslot = channel, u.f = (vol*exp)^2 */
#define GM2_EV_CHBEND    (5)   /* vslot = channel, u.f = pitch factor */
#define GM2_EV_ALLOFF    (6)   /* release everything */

struct gm2_start_s
{
  uint32_t adpcm_off;    /* Byte offset of ADPCM data within the pool */
  uint32_t len;          /* Samples */
  uint32_t loopstart;    /* Sample index */
  uint32_t loopend;      /* Inclusive last loop sample; 0 = one-shot */
  int16_t loop_pred;     /* Decoder state snapshot at loopstart */
  uint8_t loop_step;
  uint8_t ch;            /* MIDI channel (gain/bend lookup) */
  uint32_t inc;          /* 16.16 pitch increment at bend=1.0 */
  float amp;             /* velocity^vel_exp * region gain * trim */
  float pan_l;           /* combined channel+region pan gains */
  float pan_r;
  float send;            /* echo send level (CC91/127 at note-on) */
  float att_inc;         /* envelope: per-frame attack increment */
  float dec_k;           /* decay coefficient, 0 = no decay phase */
  float sus;             /* sustain level */
  float rel_k;           /* release coefficient */
};

struct gm2_ev_s
{
  uint16_t offset;       /* Frame offset within the block */
  uint8_t type;
  uint8_t vslot;         /* Voice slot for voice events, ch for CH* */
  union
  {
    struct gm2_start_s start;
    float f;
    uint32_t u;
  } u;
};

/* Worst dense block per worker: 16 STARTs + 16 RELEASEs + 16 KILLs +
 * 32 coalesced channel events.  48 was measured overflowing on tutti
 * sections (silently dropped notes/releases = audible dropouts).
 */

#define GM2_MAX_BLK_EVENTS (96)

/* One render block for one worker: event list in, 4-channel audio out
 * (dry L/R + echo send L/R as separate int16 planes).
 *
 * Blocks live INSIDE each worker's image (static array), because each
 * worker occupies a full 128KB tile anyway: the supervisor reads and
 * writes them through loadaddr + the offset reported in HELLO (the
 * proven Phase4 pattern).  The mpshm pool then carries ONLY the
 * per-song sample area, maximizing the sample budget.
 */

struct gm2_blk_s
{
  uint32_t nevents;
  struct gm2_ev_s events[GM2_MAX_BLK_EVENTS];
  int16_t dry[GM2_BLK_FRAMES * 2];    /* Interleaved L/R */
  int16_t send[GM2_BLK_FRAMES * 2];
};

struct gm2_blkarea_s
{
  struct gm2_blk_s blks[GM2_NBLKS];
};

struct gm2_pool_s
{
  uint32_t magic;
  uint32_t sample_off;     /* Byte offset of sample area (from pool base) */
};

#define GM2_SAMPLE_BYTES (GM2_POOL_SIZE - 64)

#endif /* __GM2_SHARED_H */
