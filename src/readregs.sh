#!/bin/bash
# readregs.sh - read X-Fi registers through our driver (values go to the kernel log as "xfi_dsp: read ...").
# Usage: sudo ./readregs.sh 0x1c6060 0x1c5420 ...   (no arguments: the clock/rate set used for Entertainment mode)
R=/sys/kernel/xfi_dsp/reg_access
[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"
[ $# -gt 0 ] || set -- 0x1c6060 0x1c6070 0x1c5420 0x1c5480 0x1b012c 0x1bdc00 0x1bdc04 0x1bdd00 0x1b9d00 0x1b9d04 \
	0x1b1e00 0x1b1e04 0x1b1f00 0x1b1f04 0x1c6060
for r in "$@"; do echo "r $r" > "$R"; cat "$R"; done
