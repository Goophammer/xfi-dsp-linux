#!/bin/bash
# make_package.sh - pack this folder for another computer, WITHOUT Creative's files, firmware or build output.
set -eu
cd "$(dirname "$(readlink -f "$0")")/.."
n=xfi-dsp-linux
tar czf "$n.tar.gz" --exclude-vcs --exclude='*.sys' --exclude='*.SYS' --exclude='*.exe' --exclude='*.EXE' --exclude='*.bin' --exclude='*.o' --exclude='*.ko' \
	--exclude='*.mod' --exclude='*.mod.c' --exclude='*.cmd' --exclude='*.order' --exclude='*.symvers' \
	--exclude='__pycache__' --exclude='*.kate-swp' --exclude='*~' "$n"
if tar tzf "$n.tar.gz" | grep -qiE '\.(sys|exe|bin|ko)$'; then echo "ERROR: private files in the archive"; rm -f "$n.tar.gz"; exit 1; fi
echo "packed: $PWD/$n.tar.gz"
