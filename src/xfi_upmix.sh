#!/bin/bash
# xfi_upmix.sh - "Stereo Envelopment" (tray: Effects, next to CMSS-3D; was "Stereo Upmix"): stereo upmixed to 7.1 (xfi_upmix.conf: center from L+R, rear/side from the L-R ambience,
# delayed) into gamehp's 7.1 input, so the card's CMSS-3D places it around you in the headphones, like Creative's "X-Fi CMSS-3D" enhancement
# on Windows. With ent51 (a 6-channel X-Fi output) the same to 5.1 for the speakers (xfi_upmix51.conf: no sides, LFE empty).
#   ./xfi_upmix.sh on       create the output "X-Fi DSP (Stereo Envelopment)" and move the STEREO streams playing to it
#   ./xfi_upmix.sh off      move them back to the plain X-Fi output (the upmix output stays, unused)
#   ./xfi_upmix.sh toggle   on <-> off
#   ./xfi_upmix.sh remove   off, and remove the upmix output
#   ./xfi_upmix.sh status   on / off
#   ./xfi_upmix.sh amount N how much: 0..100 % (50 = default; 0 = fronts only, the same as off; 5.1: fronts + center), live, remembered
#   ./xfi_upmix.sh amount   the current amount
#   ./xfi_upmix.sh mode xpand|surround   5.1 (ent51) only: Xpand (rears added) or Surround (the slider moves the sound
#                           from front to back); each keeps its own amount. ./xfi_upmix.sh mode: the current one
# Run as your normal user, with gamehp or ent51 loaded (sink with 8 or 6 channels). A reboot clears it; the tray switches it on again.
# Music apps that open 8 channels: XFI_UPMIX_APPS="elisa|strawberry" ./xfi_upmix.sh on   (default: elisa)
set -u
XFI=$(pactl list short sinks | awk '$2 ~ /\.xfi-dsp$/ {print $2; exit}')
UP=xfi_upmix
[ -n "$XFI" ] || { echo "no X-Fi DSP output (is the DSP running?)"; exit 1; }

have_up() { pactl list short sinks | awk -v n="$UP" '$2 == n {f = 1} END {exit !f}'; }
# the X-Fi output's channel count picks the filter: 6 = ent51 (5.1 speakers), else 7.1 (gamehp)
CH=$(pactl list short sinks | awk -v n="$XFI" '$2 == n {for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+ch$/) {print $i + 0; exit}}')
[ "$CH" = 6 ] && CONF=xfi_upmix51.conf || CONF=xfi_upmix.conf
GEN="${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.conf"

kill_up() {
	P=$(cat "${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.pid" 2>/dev/null)
	[ -n "$P" ] && kill "$P" 2>/dev/null
	rm -f "${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.pid"
	for _ in $(seq 25); do have_up || return 0; sleep 0.2; done
}

create() {
	# one left from the other mode (7.1 vs 5.1, after a mode switch): replace it
	if have_up && ! grep -q "^# ${CONF%.conf}\.conf " "$GEN" 2>/dev/null; then kill_up; fi
	have_up && return
	# the upmix itself: a PipeWire filter graph (xfi_upmix.conf) run as its own small PipeWire process
	# the filter file: next to this script (package), or installed (/usr/local/bin/xfi-upmix -> /usr/local/share/xfi-dsp)
	C="$(dirname "$(readlink -f "$0")")/$CONF"
	[ -f "$C" ] || C=/usr/local/share/xfi-dsp/$CONF
	sed "s|@XFI@|$XFI|" "$C" > "$GEN"
	pipewire -c "$GEN" >/dev/null 2>"${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.log" &
	echo $! > "${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.pid"
	disown
	for _ in $(seq 25); do have_up && { apply_amount; return; }; sleep 0.2; done
	echo "could not create the upmix output (log: ${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.log)"; exit 1
}

# Streams with up to 2 channels (music, videos, stereo-only games). Surround streams (a 7.1 game) are never moved: squashed
# into the 2-channel upmix input they'd lose their real positions. The default output stays the X-Fi itself, so a game
# started while the upmix is on still opens 7.1; a new stereo app starts plain until the next "on" or "toggle".
# Music players that open the output's full 8 channels for stereo music (only FL/FR carry sound) look like a 7.1 game
# by their format, so they're listed by name (application name, program, node or media name; case-insensitive, | between).
APPS=${XFI_UPMIX_APPS:-elisa}
# prints "pulse-index pipewire-id" per stream to move
stereo_inputs() {
	pactl list sink-inputs | awk -v apps="$APPS" '
		function out() { if (id != "" && !own && (ch <= 2 || app)) print id, oid }
		/^Sink Input #/ {out(); id = substr($3, 2); ch = 99; own = 0; app = 0; oid = ""}
		/Sample Specification:/ {for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+ch$/) ch = $i + 0}
		/node.name = "xfi_upmix/ {own = 1}
		/object.id = / {v = $0; sub(/^[^"]*"/, "", v); sub(/".*$/, "", v); oid = v}
		/application.name = |application.process.binary = |node.name = |media.name = / {v = $0; sub(/^[^"]*"/, "", v); sub(/".*$/, "", v)
			if (tolower(v) ~ "^(" tolower(apps) ")$") app = 1}
		END {out()}'
}

# pactl first; streams that pin themselves to an output (Elisa: node.dont-reconnect) refuse that, so then PipeWire's own
# way, the stream's target in the "default" metadata (what the desktop's sound settings do)
move() {	# move PULSE_INDEX PIPEWIRE_ID SINK_NAME
	pactl move-sink-input "$1" "$3" 2>/dev/null && return
	[ -n "$2" ] && pw-metadata "$2" target.object "$3" >/dev/null 2>&1
}

move_stereo() {
	stereo_inputs | while read -r id oid; do move "$id" "$oid" "$1"; done
	echo "$1" > "${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.state"
}

# While the upmix is on: move every NEW stereo stream too (Elisa starts a stream per track, new players start plain)
R=${XDG_RUNTIME_DIR:-/tmp}
watch_start() {
	watch_stop
	( pactl subscribe | while read -r line; do
		case "$line" in *"'new' on sink-input"*) stereo_inputs | while read -r id oid; do move "$id" "$oid" "$UP"; done ;; esac
	done ) >/dev/null 2>&1 &
	echo $! > "$R/xfi_upmix.watch"
	disown
}
watch_stop() {
	P=$(cat "$R/xfi_upmix.watch" 2>/dev/null)
	[ -n "$P" ] && { pkill -P "$P" 2>/dev/null; kill "$P" 2>/dev/null; }
	rm -f "$R/xfi_upmix.watch"
}

# The amount scales what the upmix adds: rear/side ambience (L-R)/2 x a (a = amount/50 = 0..2) and the center (L+R)/2 x min(a, 1),
# so 0 % is the fronts alone = off. 5.1 (ent51): the rears' level, the center is always there (as Windows' Xpand). Set live on the filter's controls (no restart, no gap). Remembered in ~/.config.
CONF_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/xfi-dsp
MODE_FILE=$CONF_DIR/upmix_mode
mode() { case "$(cat "$MODE_FILE" 2>/dev/null)" in surround) echo surround ;; *) echo xpand ;; esac; }
# the amount, remembered per filter: gamehp's 7.1 and ent51's Xpand and Surround each keep their own
# 0..100 % (like Windows' CMSS-3D slider), default 50 = what was 100 % of the old 0..200 % slider (2026-11-17).
# gamehp's old setting (upmix_amount, 0..200) is taken over once, halved.
if [ "$CH" = 6 ]; then AMOUNT_FILE=$CONF_DIR/upmix51_$(mode)
else
	AMOUNT_FILE=$CONF_DIR/upmix71
	if [ ! -f "$AMOUNT_FILE" ] && [ -f "$CONF_DIR/upmix_amount" ]; then
		o=$(cat "$CONF_DIR/upmix_amount"); case "$o" in ''|*[!0-9]*) ;; *) echo $(( o / 2 )) > "$AMOUNT_FILE" ;; esac
	fi
fi
MAXA=100; DEFA=50
amount() { a=$(cat "$AMOUNT_FILE" 2>/dev/null); case "$a" in ''|*[!0-9]*) echo $DEFA ;; *) echo "$a" ;; esac; }
apply_amount() {
	id=$(pw-cli ls Node | awk '/^\tid /{id = $2 + 0} /node.name = "xfi_upmix"$/{print id; exit}')
	[ -n "$id" ] || return 0
	a=$(amount)
	if [ "$CH" = 6 ] && [ "$(mode)" = surround ]; then
		# 5.1 Surround: equal-power cross-fade, fronts + center cos, plain left/right rears sin, x = a / 100
		set -- $(awk -v a="$a" 'BEGIN {x = (a > 100 ? 100 : a) / 100 * 1.5707963; printf "%.4f %.4f %.4f", cos(x), cos(x) / 2, sin(x)}')
		pw-cli set-param "$id" Props "{ params = [ \"outL:Gain 1\" $1 \"outR:Gain 1\" $1 \"center:Gain 1\" $2 \"center:Gain 2\" $2 \"rlout:Gain 1\" 0 \"rlout:Gain 2\" $3 \"rrout:Gain 1\" 0 \"rrout:Gain 2\" $3 ] }" >/dev/null 2>&1
		return 0
	fi
	if [ "$CH" = 6 ]; then	# 5.1 Xpand (xfi_upmix51.conf): rear = (0.75 own side - 0.25 other side) x a/70 (50 % = 0.71, -3 dB); fronts + center fixed
		g=$(awk -v a="$a" 'BEGIN {printf "%.4f", 0.75 * a / 70}')
		h=$(awk -v a="$a" 'BEGIN {printf "%.4f", 0.25 * a / 70}')
		pw-cli set-param "$id" Props "{ params = [ \"outL:Gain 1\" 1 \"outR:Gain 1\" 1 \"center:Gain 1\" 0.5 \"center:Gain 2\" 0.5 \"rlmix:Gain 1\" $g \"rlmix:Gain 2\" -$h \"rrmix:Gain 1\" $g \"rrmix:Gain 2\" -$h \"rlout:Gain 1\" 1 \"rlout:Gain 2\" 0 \"rrout:Gain 1\" 1 \"rrout:Gain 2\" 0 ] }" >/dev/null 2>&1
		return 0
	fi
	# 7.1 (gamehp): ambience (L-R)/2 x a/50 (0..2), center (L+R)/2 x min(a/50, 1)
	d=$(awk -v a="$a" 'BEGIN {printf "%.4f", a / 100}')
	c=$(awk -v a="$a" 'BEGIN {printf "%.4f", (a > 50 ? 50 : a) / 100}')
	pw-cli set-param "$id" Props "{ params = [ \"diff:Gain 1\" $d \"diff:Gain 2\" -$d \"center:Gain 1\" $c \"center:Gain 2\" $c ] }" >/dev/null 2>&1
}

state() {	# on = the upmix output exists and stereo goes to it
	have_up && [ "$(cat "${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.state" 2>/dev/null)" = "$UP" ] && echo on || echo off
}

case "${1:-toggle}" in
status)	state ;;
amount)	if [ -z "${2:-}" ]; then amount; exit 0; fi
	case "$2" in *[!0-9]*) echo "amount: 0..$MAXA"; exit 1 ;; esac
	[ "$2" -le "$MAXA" ] || { echo "amount: 0..$MAXA"; exit 1; }
	mkdir -p "$CONF_DIR"; echo "$2" > "$AMOUNT_FILE"; apply_amount ;;
mode)	if [ -z "${2:-}" ]; then mode; exit 0; fi
	case "$2" in xpand|surround) ;; *) echo "mode: xpand or surround"; exit 1 ;; esac
	mkdir -p "$CONF_DIR"; echo "$2" > "$MODE_FILE"
	AMOUNT_FILE=$CONF_DIR/upmix51_$2; [ "$CH" = 6 ] && apply_amount ;;
on)	create; pactl set-default-sink "$XFI"; move_stereo "$UP"; watch_start; echo "upmix ON: stereo streams -> $([ "$CH" = 6 ] && echo 5.1 || echo 7.1) -> X-Fi (surround streams untouched)" ;;
off)	watch_stop; move_stereo "$XFI"; echo "upmix OFF: stereo streams plain to the X-Fi" ;;
toggle)	if [ "$(cat "${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.state" 2>/dev/null)" = "$UP" ]; then "$0" off; else "$0" on; fi ;;
remove)	watch_stop; move_stereo "$XFI"; kill_up; rm -f "${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.state"; echo "upmix output removed" ;;
*)	sed -n '2,11p' "$0"; exit 1 ;;
esac
