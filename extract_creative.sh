#!/bin/bash
# extract_creative.sh - get the two Creative files out of Creative's X-Fi driver installer, on Linux, without running it.
#   ./extract_creative.sh                 uses the .exe in creative/
#   ./extract_creative.sh path/to/file.exe
# Needs 7z (package 7zip or p7zip); bsdtar can't read this installer. Only the two files are unpacked, into creative/, and their hashes are checked.
set -eu
EXE=${1:-}
[ -z "$EXE" ] || [ -f "$EXE" ] || { echo "no such file: $EXE"; exit 1; }
[ -z "$EXE" ] || EXE=$(readlink -f "$EXE")
cd "$(dirname "$(readlink -f "$0")")"
if [ -z "$EXE" ]; then
	mapfile -t found < <(find creative -maxdepth 1 -type f -iname '*.exe')
	case ${#found[@]} in
	0) echo "Put Creative's installer (SBXF_PCDRV_L11_2_30_0012.exe) into creative/ and run this again."; exit 1 ;;
	1) EXE=$PWD/${found[0]} ;;
	*) echo "More than one .exe in creative/: leave one, or name it: ./extract_creative.sh creative/FILE.exe"; exit 1 ;;
	esac
	echo "using $EXE"
fi
DIR=Drivers/wdm/win2k_xp/amd64		# the 64-bit build: the recipes refer to these files
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
Z=$(command -v 7z || command -v 7zz || true)
[ -n "$Z" ] || { echo "install 7z first (package 7zip or p7zip)"; exit 1; }
"$Z" x -y -o"$T" "$EXE" "$DIR/ha20x2k.sys" "$DIR/CTEXFIFX.SYS" >/dev/null || true
ok=1 n=0
while read -r name size sum; do
	f=$(find "$T" -type f -iname "$name" -print -quit)
	if [ -z "$f" ]; then echo "  NOT FOUND in the installer: $name"; ok=0; continue; fi
	got=$(sha256sum "$f" | cut -d' ' -f1)
	if [ "$got" = "$sum" ]; then
		install -m 644 "$f" "creative/$name"
		echo "  ok: creative/$name"
		n=$((n + 1))
	else
		echo "  WRONG VERSION: $name (sha256 $got, need $sum)"; ok=0
	fi
done < <(grep '^#FILE' recipes/hp71/winmode_seq.txt | cut -d' ' -f2-)
[ $ok = 1 ] && [ $n = 2 ] || { echo "This installer does not have the right files: use Creative's driver version 2.30.0012 (INSTALL.md, step 1)."; exit 1; }
echo "Creative files ready (./build.sh uses them)."
