/* Host-build stub for <nuttx/audio/audio.h>: the trace harness never
 * opens an audio device, but the audio plumbing in gmsynth_main.c must
 * compile.  Only the members that file touches are declared.
 */

#ifndef __STUB_NUTTX_AUDIO_H
#define __STUB_NUTTX_AUDIO_H

#include <stdint.h>

#define AUDIO_TYPE_OUTPUT       (1)
#define AUDIO_TYPE_FEATURE      (2)
#define AUDIO_FU_VOLUME         (1)

#define AUDIOIOC_CONFIGURE      (0x1001)
#define AUDIOIOC_ENQUEUEBUFFER  (0x1002)
#define AUDIOIOC_REGISTERMQ     (0x1003)
#define AUDIOIOC_START          (0x1004)
#define AUDIOIOC_STOP           (0x1005)

#define AUDIO_MSG_DEQUEUE       (1)
#define AUDIO_MSG_UNDERRUN      (2)
#define AUDIO_MSG_COMPLETE      (3)
#define AUDIO_MSG_IOERROR       (4)

typedef int nxmutex_t;

static inline int nxmutex_init(nxmutex_t *m)
{
  *m = 0;
  return 0;
}

struct ap_buffer_s
{
  uint32_t nmaxbytes;
  uint32_t nbytes;
  uint32_t curbyte;
  uint32_t flags;
  uint8_t *samp;
  nxmutex_t lock;
};

struct audio_msg_s
{
  uint16_t msg_id;
  union
  {
    void *ptr;
    uint32_t data;
  } u;
};

struct audio_caps_s
{
  uint8_t ac_len;
  uint8_t ac_type;
  uint8_t ac_channels;
  uint8_t ac_chmap;
  union
  {
    uint16_t hw;
    uint8_t b[2];
  } ac_format;
  union
  {
    uint8_t b[4];
    uint16_t hw[2];
    uint32_t w;
  } ac_controls;
};

struct audio_caps_desc_s
{
  struct audio_caps_s caps;
};

struct audio_buf_desc_s
{
  uint32_t numbytes;
  union
  {
    struct ap_buffer_s *buffer;
    void *ptr;
  } u;
};

#endif
