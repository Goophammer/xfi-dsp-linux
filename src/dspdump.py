#!/usr/bin/env python3
"""dspdump.py - dump one DSP module from CTEXFIFX.sys: sections, symbols, and .text as MSB-first
25-bit words with relocations annotated on the word they patch.

Needs modscan.py in the same directory.

Usage:
  python3 dspdump.py CTEXFIFX.sys CrystalizerBypass       # by exact ProgName (first match)
  python3 dspdump.py CTEXFIFX.sys 0x11a310                # by header offset
  python3 dspdump.py CTEXFIFX.sys CrystalizerBypass
  python3 dspdump.py ha20x2k.sys uK1 General,s0ISR              # other code sections (default: .text)
"""
import struct
import sys

import modscan as ms

SYM_SIZE = 20   # verified (modscan v2)


def load_symbols(data, mod):
    if not mod['symptr'] or not mod['nsyms']:
        return []
    base = mod['hdr'] + mod['symptr']
    strtab = base + SYM_SIZE * mod['nsyms']
    syms, i = [], 0
    while i < mod['nsyms']:
        e = data[base + SYM_SIZE * i: base + SYM_SIZE * (i + 1)]
        if e[:4] == b'\0\0\0\0':
            so = struct.unpack_from('<I', e, 4)[0]
            name = ms.cstr(data[strtab + so: strtab + so + 256])
        else:
            name = ms.cstr(e[:8])
        value, sec = struct.unpack_from('<Ii', e, 8)
        typ, sclass, naux = struct.unpack_from('<HBB', e, 16)
        syms.append(dict(name=name, value=value, sec=sec, typ=typ, sclass=sclass, naux=naux))
        syms.extend([None] * naux)
        i += 1 + naux
    return syms


def sec_name(mod, sec):
    if 1 <= sec <= len(mod['secs']):
        return mod['secs'][sec - 1]['name']
    return {0: 'UNDEF', -1: 'ABS', -2: 'DEBUG'}.get(sec, f'sec{sec}')


def describe(mod, sym):
    if sym is None:
        return '(aux)'
    return f"{sym['name']} ({sec_name(mod, sym['sec'])}+{sym['value']})"


def pick_module(data, mods, key):
    if key.lower().startswith('0x'):
        off = int(key, 16)
        return next((m for m in mods if m['hdr'] == off), None)
    hits = [m for m in mods if ms.progname(data, m) == key]
    if len(hits) > 1:
        print(f"# {len(hits)} modules named {key}: " + ", ".join(f"{m['hdr']:#x}" for m in hits)
              + " - using the first; pass an offset to choose")
    return hits[0] if hits else None


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    data = open(sys.argv[1], 'rb').read()
    mods = [m for m in ms.find_modules(data) if m['machine'] == 0x014C and m['optsize'] == 0]
    mod = pick_module(data, mods, sys.argv[2])
    if not mod:
        sys.exit(f"no module matches {sys.argv[2]}")
    hdr = mod['hdr']
    print(f"# {ms.progname(data, mod)} @ {hdr:#x}  timestamp {mod['ts']:#x}")

    print("\n## .attrib")
    r = ms.attrib_range(mod)
    if r:
        for s in ms.STR_RE.findall(data[r[0]:r[1]]):
            print("  " + s.decode('latin1'))

    print("\n## sections")
    for i, s in enumerate(mod['secs'], 1):
        print(f"  {i:2d} {s['name']:<10} vsize={s['vsize']:6d} raw={s['rsize']:6d} "
              f"nrel={s['nrel']:4d} flags={s['ch']:#010x}")

    syms = load_symbols(data, mod)
    print(f"\n## symbols ({len(syms)})")
    for i, sym in enumerate(syms):
        if sym:
            print(f"  [{i:3d}] {sym['name']:<44} {sec_name(mod, sym['sec']):<9} value={sym['value']:<6} "
                  f"type={sym['typ']:#06x} sclass={sym['sclass']}")

    want = sys.argv[3].split(',') if len(sys.argv) > 3 else ['.text']   # code sections to print, e.g. General,s0ISR
    for s in mod['secs']:
        if s['name'] not in want:
            continue
        raw = data[hdr + s['rptr']: hdr + s['rptr'] + s['rsize']]
        n = s['vsize']
        words = ms.unpack(raw, n, 'msb')
        relocs = {}
        for k in range(s['nrel']):
            va, si, typ = struct.unpack_from('<IIH', data, hdr + s['relptr'] + ms.RELOC_SIZE * k)
            relocs.setdefault(va, []).append((typ, syms[si] if si < len(syms) else None))
        print(f"\n## {s['name']} ({n} words, MSB-first)   bits shown MSB->LSB, split 9|16")
        for i, w in enumerate(words):
            b = format(w, '025b')
            line = f"  {i:5d}  {w:07x}  {b[:9]} {b[9:]}"
            for typ, sym in relocs.get(i, []):
                line += f"   <{typ:#06x} {describe(mod, sym)}>"
            print(line)
        pad = len(raw) * 8 - n * 25
        tail = int.from_bytes(raw, 'big') & ((1 << pad) - 1) if pad else 0
        print(f"  # {pad} padding bits after last word, value {tail:#x}")


if __name__ == '__main__':
    main()
