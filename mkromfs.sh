#!/bin/bash
# SPDX-License-Identifier: BSL-1.0
# Regenerate nsh_romfsimg.h (boot script ROMFS) after editing romfs/init.d/rcS
cd "$(dirname "$0")"
genromfs -f romfs.img -d romfs -V NSHInitVol
xxd -i romfs.img > nsh_romfsimg.h
rm -f romfs.img
