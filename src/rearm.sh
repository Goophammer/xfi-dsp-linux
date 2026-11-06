#!/bin/bash
# rearm.sh - (re)load our snd-ctxfi on a freshly reset card, without rebooting: stops the audio server, unloads the
# X-Fi driver in use, does a PCI Power-Management reset (D0->D3hot->D0) of the card - a plain rmmod/insmod does not
# clear the card's clock state - loads src/snd-ctxfi.ko (or KO=...), and starts the audio server again.
# Then start Windows mode with src/winmode_start.sh.
#
# Usage: sudo ./src/rearm.sh
# If the PM reset is not enough, FULL=1 ./src/rearm.sh does a PCI remove + rescan instead.
set -u
# the X-Fi 20K1 (PCI 1102:0005) - found by ID, so this works on any machine; PCI=... overrides
PCI=${PCI:-$(lspci -Dn -d 1102:0005 2>/dev/null | awk '{print $1; exit}')}
[ -n "$PCI" ] || { echo "no Creative X-Fi 20K1 (PCI 1102:0005) found"; exit 1; }
D=/sys/bus/pci/devices/$PCI
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KO=${KO:-$SELF_DIR/snd-ctxfi.ko}
DBG=0x100E30
R=/sys/kernel/xfi_dsp/reg_access

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
[ -d "$D" ] || { echo "no PCI device at $D"; exit 1; }

# Stop/start the AUDIO SERVER as the real desktop user, not root. Under sudo, `systemctl --user` targets root's
# (empty) user bus, so the invoking user's pipewire keeps /dev/snd open and the module stays "in use" - which is
# exactly what broke FULL=1. Target $SUDO_USER's session with its XDG_RUNTIME_DIR.
AUDIO_USER=${SUDO_USER:-}
AUDIO_UID=$([ -n "$AUDIO_USER" ] && id -u "$AUDIO_USER" 2>/dev/null)
user_audio() { # user_audio stop|start
	[ -n "$AUDIO_USER" ] || return 0
	sudo -u "$AUDIO_USER" XDG_RUNTIME_DIR="/run/user/$AUDIO_UID" \
		systemctl --user "$1" pipewire.socket pipewire-pulse.socket pipewire pipewire-pulse wireplumber 2>/dev/null || true
}
# (2026-10-24) the .socket units must be stopped too: with only the services stopped, socket activation respawned
# pipewire/wireplumber within a second, they reopened /dev/snd, rmmod failed and the PCI reset ran UNDER the bound driver.
# free_snd: get the module out even when the desktop pipewire is holding the card.
free_snd() { user_audio stop; sleep 0.3; fuser -k /dev/snd/* 2>/dev/null; sleep 0.2
	for _ in 1 2 3 4 5; do lsmod | grep -q '^snd_ctxfi' || return 0; rmmod snd_ctxfi 2>/dev/null && return 0; sleep 0.4; done; }

echo "== freeing the card and unloading the driver =="
free_snd
sleep 0.3
if lsmod | grep -q '^snd_ctxfi'; then
	echo "  snd_ctxfi is STILL loaded - refusing to reset the card under a bound driver. Holders:"
	fuser -v /dev/snd/* 2>&1 | sed 's/^/    /'
	user_audio start
	exit 1
fi

if [ "${FULL:-0}" = 1 ]; then
	echo "== FULL: PCI remove + rescan (re-enumerate) =="
	echo 1 > "$D/remove"; sleep 1; echo 1 > /sys/bus/pci/rescan; sleep 1
	# the rescan auto-probes snd_ctxfi AND the desktop pipewire pounces on the new card; free it again before we
	# can bind our build.
	free_snd
else
	echo "== PCI power-management reset (method: $(cat "$D/reset_method")) =="
	echo "  power_state before: $(cat "$D/power_state" 2>/dev/null)"
	echo 1 > "$D/reset" && echo "  reset OK" || echo "  reset FAILED"
	sleep 0.5
	echo "  power_state after:  $(cat "$D/power_state" 2>/dev/null)"
fi

echo "== ensuring OUR custom module is the one bound =="
# A PCI remove+rescan (FULL) makes the kernel auto-load snd_ctxfi for the re-enumerated device - which may be a
# DIFFERENT build (no reg_access). Force our module: unload whatever is loaded, then insmod ours.
if lsmod | grep -q '^snd_ctxfi'; then
	echo "  snd_ctxfi already loaded (auto-probe); unloading it first"
	free_snd
fi
if lsmod | grep -q '^snd_ctxfi'; then
	if [ "$(cat /sys/module/snd_ctxfi/srcversion 2>/dev/null)" = "$(modinfo -F srcversion "$KO")" ]; then
		echo "  the loaded snd_ctxfi IS our build (srcversion matches) - keeping it"
	else
		echo "  could not unload a DIFFERENT snd_ctxfi build (in use) - giving up"; user_audio start; exit 1
	fi
else
	# INSMOD_ARGS: module options, e.g. INSMOD_ARGS="winmode_autostart=1" (insmod ignores /etc/modprobe.d)
	# Test firmware: if /lib/firmware/ctxfi/xfi20k1-dsp-test.bin exists, the test build uses it, so the INSTALLED
	# firmware (xfi20k1-dsp.bin, loaded at boot) only changes when you run xfi_dsp_install.sh install. TESTFW=0 or
	# deleting the test file = test with the installed one.
	TESTFW_FILE=/lib/firmware/ctxfi/xfi20k1-dsp-test.bin
	if [ "${TESTFW:-1}" != 0 ] && [ -f "$TESTFW_FILE" ]; then
		echo "  using the TEST firmware $TESTFW_FILE"
		INSMOD_ARGS="winmode_fw=ctxfi/xfi20k1-dsp-test.bin ${INSMOD_ARGS:-}"
	fi
	# shellcheck disable=SC2086
	insmod "$KO" ${INSMOD_ARGS:-} || { echo "insmod failed"; user_audio start; exit 1; }
fi
sleep 0.3
user_audio start
sleep 0.3

# software re-arm: release any interrupt level a previous run left in service (src/unstick.sh)
[ -w "$R" ] && bash "$SELF_DIR/unstick.sh"

# readiness: all four cores halted (bits 0x3C00 set)
if [ -w "$R" ]; then
	echo "r $DBG" > "$R"; v=$(grep -o 'val=0x[0-9A-Fa-f]*' "$R" | cut -d= -f2)
	echo "== GLOBDSPDEBG = $v =="
	vv=$((v)); if (( (vv & 0x3C00) == 0x3C00 )); then
		echo "   cores all halted - READY. Start Windows mode now: sudo ./src/winmode_start.sh"
	else
		echo "   NOT all halted (need bits 0x3C00). Re-run this script, or fall back to a cold boot."
	fi
else
	echo "== $R not present - module may not have loaded its reg_access interface =="
fi
