#!/bin/bash
# xfi_profile.sh - switch the X-Fi's PipeWire card profile to "X-Fi DSP output + X-Fi DSP Mic" and make them the
# default output/input. Run as your normal user (no sudo), after Windows mode is loaded and the PipeWire part is
# installed (xfi_dsp_install.sh pipewire / install).
#   ./src/xfi_profile.sh            DSP output + DSP mic
#   ./src/xfi_profile.sh analog     back to the normal analog profile
set -u
CARD=$(pactl list cards 2>/dev/null | awk '/^Card #/{n=""} /Name: alsa_card/{n=$2}
	/device.vendor.id = "0x1102"/{v=1} /device.product.id = "0x0005"/{if(v&&n){print n; exit}} /^Card #/{v=0}')
[ -n "$CARD" ] || { echo "X-Fi not found in PipeWire (is the driver loaded? pactl list cards)"; exit 1; }
if [ "${1:-}" = analog ]; then
	pactl set-card-profile "$CARD" output:analog-stereo+input:analog-stereo && echo "$CARD -> analog"; exit
fi
pactl set-card-profile "$CARD" output:xfi-dsp+input:xfi-dsp-mic ||
	{ echo "profile not available - run ./src/xfi_dsp_install.sh pipewire first"; exit 1; }
sleep 0.5
# a source lost while something held the device does not come back from selecting the same profile: switch for real
if ! pactl list short sources | grep -q xfi-dsp-mic; then
	pactl set-card-profile "$CARD" off; sleep 1
	pactl set-card-profile "$CARD" output:xfi-dsp+input:xfi-dsp-mic; sleep 1
fi
SINK=$(pactl list short sinks | awk '/xfi-dsp/{print $2; exit}'); SRC=$(pactl list short sources | awk '/xfi-dsp-mic/ && !/monitor/{print $2; exit}')
[ -n "$SINK" ] && pactl set-default-sink "$SINK"; [ -n "$SRC" ] && pactl set-default-source "$SRC"
echo "$CARD -> X-Fi DSP (output: ${SINK:-?}, mic: ${SRC:-?})"
