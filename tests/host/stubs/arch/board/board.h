/* Host-build stub for <arch/board/board.h>: GPIO reads report all
 * buttons up (pull-up high).
 */

#ifndef __STUB_ARCH_BOARD_H
#define __STUB_ARCH_BOARD_H

#include <stdint.h>
#include <stdbool.h>

static inline int board_gpio_config(uint32_t pin, int mode, bool input,
                                    bool drive, int pull)
{
  (void)pin;
  (void)mode;
  (void)input;
  (void)drive;
  (void)pull;
  return 0;
}

static inline int board_gpio_read(uint32_t pin)
{
  (void)pin;
  return 1;
}

#endif
