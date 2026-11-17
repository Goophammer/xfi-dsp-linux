#!/bin/bash
# xfi-dsp-mode - switch the X-Fi DSP to another mode (firmware) without a reboot; the next boot starts it too.
# Installed to /usr/local/sbin/xfi-dsp-mode by xfi_dsp_install.sh, with every mode's firmware and its helpers in
# /usr/local/share/xfi-dsp. Runs as root: the tray app calls it through pkexec, and a polkit rule
# (/etc/polkit-1/rules.d/49-xfi-dsp.rules) lets the user who installed it do that without a password.
#   xfi-dsp-mode gamehp|ent|ent51
# Takes about 10 s: PipeWire stops, the card is reset, the installed driver loaded again, the DSP started in the new
# mode, then the PipeWire output set up for it (7.1, stereo or 5.1).
set -u
S=/usr/local/share/xfi-dsp
M=${1:-}
case "$M" in
gamehp|ent|ent51) ;;
*) echo "usage: xfi-dsp-mode gamehp|ent|ent51"; exit 1 ;;
esac
[ "$(id -u)" = 0 ] || { echo "run as root: pkexec xfi-dsp-mode $M"; exit 1; }
F=/lib/firmware/ctxfi/xfi20k1-dsp-$M.bin
[ -f "$F" ] || { echo "$F missing - install again (xfi_dsp_install.sh installs every mode it can build)"; exit 1; }
U=$(cat /etc/xfi-dsp-user 2>/dev/null) && [ -n "$U" ] || { echo "/etc/xfi-dsp-user missing - install again"; exit 1; }
KO=$(modinfo -n snd_ctxfi 2>/dev/null) || { echo "the installed snd-ctxfi was not found"; exit 1; }
as_user() { runuser -u "$U" -- env XDG_RUNTIME_DIR="/run/user/$(id -u "$U")" "$@"; }

# on failure: the previous mode back (firmware, /etc/xfi-dsp-mode), started again, so the next boot is not left on
# a mode that failed
OLD=$(cat /etc/xfi-dsp-mode 2>/dev/null || echo gamehp)
ERR=
start() {	# $1 = mode
	install -m 644 "/lib/firmware/ctxfi/xfi20k1-dsp-$1.bin" /lib/firmware/ctxfi/xfi20k1-dsp.bin || { ERR="installing the firmware failed"; return 1; }
	echo "$1" > /etc/xfi-dsp-mode
	echo "== $1: resetting the card and loading the driver"
	SUDO_USER=$U TESTFW=0 KO="$KO" "$S/rearm.sh" >/dev/null || { ERR="reloading the driver failed (see: journalctl -k)"; echo "$ERR"; return 1; }
	echo "== starting the DSP"
	/usr/local/sbin/xfi-dsp-autostart || { ERR="the DSP did not start (see: journalctl -k)"; echo "$ERR"; return 1; }
	echo "== PipeWire output for $1"
	( cd "$S" && as_user env XFI_MODE="$1" ./src/xfi_dsp_install.sh pipewire >/dev/null )
	sleep 2
	as_user "$S/src/xfi_profile.sh" >/dev/null || true
}
if start "$M"; then
	echo "$M is running"
	exit 0
fi
WHY=$ERR
if [ "$OLD" != "$M" ] && [ -f "/lib/firmware/ctxfi/xfi20k1-dsp-$OLD.bin" ]; then
	echo "== going back to $OLD"
	start "$OLD" && { echo "$M: $WHY - back on $OLD"; exit 1; }
fi
echo "$M: $WHY"
exit 1
