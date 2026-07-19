############################################################################
# SPDX-License-Identifier: BSL-1.0
# synthapps/gmsynth/worker/Makefile
#
# Drive the worker library and gmvoice ELF build (based on examples/asmp).
############################################################################

-include $(TOPDIR)/.config
include $(APPDIR)/Make.defs

WORKER_ELFS = gmvoice/gmvoice

.PHONY: lib depend clean

all: $(WORKER_ELFS)

lib:
	$(Q) $(MAKE) -C lib TOPDIR="$(TOPDIR)" SDKDIR="$(SDKDIR)" APPDIR="$(APPDIR)" CROSSDEV=$(CROSSDEV)

$(WORKER_ELFS): lib
	$(Q) $(MAKE) -C $(dir $@) TOPDIR="$(TOPDIR)" SDKDIR="$(SDKDIR)" APPDIR="$(APPDIR)" CROSSDEV=$(CROSSDEV)

.depend:
	$(Q) $(MAKE) -C lib TOPDIR="$(TOPDIR)" SDKDIR="$(SDKDIR)" APPDIR="$(APPDIR)" CROSSDEV=$(CROSSDEV) depend
	@touch $@

depend: .depend

clean:
	$(Q) $(MAKE) -C gmvoice TOPDIR="$(TOPDIR)" clean
	$(Q) $(MAKE) -C lib TOPDIR="$(TOPDIR)" clean
	$(Q) rm -f .depend
