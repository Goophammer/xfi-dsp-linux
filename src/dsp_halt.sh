#!/bin/bash
# dsp_halt.sh - SAFE HALT of all four DSP cores: mask every core's interrupts and stop the timers, let any running
# handler return, then STEP + HALT each core via its own debug register (0x10nE30), then release interrupt levels left
# in service (unstick.sh). Stops a running Windows mode immediately (the PCI reset in rearm.sh does not stop the
# cores). Usage:  sudo ./src/dsp_halt.sh
set -u
R=/sys/kernel/xfi_dsp/reg_access
[ -w "$R" ] || { echo "dsp_halt: $R not writable (sudo; our snd-ctxfi loaded?)"; exit 1; }
rd() { echo "r $1" > "$R"; grep -o 'val=0x[0-9A-Fa-f]*' "$R" | cut -d= -f2; }
wr() { echo "w $1 $2" > "$R"; }
for cb in 0x100000 0x102000 0x104000 0x106000; do
	wr "$(printf '0x%06X' $(( cb + 0xEE4 )))" 0x00000000      # interrupt mask
	wr "$(printf '0x%06X' $(( cb + 0xD60 )))" 0x00000000      # timer 0 control
	wr "$(printf '0x%06X' $(( cb + 0xD68 )))" 0x00000000      # timer 1 control
done
sleep 0.05
E=$(( ($(rd 0x100E30) & 0xFFFF) | 0x3CF0 ))
STEP=$(printf '0x%08X' $(( E | 1 ))); HALT=$(printf '0x%08X' $(( E | 0x10 )))
for dbg in 0x102E30 0x104E30 0x106E30 0x100E30; do
	wr "$dbg" "$STEP"; sleep 0.005; wr "$dbg" "$HALT"; sleep 0.005
done
sleep 0.02
for n in 0 1 2 3; do
	pc=$(printf '0x%06X' $(( 0x100E2C + n * 0x2000 )))
	a=$(rd "$pc"); sleep 0.005; b=$(rd "$pc")
	echo "dsp_halt: DSP$n PC $a $( [ "$a" = "$b" ] && echo halted || echo 'STILL RUNNING' )"
done
bash "$(dirname "$0")/unstick.sh"
v=$(rd 0x100E30)
echo "dsp_halt: GLOBDSPDEBG = $v ($( (( ($v & 0x3C00) == 0x3C00 )) && echo 'all four cores halted' || echo 'NOT all halted' ))"
