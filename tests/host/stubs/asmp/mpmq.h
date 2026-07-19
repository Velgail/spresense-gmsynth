/* Host-build stub for <asmp/mpmq.h>: sends are swallowed, receives
 * time out, so code paths that talk to workers fail safely.
 */

#ifndef __STUB_ASMP_MPMQ_H
#define __STUB_ASMP_MPMQ_H

#include <stdint.h>

typedef struct
{
  int dummy;
} mpmq_t;

static inline int mpmq_init(mpmq_t *q, int key, int cpuid)
{
  (void)q;
  (void)key;
  (void)cpuid;
  return 0;
}

static inline int mpmq_send(mpmq_t *q, int id, uint32_t data)
{
  (void)q;
  (void)id;
  (void)data;
  return 0;
}

static inline int mpmq_tryreceive(mpmq_t *q, uint32_t *data)
{
  (void)q;
  (void)data;
  return -1;
}

static inline int mpmq_timedreceive(mpmq_t *q, uint32_t *data, int ms)
{
  (void)q;
  (void)data;
  (void)ms;
  return -1;
}

#endif
