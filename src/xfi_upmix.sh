#!/bin/bash
# xfi_upmix.sh - "Stereo Upmix" (tray: Effects): stereo upmixed to 7.1 (xfi_upmix.conf: center from L+R, rear/side from the L-R ambience,
# delayed) into hp71's 7.1 input, so the card's CMSS-3D places it around you in the headphones, like Creative's "X-Fi CMSS-3D" enhancement
# on Windows.
#   ./xfi_upmix.sh on       create the output "X-Fi DSP (stereo upmix)" and move the STEREO streams playing to it
#   ./xfi_upmix.sh off      move them back to the plain X-Fi output (the upmix output stays, unused)
#   ./xfi_upmix.sh toggle   on <-> off
#   ./xfi_upmix.sh remove   off, and remove the upmix output
#   ./xfi_upmix.sh status   on / off
# Run as your normal user, with hp71 loaded (its sink has 8 channels). A reboot clears it; the tray switches it on again.
# Music apps that open 8 channels: XFI_UPMIX_APPS="elisa|strawberry" ./xfi_upmix.sh on   (default: elisa)
set -u
XFI=$(pactl list short sinks | awk '$2 ~ /\.xfi-dsp$/ {print $2; exit}')
UP=xfi_upmix
[ -n "$XFI" ] || { echo "no X-Fi DSP output (is the DSP running?)"; exit 1; }

have_up() { pactl list short sinks | awk -v n="$UP" '$2 == n {f = 1} END {exit !f}'; }

create() {
	have_up && return
	# the upmix itself: a PipeWire filter graph (xfi_upmix.conf) run as its own small PipeWire process
	# the filter file: next to this script (package), or installed (/usr/local/bin/xfi-upmix -> /usr/local/share/xfi-dsp)
	C="$(dirname "$(readlink -f "$0")")/xfi_upmix.conf"
	[ -f "$C" ] || C=/usr/local/share/xfi-dsp/xfi_upmix.conf
	sed "s|@XFI@|$XFI|" "$C" > "${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.conf"
	pipewire -c "${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.conf" >/dev/null 2>"${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.log" &
	echo $! > "${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.pid"
	disown
	for _ in $(seq 25); do have_up && return; sleep 0.2; done
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

state() {	# on = the upmix output exists and stereo goes to it
	have_up && [ "$(cat "${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.state" 2>/dev/null)" = "$UP" ] && echo on || echo off
}

case "${1:-toggle}" in
status)	state ;;
on)	create; pactl set-default-sink "$XFI"; move_stereo "$UP"; watch_start; echo "upmix ON: stereo streams -> 7.1 -> X-Fi (surround streams untouched)" ;;
off)	watch_stop; move_stereo "$XFI"; echo "upmix OFF: stereo streams plain to the X-Fi" ;;
toggle)	if [ "$(cat "${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.state" 2>/dev/null)" = "$UP" ]; then "$0" off; else "$0" on; fi ;;
remove)	watch_stop; move_stereo "$XFI"; P=$(cat "${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.pid" 2>/dev/null); [ -n "$P" ] && kill "$P" 2>/dev/null; rm -f "${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.pid"; rm -f "${XDG_RUNTIME_DIR:-/tmp}/xfi_upmix.state"; echo "upmix output removed" ;;
*)	sed -n '2,9p' "$0"; exit 1 ;;
esac
