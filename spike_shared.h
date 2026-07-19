/****************************************************************************
 * SPDX-License-Identifier: BSL-1.0
 * synthapps/gmsynth/spike_shared.h
 *
 * Phase A spike: validate the architecture prerequisites for the
 * 64-voice GM engine before building it.
 *
 * Questions this spike answers on real hardware:
 *   1. Can a SMALL worker (code only, ~1 tile) attach a LARGE mpshm
 *      (640KB = 10 address-converter tags)?  Phase4 showed attach
 *      silently failing for large worker images; the worker VA space
 *      is only 16 x 64KB tags, so small-image + big-shm should fit.
 *   2. Do THREE workers attach the same shm and see identical bytes?
 *      (checksum vs supervisor-computed value)
 *   3. What does one rendered block cost when voices fetch ADPCM from
 *      shared memory instead of the worker image, and how much does it
 *      degrade when 3 cores render concurrently (RAM bus contention)?
 *   4. Raw sequential read bandwidth from the shm, solo vs concurrent
 *      (matters for per-song sample loading and any streaming).
 ****************************************************************************/

#ifndef __SPIKE_SHARED_H
#define __SPIKE_SHARED_H

#include <stdint.h>

/* MP object keys (same key works across workers: bindings are
 * per-task, each worker looks up only objects bound to itself).
 */

#define SPIKE_KEY_SHM     (1)
#define SPIKE_KEY_MQ      (2)

#define SPIKE_NWORKERS    (4)

/* 640KB pool = 5 tiles = 10 worker address-converter tags */

#define SPIKE_POOL_SIZE   (640 * 1024)

#define SPIKE_MAGIC       (0x53504b31)  /* "SPK1" */

/* Render benchmark geometry: identical to the planned engine */

#define SPIKE_RATE        (32000)
#define SPIKE_BLK_FRAMES  (512)         /* 16ms per block */
#define SPIKE_MAX_VOICES  (24)

/* Messages.  Replies carry a 24-bit payload in bits 8-31 where noted;
 * one reply per command keeps the ICC queue far from overflow.
 *
 *  HELLO    worker->sup   0xA00xxxxx = attach OK, xxxxx = worker VA
 *                         0xF1000000|err = mpshm_init failed
 *                         0xF2000000     = mpshm_attach returned NULL
 *                         0xF3000000|w   = magic mismatch (w = low bits read)
 *                         0xE0000000|pc  = fault handler (crashed)
 *  CFG      sup->worker   data = worker index (selects output slot)
 *  CSUM     sup->worker   reply CSUM_R: additive checksum of sample area,
 *                         folded to 24 bits
 *  RENDER   sup->worker   data = nvoices | (nblocks << 8) | (mode << 16)
 *                         mode 0: pitch increments for 32kHz output
 *                         mode 1: increments scaled x2/3 (48kHz output:
 *                                 same source consumption, 1.5x frames/s)
 *                         reply DONE: average render us per block
 *  MEMRD    sup->worker   data = KB to read sequentially
 *                         reply MEMRD_R: elapsed us
 *  EXIT     sup->worker   terminate
 */

#define SPIKE_MSG_HELLO   (1)
#define SPIKE_MSG_CFG     (2)
#define SPIKE_MSG_CSUM    (3)
#define SPIKE_MSG_CSUM_R  (4)
#define SPIKE_MSG_RENDER  (5)
#define SPIKE_MSG_DONE    (6)
#define SPIKE_MSG_MEMRD   (7)
#define SPIKE_MSG_MEMRD_R (8)
#define SPIKE_MSG_EXIT    (9)

struct spike_pool_s
{
  uint32_t magic;
  uint32_t sample_bytes;              /* Valid bytes in samples[] */
  int16_t out[SPIKE_NWORKERS][SPIKE_BLK_FRAMES * 2];
  uint8_t samples[];                  /* ADPCM area to end of pool */
};

#define SPIKE_SAMPLE_BYTES \
  (SPIKE_POOL_SIZE - sizeof(struct spike_pool_s))

#endif /* __SPIKE_SHARED_H */
