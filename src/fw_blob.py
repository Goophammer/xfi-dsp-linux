#!/usr/bin/env python3
"""fw_blob.py - build the X-Fi DSP firmware file for the kernel driver (ct_winmode.c) on the USER'S machine.

Input: one of our trace-free load sequences (<mode>/winmode_seq.txt in recipes/, analysis/ in the project;
fw_seq.py) + a mode description (<mode>/mode.json: playback stream + effect controls) + the
user's own copies of Creative's driver files (ha20x2k.sys, CTEXFIFX.sys; found under DRIVERDIR by SHA-256).
Output: everything the driver needs to bring the card up the way Creative's Windows driver does. It contains
Creative's DSP code, so it is generated locally and never distributed (the b43-fwcutter model); install it as
/lib/firmware/ctxfi/xfi20k1-dsp.bin.

Format v2 (little endian):
  "XFIW" u32 version=2, u32 n_ops, u32 n_controls
  stream: u32 src (playback converter), u32 map_entry (its transport map entry: dev/hi/lo at +0/+4/+8),
          u32 loop_sa (device address of the playback buffer), u32 capture converter (0 = none; its setup ops are
          appended to the sequence, its loop is the driver's recording buffer, ct_dsp_fx.c WIN_REC_VA)
  n_ops ops, then n_controls controls
  op = u32 w0 (opcode << 28 | arg), u32 w1:
    1 WRITE  reg value          2 DELAY us              3 BUFLO reg id     4 BUFHI reg id
    5 WAITZ  reg count  - wait until count words from reg read 0 (DSP idle: X 0x4FA9..)
    6 WAITEQ reg value  - wait until reg reads value (kernel command mailbox status = 2)
    7 DEVLO  reg devaddr - low 32 bits of the bus address of OUR page backing that device page (page tables)
    8 DEVHI  reg devaddr - high 32 bits
    9 CODEC  device (data << 8 | reg) - codec register over the card's I2C (DAC 0x180080, ADC 0x1a0080)
  buffer ids (BUFLO/HI): 4..19 page-table pages pt0..pt15, 20 BAR3, 21 BAR3 + 1 MB
  control = char name[44], u32 n_states, then per state: char label[16], u32 n_ops, n_ops ops
            (2 states named "off"/"on" -> an ALSA switch, otherwise an enumeration; n_states bits 16-30 = the trace's
            start state ("default"), bit 31 = slider ("slider": value = state index, up to 512 steps))
            n_ops bit 31 = a RESTORE state: the driver snapshots every register the control's other state writes
            (except the parameter engine 0x148000-0x15BFFF, which does not read back reliably) before applying it;
            this state writes the snapshot back in reverse order, then its own ops (low 31 bits = count; for the
            parameter engine: Windows' own "off" writes). For effects
            whose Windows on/off sequences re-allocate mixer operands (Game-mode CMSS-3D: on->off does not return
            654 of 918 registers to their start values), so they can be toggled any number of times.
Translation of Windows' guest addresses (any boot): page-table registers 0x13B000-0x13B07C -> pt pages; 0x80000000 /
0x80100000 (guest BAR3) -> BAR3; any other guest page written to a transport map entry (0x130020 + 32n + 8, or map A
0x130008) -> DEV of the device address written to that entry; the register just below always gets the HI part.
Delays: every trace pause >= --min-gap-us (default 200), capped at 1 s.

Usage: python3 fw_blob.py SEQ.txt MODE.json DRIVERDIR OUT.bin [--min-gap-us N]
"""
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fw_seq               # noqa: E402

OP_WRITE, OP_DELAY, OP_BUFLO, OP_BUFHI, OP_WAITZ, OP_WAITEQ, OP_DEVLO, OP_DEVHI = 1, 2, 3, 4, 5, 6, 7, 8
BAR3 = {0x80000000: 20, 0x80100000: 21}


def is_guest(v):
    return v in BAR3 or ((v >> 24) in (0x7A, 0x7B, 0x7C) and not v & 0xFFF)


def translate(rows):
    """per TRANSPORT row: ('buf', id) / ('dev', devaddr) for guest low words, the same marked 'hi' for the word below"""
    lo, hi = {}, {}
    last = {}                                       # register -> last value written (device addresses of entries)
    for i, (t, a, v, blk) in enumerate(rows):
        if blk == 'TRANSPORT':
            if is_guest(v):
                if 0x13B000 <= a <= 0x13B07C:
                    k = 0 if a == 0x13B004 else (a - 0x13B00C) // 8 + 1
                    lo[i] = ('buf', 4 + k)
                elif v in BAR3:
                    lo[i] = ('buf', BAR3[v])
                elif (0x130020 <= a < 0x13A300 and (a - 0x130020) % 32 == 8) or a in (0x130008, 0x130018):
                    lo[i] = ('dev', last.get(a - 8, 0))
                else:
                    sys.exit(f"fw_blob: guest address {v:#x} written to unexpected register {a:#x} (row {i})")
                hi[a - 4] = lo[i]
            last[a] = v
    return lo, hi


def ops_for(rows, min_gap_us=200):
    lo, hi = translate(rows)
    ops = []
    prev = rows[0][0] if rows else 0
    cur_hi = {}
    for i, (t, a, v, blk) in enumerate(rows):
        gap = int(round((t - prev) * 1e6))
        prev = t
        if gap >= min_gap_us:
            ops.append((OP_DELAY << 28, min(gap, 1000000)))
        if blk == 'WAIT':
            ops.append(((OP_WAITZ << 28) | a, v))
        elif blk == 'WAITEQ':
            ops.append(((OP_WAITEQ << 28) | a, v))
        elif i in lo:
            kind, x = lo[i]
            ops.append((((OP_BUFLO if kind == 'buf' else OP_DEVLO) << 28) | a, x))
            cur_hi[a - 4] = lo[i]
        elif blk == 'TRANSPORT' and a in hi:
            kind, x = cur_hi.get(a, hi[a])
            ops.append((((OP_BUFHI if kind == 'buf' else OP_DEVHI) << 28) | a, x))
        else:
            ops.append(((OP_WRITE << 28) | a, v & 0xFFFFFFFF))
    return ops


def pack_ops(ops):
    return b''.join(struct.pack('<II', w0, w1) for w0, w1 in ops)


def main():
    if len(sys.argv) < 5:
        sys.exit(__doc__)
    seq, modef, root, out = sys.argv[1:5]
    gap = int(sys.argv[sys.argv.index('--min-gap-us') + 1]) if '--min-gap-us' in sys.argv else 200
    mode = json.load(open(modef))
    rows = fw_seq.resolve(seq, root)
    ops = ops_for(rows, gap)
    # post_writes: fixed register values after the sequence, e.g. the master-volume parameters at 1.0 (0 dB) - the
    # trace carries the VM's Windows volume slider (Game3 capture: 30% -> parameter targets 0.2985)
    ops += [((OP_WRITE << 28) | int(a, 16), int(v, 16)) for a, v in mode.get('post_writes', [])]
    # capture: the recording setup (writes; ["devlo"/"devhi", reg, devaddr] = bus address of our page backing devaddr)
    cap = mode.get('capture')
    if cap:
        for o in cap['ops']:
            kind = {'devlo': OP_DEVLO, 'devhi': OP_DEVHI}.get(o[0])
            ops.append(((kind << 28) | int(o[1], 16), int(o[2], 16)) if kind else
                       ((OP_WRITE << 28) | int(o[0], 16), int(o[1], 16)))
    ctl_blobs = []
    for c in mode.get('controls', []):
        # count word: states | start state << 16 | slider << 31 (ct_winmode.c WM_SLIDER)
        cnt = len(c['states']) | c.get('default', 0) << 16 | (0x80000000 if c.get('slider') else 0)
        b = c['name'].encode()[:43].ljust(44, b'\0') + struct.pack('<I', cnt)
        for st in c['states']:
            sops = [((OP_WRITE << 28) | int(a, 16), int(v, 16)) for a, v in st.get('writes', [])]
            flag = 0x80000000 if st.get('restore') else 0
            b += st['label'].encode()[:15].ljust(16, b'\0') + struct.pack('<I', flag | len(sops)) + pack_ops(sops)
        ctl_blobs.append(b)
    # codec: [device, reg, data] register writes over the card's I2C, after everything else (Entertainment mode runs
    # the card at 4x - the DAC must be told; OP_CODEC = 9)
    ops += [((9 << 28) | int(d, 16), (int(v, 16) << 8) | int(r, 16)) for d, r, v in mode.get('codec', [])]
    st = mode['stream']
    with open(out, 'wb') as f:
        f.write(b'XFIW' + struct.pack('<III', 2, len(ops), len(ctl_blobs)))
        # src word: bits 16+ = channels of the stream if not stereo (8: converters src..src+7)
        src_word = st['src'] | (st['channels'] << 16 if st.get('channels', 2) != 2 else 0)
        f.write(struct.pack('<IIII', src_word, int(st['map_entry'], 16), int(st['loop_sa'], 16),
                            cap['src'] if cap else 0))
        f.write(pack_ops(ops))
        for b in ctl_blobs:
            f.write(b)
    n = {k: sum(1 for w0, _ in ops if w0 >> 28 == k) for k in range(1, 9)}
    print(f"fw_blob: {mode.get('name', modef)}: {len(ops)} ops ({n[OP_WRITE]} writes, {n[OP_BUFLO]}+{n[OP_BUFHI]} buffer, "
          f"{n[OP_DEVLO]}+{n[OP_DEVHI]} device-page, {n[OP_WAITZ]} idle waits, {n[OP_WAITEQ]} mailbox waits, "
          f"{n[OP_DELAY]} delays = {sum(w1 for w0, w1 in ops if w0 >> 28 == OP_DELAY) / 1e6:.1f} s), "
          f"{len(ctl_blobs)} controls -> {out} ({os.path.getsize(out)} bytes)")
    print(f"install: sudo install -D -m 644 {out} /lib/firmware/ctxfi/xfi20k1-dsp.bin")


if __name__ == '__main__':
    main()
