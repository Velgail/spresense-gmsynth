/* Host-build stub for <asmp/mpshm.h>. */

#ifndef __STUB_ASMP_MPSHM_H
#define __STUB_ASMP_MPSHM_H

#include <stdint.h>
#include <stddef.h>

typedef struct
{
  int dummy;
} mpshm_t;

static inline int mpshm_init(mpshm_t *m, int key, size_t size)
{
  (void)m;
  (void)key;
  (void)size;
  return -1;
}

static inline void *mpshm_attach(mpshm_t *m, int flags)
{
  (void)m;
  (void)flags;
  return 0;
}

#endif
