# SPDX-License-Identifier: BSL-1.0

include $(APPDIR)/Make.defs
include $(SDKDIR)/Make.defs

PROGNAME = $(CONFIG_SYNTHAPPS_GMSYNTH_PROGNAME)
PRIORITY = $(CONFIG_SYNTHAPPS_GMSYNTH_PRIORITY)
STACKSIZE = $(CONFIG_SYNTHAPPS_GMSYNTH_STACKSIZE)
MODULE = $(CONFIG_SYNTHAPPS_GMSYNTH)

ASRCS =
CSRCS = gm_bank.c gm_seq.c
CXXSRCS =
MAINSRC = gmsynth_main.c

CFLAGS += -O3

include $(APPDIR)/Application.mk

build_worker:
	@$(MAKE) -C worker TOPDIR="$(TOPDIR)" SDKDIR="$(SDKDIR)" APPDIR="$(APPDIR)" CROSSDEV=$(CROSSDEV)

$(OBJS): build_worker

clean:: clean_worker

clean_worker:
	@$(MAKE) -C worker TOPDIR="$(TOPDIR)" SDKDIR="$(SDKDIR)" APPDIR="$(APPDIR)" CROSSDEV=$(CROSSDEV) clean
