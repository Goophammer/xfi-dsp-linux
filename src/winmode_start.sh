#!/bin/bash
# winmode_start.sh - start Windows mode (the X-Fi DSP running the Crystalizer etc.) and wait until it is up.
#   sudo ./src/winmode_start.sh
# Needs our snd-ctxfi loaded (src/rearm.sh) and /lib/firmware/ctxfi/xfi20k1-dsp.bin (src/fw_blob.py).
set -u
W=/sys/kernel/xfi_dsp/winmode
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
[ -w "$W" ] || { echo "$W missing - load our snd-ctxfi first (sudo ./src/rearm.sh)"; exit 1; }
case "$(cat "$W")" in
running*) echo "Windows mode is already running."; exit 0 ;;
loading*) echo "Windows mode is already loading." ;;
*)        echo start > "$W" || { echo "could not start: $(cat "$W")"; exit 1; } ;;
esac
while :; do
	s=$(cat "$W")
	case "$s" in
	loading*)
		set -- $s; n=${2%/*}; t=${2#*/}
		printf '\rloading the DSP ... %3d%%' $(( t ? 100 * n / t : 0 )) ;;
	running*)
		printf '\rloading the DSP ... 100%%\n'
		echo "Windows mode loaded - the X-Fi DSP is running."
		exit 0 ;;
	failed*)
		printf '\n'; echo "Windows mode FAILED: ${s#failed: }"
		echo "details: sudo dmesg | grep -i 'windows mode'"
		exit 1 ;;
	*)
		printf '\n'; echo "unexpected state: $s"; exit 1 ;;
	esac
	sleep 0.5
done
