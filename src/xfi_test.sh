#!/bin/bash
# xfi_test.sh - try the X-Fi DSP without installing it: the driver and firmware last until the next reboot, then your
# normal setup is back. Run as your normal user (it asks for sudo). Stops PipeWire briefly and closes every program
# using a sound device (see src/rearm.sh).
#   ./src/xfi_test.sh                      load hp71 (7.1 + CMSS-3D Headphone)
#   ./src/xfi_test.sh hp71|game|ent        load that mode, or switch to it
#   ./src/xfi_test.sh off                  remove what the test left behind (test firmware; PipeWire profile, or with a
#                                          permanent install put that one's profile back), then reboot
set -eu
cd "$(dirname "$(readlink -f "$0")")/.."
[ "$(id -u)" != 0 ] || { echo "run as your normal user, not with sudo"; exit 1; }
TEST=/lib/firmware/ctxfi/xfi20k1-dsp-test.bin
case "${1:-hp71}" in
hp71|game|ent) M=${1:-hp71} ;;
off)
	sudo rm -f "$TEST"
	if [ -e /etc/systemd/system/xfi-dsp.service ]; then
		# a test of another mode may have left its channel layout in the profile
		XFI_MODE=$(cat /etc/xfi-dsp-mode 2>/dev/null || echo hp71) ./src/xfi_dsp_install.sh pipewire
		echo "test firmware removed; PipeWire profile set back to the installed mode"
	else
		./src/xfi_dsp_install.sh pipewire-off
		echo "test firmware and PipeWire profile removed"
	fi
	echo "Reboot to get back to the driver that was there before."
	exit 0 ;;
*)	sed -n '2,/^set -eu/p' "$0" | sed '$d'; exit 1 ;;
esac
./src/xfi_card_check.sh
FW=firmware/xfi20k1-dsp-$M.bin
[ -f "$FW" ] || { echo "$FW missing - run ./build.sh $M first"; exit 1; }
# this loads the build in src/, not the installed driver: after a kernel update it has to be built again
if [ ! -f src/snd-ctxfi.ko ] || [ "$(modinfo -F vermagic src/snd-ctxfi.ko | cut -d' ' -f1)" != "$(uname -r)" ]; then
	KB=/lib/modules/$(uname -r)/build
	[ -d "$KB" ] || { echo "kernel headers for $(uname -r) missing (INSTALL.md, step 2)"; exit 1; }
	echo "== building the driver for kernel $(uname -r)"
	make -s -C "$KB" M="$PWD/src" clean >/dev/null 2>&1 || true
	make -s -C "$KB" M="$PWD/src" modules
fi
sudo install -D -m 644 "$FW" "$TEST"
sudo ./src/rearm.sh
sudo ./src/winmode_start.sh
XFI_MODE=$M ./src/xfi_dsp_install.sh pipewire
sleep 2
./src/xfi_profile.sh
echo "$M is running until the next reboot. Effects: alsamixer -c XFi"
