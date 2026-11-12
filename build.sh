#!/bin/bash
# build.sh - build the X-Fi DSP driver and firmware on THIS computer (no root needed).
#   ./build.sh               driver (src/snd-ctxfi.ko) + the gamehp firmware (firmware/xfi20k1-dsp-gamehp.bin)
#   ./build.sh ent           driver + the firmwares named (gamehp, ent)
#   ./build.sh all           driver + both firmwares
# Needs: Creative's installer or the two Creative driver files in creative/ (see INSTALL.md), python3, make, gcc, kernel headers.
set -eu
[ "$(id -u)" != 0 ] || { echo "run ./build.sh WITHOUT sudo (as root it leaves root-owned files in this folder)"; exit 1; }
cd "$(dirname "$(readlink -f "$0")")"
MODES=()
for a in "$@"; do
	case "$a" in
	gamehp|ent) MODES+=("$a") ;;
	hp71) MODES+=(gamehp) ;;	# its old name
	all) MODES=(gamehp ent) ;;
	*) sed -n '2,6p' "$0"; exit 1 ;;
	esac
done
[ ${#MODES[@]} -gt 0 ] || MODES=(gamehp)
need() { command -v "$1" >/dev/null || { echo "missing: $1 ($2)"; exit 1; }; }
need python3 "install python3"
need make "install make (package: make / build-essential)"
need gcc "install gcc (package: gcc / build-essential)"
KB=/lib/modules/$(uname -r)/build
[ -d "$KB" ] || { echo "missing kernel headers for $(uname -r): $KB"
	echo "  Fedora/Nobara: sudo dnf install kernel-devel-$(uname -r)    Debian/Ubuntu: sudo apt install linux-headers-$(uname -r)"
	echo "  Arch: sudo pacman -S linux-headers"; exit 1; }

# Creative's installer dropped into creative/: get the two files out of it first
if [ -z "$(find creative -maxdepth 1 -iname 'ha20x2k.sys' -print -quit)" ] || [ -z "$(find creative -maxdepth 1 -iname 'CTEXFIFX.sys' -print -quit)" ]; then
	if [ -n "$(find creative -maxdepth 1 -type f -iname '*.exe' -print -quit)" ]; then
		echo "== unpacking the Creative files from the installer in creative/"
		./extract_creative.sh
	fi
fi

echo "== checking the Creative driver files in creative/"
ok=1
while read -r name size sum; do
	f=$(find creative -maxdepth 1 -iname "$name" -print -quit)
	if [ -z "$f" ]; then echo "  MISSING: creative/$name"; ok=0; continue; fi
	got=$(sha256sum "$f" | cut -d' ' -f1)
	if [ "$got" = "$sum" ]; then echo "  ok: $f"; else echo "  WRONG VERSION: $f (sha256 $got, need $sum, size $size)"; ok=0; fi
done < <(grep '^#FILE' recipes/gamehp/winmode_seq.txt | cut -d' ' -f2-)
[ $ok = 1 ] || { echo "Put the right files into creative/ (INSTALL.md, step 1) and run ./build.sh again."; exit 1; }

echo "== building the driver for kernel $(uname -r)"
make -C "$KB" M="$PWD/src" modules
[ -f src/snd-ctxfi.ko ] || { echo "driver build failed"; exit 1; }

for m in $(printf '%s\n' "${MODES[@]}" | awk '!s[$0]++'); do
	echo "== building the $m firmware from your Creative files"
	python3 src/fw_blob.py recipes/$m/winmode_seq.txt recipes/$m/mode.json creative firmware/xfi20k1-dsp-$m.bin | head -1
done
echo
echo "Done. Next: INSTALL.md step 4 (try it) or step 5 (install)."
