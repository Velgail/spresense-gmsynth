/* Host-build stub for <asmp/mptask.h>: the harness never boots
 * workers; every call fails so any accidental use is loud.
 */

#ifndef __STUB_ASMP_MPTASK_H
#define __STUB_ASMP_MPTASK_H

#include <stdint.h>

typedef struct
{
  uintptr_t loadaddr;
} mptask_t;

static inline int mptask_init(mptask_t *t, const char *path)
{
  (void)t;
  (void)path;
  return -1;
}

static inline int mptask_assign(mptask_t *t)
{
  (void)t;
  return -1;
}

static inline int mptask_getcpuid(mptask_t *t)
{
  (void)t;
  return 0;
}

static inline int mptask_bindobj(mptask_t *t, void *obj)
{
  (void)t;
  (void)obj;
  return -1;
}

static inline int mptask_exec(mptask_t *t)
{
  (void)t;
  return -1;
}

#endif
