#!/bin/bash
# xfi-dsp-autostart - start the X-Fi DSP ("Windows mode") at boot, with a crash guard. Run by xfi-dsp.service as
# root (installed to /usr/local/sbin by xfi_dsp_install.sh).
#
# Guard: a marker file is written (and synced to disk) before the start and removed once Windows mode reports
# "running" or "failed". If the marker is still there at the next boot, the previous start never finished - the
# system froze or was reset while the DSP was loading - so this boot skips the autostart and says so in the log.
# Retry by hand:  sudo rm /var/lib/xfi-dsp/starting, then reboot (or run src/winmode_start.sh from the driver folder).
# Disable the autostart without uninstalling:  sudo touch /etc/xfi-dsp-noautostart
set -u
MARK=/var/lib/xfi-dsp/starting
W=/sys/kernel/xfi_dsp/winmode
log() { echo "xfi-dsp: $*"; }
# The DSP's mixer controls appear with the DSP, PipeWire reads the card's controls when it starts: a session that is already up has to look again
pw_rescan() {
	local u; u=$(cat /etc/xfi-dsp-user 2>/dev/null) || return 0
	if systemctl --user -M "$u@" is-active -q wireplumber 2>/dev/null; then
		systemctl --user -M "$u@" restart wireplumber && log "WirePlumber restarted for $u"
	fi
}

[ -e /etc/xfi-dsp-noautostart ] && { log "autostart disabled (/etc/xfi-dsp-noautostart)"; exit 0; }
if [ -e "$MARK" ]; then
	log "the previous DSP start did not finish (freeze or reset during loading) - NOT starting it this boot."
	log "retry: sudo rm $MARK, then reboot (or run src/winmode_start.sh from the driver folder); details: journalctl -b -1 -k"
	exit 0
fi
for _ in $(seq 60); do [ -w "$W" ] && break; sleep 0.5; done
[ -w "$W" ] || { log "$W not there - our snd-ctxfi is not loaded (distro driver, or no X-Fi 20K1)"; exit 0; }
case "$(cat "$W")" in running*|loading*) log "already $(cat "$W")"; exit 0 ;; esac

mkdir -p "$(dirname "$MARK")" && date > "$MARK" && sync
echo start > "$W" || { rm -f "$MARK"; log "could not start: $(cat "$W")"; exit 1; }
for _ in $(seq 240); do
	s=$(cat "$W")
	case "$s" in
	running*) rm -f "$MARK"; sync; log "DSP running"; pw_rescan; exit 0 ;;
	failed*)  rm -f "$MARK"; sync; log "DSP start failed: ${s#failed: }"; exit 1 ;;
	esac
	sleep 0.5
done
rm -f "$MARK"; log "DSP did not come up within 120 s (state: $(cat "$W"))"; exit 1
