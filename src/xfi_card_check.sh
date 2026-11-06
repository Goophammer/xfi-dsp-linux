#!/bin/bash
# xfi_card_check.sh - say so if this PC has no tested X-Fi. Only informs: the install or test goes on either way.
# The DSP sequence was recorded on SB073x (PCI 1102:0005, subsystem 1102:0029 or 1102:0031; ctatc.c's model list).
found=
for d in /sys/bus/pci/devices/*; do
	[ "$(cat "$d/vendor" 2>/dev/null)" = 0x1102 ] && [ "$(cat "$d/device" 2>/dev/null)" = 0x0005 ] || continue
	found=1
	sv=$(cat "$d/subsystem_vendor"); sd=$(cat "$d/subsystem_device")
	[ "$sv" = 0x1102 ] && { [ "$sd" = 0x0029 ] || [ "$sd" = 0x0031 ]; } && continue
	echo "NOTE: this X-Fi (subsystem ${sv#0x}:${sd#0x}) is not the tested model, SB073x. It may not work and the mic may stay silent."
	echo "      Please report whether it works, with this subsystem number."
done
[ -n "$found" ] || echo "NOTE: no X-Fi 20K1 card (PCI 1102:0005) found in this PC. This driver needs one."
exit 0
