#!/bin/bash
# unstick.sh - software re-arm: release DSP interrupt levels left "in service" by a halt inside an interrupt handler.
#
# 0x100EEC (per core) behaves like a per-level in-service mask: the hardware clears a level's bit when an interrupt
# handler starts and sets it again only when the core executes RETURN-FROM-INTERRUPT (0x0160000). A core halted
# mid-handler leaves its level blocked (0x0F instead of 0x1F) -> it never takes that interrupt again. A PCI PM reset
# does not clear it; a cold boot does. Used by dsp_halt.sh.
# Fix: with the core halted, point its PC at a 0x0160000 instruction (prog 0x65, the end of DSP0's SOF handler) and
# single-step it, so it really executes an interrupt return; repeat until 0xEEC reads 0x1F. Then restore PC + context.
#
# Usage: sudo src/unstick.sh            (all four cores; safe to run any time the cores are halted)
set -u
R=/sys/kernel/xfi_dsp/reg_access
[ -w "$R" ] || { echo "unstick: need write access to $R (sudo, custom module loaded)"; exit 1; }
rd() { echo "r $1" > "$R"; grep -o 'val=0x[0-9A-Fa-f]*' "$R" | cut -d= -f2; }
wr() { echo "w $1 $2" > "$R"; }
hx() { printf '0x%06X' "$1"; }
RTI=0x00160000
RTI_PC=0x65
PW=$(hx $(( 0x080000 + 4*RTI_PC )))
LEVELS=0x1F
STEP=0x00003CF1; HALT=0x00003CF0

pw_old=$(rd "$PW")
(( pw_old == RTI )) || wr "$PW" "$RTI"          # make sure the word at 0x65 is a return-from-interrupt
rc=0
for c in 0 1 2 3; do
	cb=$(( 0x100000 + c*0x2000 ))
	EEC=$(hx $(( cb + 0xEEC ))); PC=$(hx $(( cb + 0xE2C ))); DBGR=$(hx $(( cb + 0xE30 )))
	e0=$(rd "$EEC")
	if (( (e0 & LEVELS) == LEVELS )); then echo "unstick: DSP$c EEC=$e0 - no level stuck"; continue; fi
	halt_bit=$(( 0x400 << c ))
	if (( ($(rd 0x100E30) & halt_bit) == 0 )); then
		echo "unstick: DSP$c EEC=$e0 but the core is RUNNING - halt it first (skipped)"; rc=1; continue
	fi
	saved=(); for r in 0xE00 0xE08 0xE10 0xE14 0xE20 0xE28 0xE2C; do saved+=("$(rd "$(hx $(( cb + r )))")"); done
	n=0
	while (( (($(rd "$EEC")) & LEVELS) != LEVELS && n < 6 )); do
		wr "$PC" "$RTI_PC"; wr "$DBGR" "$STEP"; sleep 0.005; wr "$DBGR" "$HALT"; sleep 0.002
		n=$((n+1))
	done
	i=0; for r in 0xE00 0xE08 0xE10 0xE14 0xE20 0xE28 0xE2C; do wr "$(hx $(( cb + r )))" "${saved[$i]}"; i=$((i+1)); done
	e1=$(rd "$EEC")
	if (( (e1 & LEVELS) == LEVELS )); then
		echo "unstick: DSP$c EEC $e0 -> $e1 after $n interrupt return(s) - RELEASED"
	else
		echo "unstick: DSP$c EEC $e0 -> $e1 after $n step(s) - still stuck"; rc=1
	fi
done
(( pw_old == RTI )) || wr "$PW" "$pw_old"
exit $rc
