############################################################################
# SPDX-License-Identifier: BSL-1.0
# synthapps/gmsynth/worker/gmvoice/Makefile
#
# Build the Phase A spike ASMP worker (based on examples/asmp).
############################################################################

include $(APPDIR)/Make.defs
-include $(SDKDIR)/Make.defs

LIB_DIR = "../lib"

LDLIBPATH += -L $(LIB_DIR)

LDLIBS += $(ASMPWORKER_LIB)
LIB_ASMPWORKER := $(LIB_DIR)$(DELIM)libasmpw.a

BIN = gmvoice

WORKERSTACKSIZE = 8192

CSRCS = $(BIN).c

CELFFLAGS += -O3
CELFFLAGS += ${INCDIR_PREFIX}$(APPDIR)
CELFFLAGS += ${INCDIR_PREFIX}$(SDKDIR)$(DELIM)modules$(DELIM)asmp$(DELIM)worker
CELFFLAGS += ${INCDIR_PREFIX}..$(DELIM)..

AOBJS = $(ASRCS:.S=$(OBJEXT))
COBJS = $(CSRCS:.c=$(OBJEXT))

OBJEXT ?= .o

all: $(BIN)

$(COBJS): %$(OBJEXT): %.c
	@echo "CC: $<"
	$(Q) $(CC) -c $(CELFFLAGS) $< -o $@

$(AOBJS): %$(OBJEXT): %.S
	@echo "AS: $<"
	$(Q) $(CC) -c $(AFLAGS) $< -o $@

$(BIN): $(COBJS) $(AOBJS) $(LIB_ASMPWORKER)
	@echo "LD: $@"
	$(Q) $(LD) $(LDRAWELFFLAGS) $(LDLIBPATH) -o $@.debug $^ $(LDLIBS)
	$(Q) $(STRIP) -d -o $@ $@.debug

$(LIB_ASMPWORKER):

clean:
	$(call DELFILE, $(BIN))
	$(call DELFILE, $(BIN).debug)
	$(call CLEAN)
