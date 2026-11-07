#!/bin/bash
# xfi_dsp_install.sh - install / remove the X-Fi DSP setup so it works without terminals:
#   - our snd-ctxfi build      -> with DKMS (if installed): sources in /usr/src/xfi-dsp-<version>, rebuilt automatically
#                                  for every new kernel; /etc/depmod.d/xfi-dsp.conf makes it win over the distro module.
#                                  Without DKMS: /lib/modules/$(uname -r)/updates/ (this kernel only). XFI_NODKMS=1 forces that.
#   - the DSP firmware          -> /lib/firmware/ctxfi/xfi20k1-dsp.bin (built here from YOUR Creative driver files)
#   - autostart                 -> systemd service xfi-dsp.service (+ /usr/local/sbin/xfi-dsp-autostart): starts the DSP
#                                  in the background after boot, with a crash guard - if a start never finished (freeze),
#                                  the next boot skips it (see the script; disable: sudo touch /etc/xfi-dsp-noautostart)
#   - tray app                  -> /usr/local/bin/xfi-tray, started at login (~/.config/autostart/xfi-tray.desktop): the DSP's
#                                  controls, and your settings restored after every boot
#   - PipeWire output           -> card profile "X-Fi DSP" (ALSA device 6 through the DSP; the taskbar
#                                  volume moves the DSP's master volume after the effects, as in Windows):
#                                  ~/.local/share/xfi-dsp/xfi-dsp.conf (ACP profile set: X-Fi DSP + Analog Stereo)
#                                  ~/.local/share/xfi-dsp/xfi-dsp-output.conf (its mixer path: DSP Master)
#                                  ~/.local/share/xfi-dsp/xfi-dsp-input.conf (mic "X-Fi DSP Mic" = device 7)
#                                  ~/.config/wireplumber/wireplumber.conf.d/xfi-dsp.conf (selects it for the X-Fi)
# Tests afterwards: src/rearm.sh loads the build in src/ (insmod - ignores this install) and, if it exists, the TEST
# firmware /lib/firmware/ctxfi/xfi20k1-dsp-test.bin; the installed firmware only changes when this runs again.
# The module is built for ONE kernel: after a kernel update rebuild it (make -C /lib/modules/$(uname -r)/build
# M=$PWD/src modules) and run this again. The firmware file contains Creative's DSP code: it is generated locally
# and must not be shared.
#
#   sudo ./src/xfi_dsp_install.sh install-dkms    driver through DKMS, rebuilt for every new kernel (needs dkms)
#   sudo ./src/xfi_dsp_install.sh install-nodkms  driver for the running kernel only, survives reboots until a kernel update
#   sudo ./src/xfi_dsp_install.sh install         DKMS if it is installed, otherwise like install-nodkms
#                                                 (then reboot, or: sudo ./src/rearm.sh after the first install)
#   Mode: sudo env XFI_MODE=hp71|game|ent ./src/xfi_dsp_install.sh install...   default hp71 = 7.1 input through CMSS-3D
#   Headphone, PipeWire output 7.1; game = Game mode 2/2.1 speakers; ent = Entertainment mode 2/2.1 speakers
#   To try something without installing: ./src/xfi_test.sh
#   sudo ./src/xfi_dsp_install.sh uninstall
#   ./src/xfi_dsp_install.sh pipewire           only the PipeWire part (no root; Windows mode still by hand)
#   ./src/xfi_dsp_install.sh pipewire-off       remove only the PipeWire part
set -eu
cd "$(dirname "$0")/.."
MODE=${XFI_MODE:-hp71}
case $MODE in
hp71|game|ent) ;;
*) echo "unknown XFI_MODE '$MODE' - use hp71, game or ent"; exit 1 ;;
esac
if [ "$(id -u)" = 0 ]; then
	U=${SUDO_USER:-}
	[ -n "$U" ] || { echo "run via sudo from your user account (needs SUDO_USER for the PipeWire config)"; exit 1; }
else
	U=$(id -un)
	case "${1:-}" in pipewire|pipewire-off) ;; *) echo "run with sudo"; exit 1 ;; esac
fi
UHOME=$(getent passwd "$U" | cut -d: -f6)
KDIR=/lib/modules/$(uname -r)/updates
FW=/lib/firmware/ctxfi/xfi20k1-dsp.bin
CONF=/etc/modprobe.d/xfi-dsp.conf		# old autostart (winmode_autostart=1, before 2026-11-01) - removed
SVC=/etc/systemd/system/xfi-dsp.service
DEPMODC=/etc/depmod.d/xfi-dsp.conf
DKMS_VER=2026.11.09
AUTO=/usr/local/sbin/xfi-dsp-autostart
TRAY=/usr/local/bin/xfi-tray
PWC=$UHOME/.config/pipewire/pipewire.conf.d/xfi-dsp.conf	# old: a separate sink node (before 2026-10-30)
PSET=$UHOME/.local/share/xfi-dsp/xfi-dsp.conf
WPC=$UHOME/.config/wireplumber/wireplumber.conf.d/xfi-dsp.conf
TRAYDT=$UHOME/.config/autostart/xfi-tray.desktop
FWSRC=firmware/xfi20k1-dsp-$MODE.bin

use_dkms() { [ -z "${XFI_NODKMS:-}" ] && command -v dkms >/dev/null; }
dkms_remove() {
	for v in $(ls -d /usr/src/xfi-dsp-* 2>/dev/null | sed 's|.*/xfi-dsp-||'); do
		dkms remove -m xfi-dsp -v "$v" --all >/dev/null 2>&1 || true
		rm -rf "/usr/src/xfi-dsp-$v"
	done
}
dkms_install() {
	local d=/usr/src/xfi-dsp-$DKMS_VER
	dkms_remove
	# before DKMS looks: it moves the module it finds aside as the "original". Finding a copy from install-nodkms, it saved
	# that and left the distro's in kernel/, which then won over ours (the depmod override below does not beat it)
	rm -f "$KDIR/snd-ctxfi.ko"
	depmod -a
	mkdir -p "$d"
	cp src/*.c src/*.h src/Kbuild "$d/"
	rm -f "$d/snd-ctxfi.mod.c"
	cat > "$d/dkms.conf" <<'DKMSEOF'
PACKAGE_NAME="xfi-dsp"
PACKAGE_VERSION="@VER@"
BUILT_MODULE_NAME[0]="snd-ctxfi"
DEST_MODULE_LOCATION[0]="/updates/dkms"
MAKE[0]="make -C ${kernel_source_dir} M=${dkms_tree}/${PACKAGE_NAME}/${PACKAGE_VERSION}/build modules"
AUTOINSTALL="yes"
NO_WEAK_MODULES="yes"
DKMSEOF
	sed -i "s/@VER@/$DKMS_VER/" "$d/dkms.conf"
	# Fedora/Nobara's DKMS runs "dracut --regenerate-all --force" after every install: all initramfs images are
	# rebuilt, which takes minutes. Our module isn't in the initramfs, but this can't be skipped per module.
	if grep -qs '^post_transaction=.*dracut' /etc/dkms/framework.conf; then
		echo "   NOTE: DKMS rebuilds the initramfs of ALL kernels at the end - this can take several minutes."
		echo "   Please don't press Ctrl+C at 'Executing post-transaction command'."
	fi
	dkms add -m xfi-dsp -v "$DKMS_VER" && dkms build -m xfi-dsp -v "$DKMS_VER" && dkms install -m xfi-dsp -v "$DKMS_VER" ||
		{ echo "DKMS build/install failed - see the messages above (kernel headers installed?)"; exit 1; }
	# same module name as the distro's snd-ctxfi: tell depmod which one wins (Fedora's DKMS uses extra/, Debian's
	# updates/dkms/)
	printf 'override snd_ctxfi * updates/dkms\noverride snd_ctxfi * extra\n' > "$DEPMODC"
}
as_user() { if [ "$(id -u)" = 0 ]; then sudo -u "$U" "$@"; else "$@"; fi; }
pw_restart() {
	as_user env XDG_RUNTIME_DIR=/run/user/$(id -u "$U") systemctl --user restart wireplumber pipewire 2>/dev/null ||
		echo "(restart PipeWire yourself: systemctl --user restart wireplumber pipewire)"
}
pw_install() {
	as_user rm -f "$PWC"
	as_user mkdir -p "$(dirname "$PSET")" "$(dirname "$WPC")"
	as_user cp src/xfi-dsp-output.conf src/xfi-dsp-input.conf src/xfi-dsp-mic.conf src/xfi-dsp-wuh.conf "$(dirname "$PSET")/"
	P=$(dirname "$PSET")
	case $MODE in
	hp71) OUT_MAP=front-left,front-right,front-center,lfe,rear-left,rear-right,side-left,side-right
	      IN_PATHS="$P/xfi-dsp-mic $P/xfi-dsp-wuh" ;;
	*) OUT_MAP=left,right; IN_PATHS="$P/xfi-dsp-input" ;;
	esac
	as_user sh -c "sed 's|@PATHS_DIR@|$(dirname "$PSET")|; s|@OUT_MAP@|$OUT_MAP|; s|@IN_PATHS@|$IN_PATHS|' src/xfi-dsp-profile.conf > '$PSET'
		sed -n '/^\[Mapping analog-stereo\]/,/^#/p' /usr/share/alsa-card-profile/mixer/profile-sets/default.conf >> '$PSET'"
	as_user sh -c "sed 's|@PROFILE_SET@|$PSET|' src/wireplumber-xfi-dsp.conf > '$WPC'"
	pw_restart
	echo "PipeWire: $PSET, $WPC - pick the card profile 'X-Fi DSP' if it is not selected"
}
pw_remove() {
	as_user rm -f "$PWC" "$PSET" "$WPC" "$(dirname "$PSET")/xfi-dsp-output.conf" "$(dirname "$PSET")/xfi-dsp-input.conf" "$(dirname "$PSET")/xfi-dsp-mic.conf" "$(dirname "$PSET")/xfi-dsp-wuh.conf"
	pw_restart
}

case "${1:-}" in
install-dkms)
	command -v dkms >/dev/null || { echo "dkms is not installed - install it first, or use install-nodkms"; exit 1; }
	XFI_NODKMS=; set -- install ;;
install-nodkms)
	XFI_NODKMS=1; set -- install ;;
esac
case "${1:-}" in
install)
	./src/xfi_card_check.sh
	if ! use_dkms; then
		[ -f src/snd-ctxfi.ko ] || { echo "src/snd-ctxfi.ko missing - build it first"; exit 1; }
		[ "$(modinfo -F vermagic src/snd-ctxfi.ko | cut -d' ' -f1)" = "$(uname -r)" ] ||
			{ echo "src/snd-ctxfi.ko was built for another kernel - rebuild it for $(uname -r)"; exit 1; }
	fi
	if [ ! -f "$FWSRC" ]; then
		echo "== building the $MODE DSP firmware from your Creative driver files"
		[ -d creative ] && DRV=creative || DRV=.
		[ -d recipes ] && R=recipes || R=analysis	# the package's folder, the project's folder
		sudo -u "$U" python3 src/fw_blob.py $R/$MODE/winmode_seq.txt $R/$MODE/mode.json "$DRV" "$FWSRC"
	fi
	if use_dkms; then
		echo "== driver via DKMS (rebuilt automatically for new kernels)"
		dkms_install
	else
		echo "== driver for this kernel only (no DKMS)"
		dkms_remove; rm -f "$DEPMODC"
		install -D -m 644 src/snd-ctxfi.ko "$KDIR/snd-ctxfi.ko"
	fi
	depmod -a
	case $(modinfo -n snd_ctxfi 2>/dev/null) in
	*/kernel/sound/*)
		echo "ERROR: the distro's snd-ctxfi would still be loaded after a reboot ($(modinfo -n snd_ctxfi))."
		echo "Nothing else was installed. Please report this with: dkms status"; exit 1 ;;
	esac
	install -D -m 644 "$FWSRC" "$FW"
	echo "$MODE" > /etc/xfi-dsp-mode
	echo "$U" > /etc/xfi-dsp-user
	rm -f "$CONF"
	install -D -m 755 src/xfi-dsp-autostart.sh "$AUTO"
	install -D -m 644 src/xfi-dsp.service "$SVC"
	install -D -m 755 src/xfi_tray.py "$TRAY"
	as_user mkdir -p "$(dirname "$TRAYDT")"
	as_user cp src/xfi-tray.desktop "$TRAYDT"
	python3 -c 'import PyQt6.QtWidgets' 2>/dev/null || echo "NOTE: PyQt6 is not installed, the tray app needs it (INSTALL.md, step 2)"
	rm -f /var/lib/xfi-dsp/starting
	systemctl daemon-reload && systemctl enable xfi-dsp.service
	if use_dkms; then echo "installed: DKMS xfi-dsp/$DKMS_VER, $FW, $AUTO, $SVC"
	else echo "installed: $KDIR/snd-ctxfi.ko, $FW, $AUTO, $SVC"; fi
	pw_install
	echo "Reboot to start it."
	echo "Effects: alsamixer -c XFi (DSP Crystalizer, DSP SVM, DSP EQ, ...)"
	;;
uninstall)
	systemctl disable xfi-dsp.service 2>/dev/null || true
	command -v dkms >/dev/null && dkms_remove
	rm -f "$KDIR/snd-ctxfi.ko" "$FW" "$CONF" "$SVC" "$AUTO" "$DEPMODC" /etc/xfi-dsp-mode /etc/xfi-dsp-user "$TRAY" "$TRAYDT"
	rm -rf /var/lib/xfi-dsp
	systemctl daemon-reload
	depmod -a
	pw_remove
	echo "removed. The distro snd-ctxfi is used again after a reboot."
	;;
pipewire)
	pw_install ;;
pipewire-off)
	pw_remove; echo "PipeWire part removed" ;;
*)
	sed -n '2,/^set -eu/p' "$0" | sed '$d'; exit 1 ;;
esac
