#!/usr/bin/env python3
"""modscan.py v2 - census of Creative DSP COFF modules in CTEXFIFX.sys + reloc/symbol probing.

v2 changes:
  - relocation records are 12 bytes (verified by stride); each is printed raw + split into fields
  - .text unpacked MSB-first (verified); LSB shown only as a control
  - symbol-table entry size is auto-probed (standard 18 B gave truncated names)
  - unnamed modules show the first strings in their .attrib; orphan ProgNames show the nearest header

Usage:
  python3 modscan.py CTEXFIFX.sys
  python3 modscan.py CTEXFIFX.sys --relocs --only CrystalizerBypass
"""
import bisect
import re
import struct
import sys
from collections import Counter, defaultdict

SEC_FMT = '<8sIIIIIIHHI'   # COFF section header, 40 bytes
FH_FMT = '<HHIIIHH'        # COFF file header, 20 bytes
RELOC_SIZE = 12            # verified; standard i386 COFF is 10
MASK25 = (1 << 25) - 1
PROG_RE = re.compile(rb'@EA\.ProgName\(([^)\0]{1,128})\)')
STR_RE = re.compile(rb'[\x20-\x7e]{5,}')


def cstr(b):
    return b.split(b'\0', 1)[0].decode('latin1')


def parse_sections(data, hdr, nsec, table):
    secs = []
    for i in range(nsec):
        o = table + 40 * i
        if o + 40 > len(data):
            return None
        name, vsize, vaddr, rsize, rptr, relptr, lnptr, nrel, nln, ch = struct.unpack_from(SEC_FMT, data, o)
        n = cstr(name)
        if not n or not all(32 < ord(c) < 127 for c in n):
            return None
        if rsize and (rptr > 0x200000 or hdr + rptr + rsize > len(data)):
            return None
        if nrel and (relptr > 0x200000 or hdr + relptr + RELOC_SIZE * nrel > len(data)):
            return None
        secs.append(dict(name=n, vsize=vsize, vaddr=vaddr, rsize=rsize, rptr=rptr,
                         relptr=relptr, nrel=nrel, ch=ch))
    return secs


def find_modules(data):
    found = {}
    for m in re.finditer(re.escape(b'.attrib\0'), data):
        off = m.start()
        for i in range(64):                      # v3: was 8; modules with many delay-line sections were missed
            table = off - 40 * i
            lo = max(table - 20 - 512, -1)
            for hdr in range(table - 20, lo, -1):
                if hdr < 0:
                    break
                machine, nsec, ts, symptr, nsyms, optsize, flags = struct.unpack_from(FH_FMT, data, hdr)
                if hdr + 20 + optsize != table or not (1 <= nsec <= 64) or i >= nsec:
                    continue
                secs = parse_sections(data, hdr, nsec, table)
                if not secs or secs[i]['name'] != '.attrib':
                    continue
                if symptr and symptr > 0x200000:
                    continue
                found.setdefault(hdr, dict(hdr=hdr, machine=machine, ts=ts, symptr=symptr,
                                           nsyms=nsyms, optsize=optsize, flags=flags, secs=secs))
    return [found[k] for k in sorted(found)]


def attrib_range(mod):
    for s in mod['secs']:
        if s['name'] == '.attrib' and s['rsize']:
            a = mod['hdr'] + s['rptr']
            return a, a + s['rsize']
    return None


def progname(data, mod):
    r = attrib_range(mod)
    if r:
        m = PROG_RE.search(data, r[0], r[1])
        if m:
            return m.group(1).decode('latin1')
    return '?'


def attrib_strings(data, mod, k=4):
    r = attrib_range(mod)
    if not r:
        return []
    return [s.decode('latin1')[:40] for s in STR_RE.findall(data[r[0]:r[1]])[:k]]


def unpack(raw, n, order):
    if order == 'lsb':
        big = int.from_bytes(raw, 'little')
        return [(big >> (25 * i)) & MASK25 for i in range(n)]
    big, total = int.from_bytes(raw, 'big'), len(raw) * 8
    return [(big >> (total - 25 * (i + 1))) & MASK25 for i in range(n)]


def probe_symbols(data, mod):
    """COFF string table normally starts with its own u32 length right after the symbols.
    For each candidate entry size, show that u32 and how printable the following bytes are."""
    if not mod['symptr'] or not mod['nsyms']:
        print("  symbols: none")
        return
    base = mod['hdr'] + mod['symptr']
    print(f"  symbols: nsyms={mod['nsyms']} at {base:08x}")
    for size in (18, 20, 22, 24, 26, 28, 32, 36, 40):
        st = base + size * mod['nsyms']
        if st + 40 > len(data):
            continue
        ln = struct.unpack_from('<I', data, st)[0]
        tail = data[st + 4: st + 36]
        printable = sum(32 <= b < 127 for b in tail)
        mark = '  <-- plausible' if 4 <= ln < 0x10000 and printable >= 24 else ''
        print(f"    entry={size:2d}B  strtab@{st:08x} len_u32={ln:#010x} "
              f"printable={printable:2d}/32  {tail[:24]!r}{mark}")
    print("    first 64 bytes of symbol table:")
    for o in range(0, 64, 16):
        print(f"      {base + o:08x}: {data[base + o: base + o + 16].hex(' ')}")


def reloc_report(data, mod):
    for s in mod['secs']:
        if s['name'] != '.text':
            continue
        raw = data[mod['hdr'] + s['rptr']: mod['hdr'] + s['rptr'] + s['rsize']]
        n = s['vsize']
        if not (0 < n and n * 25 <= len(raw) * 8):
            print(f"  .text vsize={n} raw={len(raw)}: does not fit 25-bit packing, skipped")
            continue
        msb, lsb = unpack(raw, n, 'msb'), unpack(raw, n, 'lsb')
        print(f"  .text vsize={n} raw={len(raw)} B  nrel={s['nrel']}  (record = {RELOC_SIZE} B)")
        print(f"    {'raw record':<36} {'va':>5}  {'+4 u32':>10} {'+8':>6} {'+10':>6}  msb-word  lsb-word")
        in_range, ascending, prev = 0, True, -1
        by_kind = defaultdict(Counter)
        for r in range(s['nrel']):
            o = mod['hdr'] + s['relptr'] + RELOC_SIZE * r
            rec = data[o:o + RELOC_SIZE]
            va, f4, f8, f10 = struct.unpack_from('<IIHH', rec)
            ok = va < n
            in_range += ok
            ascending &= va >= prev
            prev = va
            line = f"    {rec.hex(' '):<36} {va:5d}  {f4:#010x} {f8:#06x} {f10:#06x}"
            if ok:
                line += f"  {msb[va]:07x}   {lsb[va]:07x}"
                by_kind[f8][msb[va] >> 16] += 1
            else:
                line += "  (va out of range)"
            print(line)
        print(f"  va in range: {in_range}/{s['nrel']}   ascending: {ascending}")
        print("  msb top-9-bit field per +8 value:")
        for k, c in sorted(by_kind.items()):
            print(f"    +8={k:#06x}: " + ", ".join(f"{op:#05x}x{cnt}" for op, cnt in c.most_common()))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    args = sys.argv[2:]
    only = args[args.index('--only') + 1] if '--only' in args else None
    data = open(sys.argv[1], 'rb').read()

    all_names = [(m.start(), m.group(1).decode('latin1')) for m in PROG_RE.finditer(data)]
    mods = find_modules(data)
    real = [m for m in mods if m['machine'] == 0x014C and m['optsize'] == 0]
    covered = [attrib_range(m) for m in real if attrib_range(m)]

    print(f"{'header':>8}  machine  timestamp  nsec  sections  ->  ProgName")
    for m in mods:
        names = ' '.join(s['name'] for s in m['secs'])
        pn = progname(data, m)
        flag = '' if m in real else '   [not counted: machine/optsize]'
        hint = f"   attrib strings: {attrib_strings(data, m)}" if pn == '?' and m in real else ''
        print(f"{m['hdr']:08x}  0x{m['machine']:04x}   {m['ts']:08x}   {len(m['secs']):3}  "
              f"{names}  ->  {pn}{flag}{hint}")

    print(f"\nReal modules: {len(real)}   (all hits: {len(mods)})   ProgName strings in file: {len(all_names)}")
    print(f"Unnamed real modules: {sum(progname(data, m) == '?' for m in real)}")

    hdrs = [m['hdr'] for m in real]
    orphans = [(o, n) for o, n in all_names if not any(a <= o < b for a, b in covered)]
    print(f"ProgNames outside any .attrib: {len(orphans)}")
    for o, n in orphans:
        i = bisect.bisect_right(hdrs, o) - 1
        near = f"nearest preceding header {real[i]['hdr']:08x} ({progname(data, real[i])}), " \
               f"+{o - real[i]['hdr']:#x}" if i >= 0 else "no preceding header"
        print(f"  {o:08x}  {n:<24} {near}")

    if '--relocs' in args:
        for m in real:
            pn = progname(data, m)
            if only and only.lower() not in pn.lower():
                continue
            print(f"\n== {pn} @ {m['hdr']:08x}")
            reloc_report(data, m)
            probe_symbols(data, m)


if __name__ == '__main__':
    main()
