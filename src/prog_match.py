#!/usr/bin/env python3
"""prog_match.py - find CTEXFIFX DSP modules inside a program-RAM capture and read back how Windows linked them.

For every module: unpack .text (MSB-first), build a bit mask of every relocation field, then look for positions
in the capture where all un-relocated bits match. For each placement, every relocation field is read from the
capture and compared with the unlinked addend:
    data types: real address = field, provisional = base + symbol.value  -> real base = field - symbol.value
    0x2016:     real target  = low 22 bits                               -> load PC = target - label
Usage:
  python3 prog_match.py CTEXFIFX.sys ../capture_crash/prog_080000.bin            # placements + linked bases
  python3 prog_match.py CTEXFIFX.sys ../capture_crash/prog_080000.bin -v         # + every reloc field
Needs modscan.py, dspdump.py, dspload.py in the same directory.
"""
import struct
import sys
from collections import Counter, defaultdict

import dspdump
import dspload as dl
import modscan as ms

L_OF = {0x2016: 2, 0x2590: 2}           # verified window lengths; everything else 4


def window(t):
    return L_OF.get(t, 4)


def reloc_masks(n, relocs):
    """per-word 25-bit masks of relocated bits"""
    m = [0] * n
    for va, sym, t in relocs:
        shift, width = dl.decode(t)
        L = window(t)
        for b in range(shift, shift + width):
            w = va + L - 1 - b // 25
            if 0 <= w < n:
                m[w] |= 1 << (b % 25)
    return m


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    verbose = '-v' in sys.argv
    data = open(sys.argv[1], 'rb').read()
    raw = open(sys.argv[2], 'rb').read()
    prog = [x & ms.MASK25 for x in struct.unpack(f'<{len(raw) // 4}I', raw)]
    index = defaultdict(list)
    for i, x in enumerate(prog):
        index[x].append(i)
    mods = [m for m in ms.find_modules(data) if m['machine'] == 0x014C and m['optsize'] == 0]

    placements = []
    for m in mods:
        try:
            mod, syms, words, relocs = dl.load_module(data, hex(m['hdr']))
        except SystemExit:
            continue
        n = len(words)
        if n < 8:
            continue
        mask = reloc_masks(n, relocs)
        # anchor: the rarest fully un-relocated non-trivial word in the capture
        cands = [k for k in range(n) if mask[k] == 0 and words[k] not in (0, 0x230000)]
        if not cands:
            continue
        k0 = min(cands, key=lambda k: len(index.get(words[k], ())))
        for p in index.get(words[k0], ()):
            base = p - k0
            if base < 0 or base + n > len(prog):
                continue
            if all((prog[base + k] ^ words[k]) & ~mask[k] & ms.MASK25 == 0 for k in range(n)):
                placements.append((base, n, ms.progname(data, m), m['hdr'], mod, syms, words, relocs))

    placements.sort()
    print(f"{len(placements)} placements found")
    print(f"{'PC':>6} {'words':>6}  module")
    covered = 0
    bases = defaultdict(Counter)          # (type, section) -> Counter(real base)
    loadpc = Counter()
    for base, n, name, hdr, mod, syms, words, relocs in placements:
        covered += n
        lp = Counter()
        for va, sym, t in relocs:
            if sym is None:
                continue
            shift, width = dl.decode(t)
            L = window(t)
            field = dl.get_field(prog, base + va, L, shift, width)
            sn = dspdump.sec_name(mod, sym['sec'])
            if t == 0x2016:
                if sn == 'UNDEF':
                    key = f"call {sym['name']}"
                    bases[(t, key)][field & 0x3FFFFF] += 1
                else:
                    lp[(field & 0x3FFFFF) - sym['value']] += 1
            else:
                real = field & 0x7FF if width == 12 else field
                bases[(t, sn)][real - sym['value']] += 1
            if verbose:
                print(f"         va {va:5d} {t:#06x} {sym['name'][:36]:<36} {sn:<8} val={sym['value']:<5} field={field:#x}")
        lps = ', '.join(f"{k:#x}" for k, _ in lp.most_common(2))
        print(f"{base:#06x} {n:6d}  {name} @{hdr:#x}   branch-load-PC: {lps or '-'}")
        loadpc[base - (lp.most_common(1)[0][0] if lp else base)] += 1
    print(f"\ncovered {covered} words; branch load PC == placement PC for {loadpc.get(0, 0)}/{len(placements)}")
    print("\nlinked bases per (reloc type, section): real field - symbol value")
    for (t, sn), c in sorted(bases.items(), key=lambda x: (str(x[0][1]), x[0][0])):
        print(f"  {t:#06x} {sn:<28} " + ', '.join(f"{k:#x}x{v}" for k, v in c.most_common(8)))


if __name__ == '__main__':
    main()
