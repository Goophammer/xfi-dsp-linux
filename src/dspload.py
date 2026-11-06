#!/usr/bin/env python3
"""dspload.py - offline loader for one DSP module from CTEXFIFX.sys:
unpack .text MSB-first -> apply relocations -> emit program-RAM writes.

Relocation model (verified during development):
    type  = 0x2000 | shift[12:6] | (width-1)[5:0]
    field = bits [shift, shift+width) from the LSB of an L-word instruction starting at va
    field holds section_base + symbol.value (pre-filled), so relocating = adding a delta.
    12-bit data types: L=4, only the low 11 bits are the address (bit 11 is the neighbour field).
    0x2016: L=2, field = [0x400000 |] (label + 0x10)  ->  .text is pre-linked at PC 0x10.
    0x2590 (17-bit X/Y), 0x26A0/0x2EA0 (33-bit, 32-bit address) and bit-14 types (symbol + addend in
    the high half of the symbol index) use the same additive rule; window = instruction length.
    Verified bit for bit on 39/39 modules Windows loaded.

Provisional data bases are the ones seen in the unlinked objects. They coincide with the DSP0 I/O
window in ct20k1reg.h (word = (host - 0x100000) / 4):
    .input 0x000 = AUDIORINGIPDSP0 0x100000   .output 0x100 = AUDIORINGOPDSP0 0x100400
    .inparm/.outparm 0x200 = AUDPARARINGIODSP0 0x100800        (hypothesis)

Usage:
  python3 dspload.py CTEXFIFX.sys CrystalizerBypass                  # listing
  python3 dspload.py CTEXFIFX.sys CrystalizerBypass --pc 0x10 --out bypass.load
  python3 dspload.py CTEXFIFX.sys CrystalizerBypass --base .output=0x180   # rebase a section
  python3 dspload.py CTEXFIFX.sys CrystalizerBypass --selftest
  python3 dspload.py CTEXFIFX.sys Crystalizer --pc 0x51cf --base X=0x2f94 ... --ext _BusWrite=0x156f ...
      (any data section needs --base unless it has a provisional base; runtime calls need --ext;
       Windows' values from a captured load)
.load format: one "PC WORD" pair per line (hex), comments with '#'.
Needs modscan.py and dspdump.py in the same directory.
"""
import struct
import sys

import dspdump
import modscan as ms

PROG_HOST = 0x080000          # instruction N at host 0x080000 + 4N (verified)
PROG_END = 0x0B3FFC           # DSPMICRO_END
LINK_PC = 0x10                # .text pre-link address (0x2016 addends)
PROVISIONAL = {'.input': 0x000, '.output': 0x100, '.inparm': 0x200, '.outparm': 0x200,
               'XGP': 0x400, 'YGP': 0x600}


def decode(t):
    return (t >> 6) & 0x7F, (t & 0x3F) + 1


def get_field(words, va, L, shift, width):
    v = 0
    for w in words[va:va + L]:
        v = (v << 25) | w
    return (v >> shift) & ((1 << width) - 1)


def put_field(words, va, L, shift, width, value):
    v = 0
    for w in words[va:va + L]:
        v = (v << 25) | w
    mask = ((1 << width) - 1) << shift
    v = (v & ~mask) | ((value << shift) & mask)
    for i in range(L):
        words[va + L - 1 - i] = (v >> (25 * i)) & ms.MASK25


def ilen(word):
    """instruction length from its first word: bit 24 = 0 -> 1, bits 24..23 = 10 -> 2, 11 -> 4 (7736/7736 relocs)"""
    return 1 if not word >> 24 else (2 if not (word >> 23) & 1 else 4)


def window(t, first_word=None):
    """relocation window = length of the instruction at va; without the word: 2 for 0x2016/0x2590, else 4"""
    if first_word is not None:
        return ilen(first_word)
    return 2 if t & ~0x4000 in (0x2016, 0x2590) else 4


def load_module(data, key):
    mods = [m for m in ms.find_modules(data) if m['machine'] == 0x014C and m['optsize'] == 0]
    mod = dspdump.pick_module(data, mods, key)
    if not mod:
        sys.exit(f"no module matches {key}")
    syms = dspdump.load_symbols(data, mod)
    text = next((s for s in mod['secs'] if s['name'] == '.text'), None)
    if text is None:
        sys.exit("module has no .text")
    raw = data[mod['hdr'] + text['rptr']: mod['hdr'] + text['rptr'] + text['rsize']]
    words = ms.unpack(raw, text['vsize'], 'msb')
    relocs = []
    for k in range(text['nrel']):
        va, si, t = struct.unpack_from('<IIH', data, mod['hdr'] + text['relptr'] + ms.RELOC_SIZE * k)
        # symbol index: low 16 bits = symbol, high 16 bits = addend (types with bit 14 set, e.g. Reverb 0x734B)
        lo, hi = si & 0xFFFF, si >> 16
        sym = syms[lo] if lo < len(syms) else None
        if sym is not None and hi:
            sym = dict(sym, addend=hi)
        relocs.append((va, sym, t))
    return mod, syms, words, relocs


PER_SYMBOL = ('.input', '.output', '.inparm', '.outparm')   # Windows binds these symbol by symbol


def addr_bits(width):
    # 12- and 33-bit fields overlap the next field by one bit (its bit 0): the address is width - 1 bits
    # (verified: 0x26A0/0x2EA0 meet at bit 58)
    return width - 1 if width in (12, 33) else width


def link(mod, words, relocs, pc, bases=None, deltas=None, sym_bind=None, externals=None):
    """Relocate .text for load address pc. Returns (new words, log).

    Data fields (every type except 0x2016): the unlinked field holds provisional_base + value + addend; linking
    adds a displacement. Where it comes from, first match wins:
      sym_bind[(section, name)] = absolute address of the symbol (Windows binds .input/.output/.inparm/.outparm
                                  one by one); field = that + addend
      deltas[section]           = displacement added to the unlinked field (any section, X/Y/XY/AX/AY/XGP/YGP…)
      bases[section]            = absolute section base: displacement = base - provisional base, where the
                                  provisional base is PROVISIONAL[section] (checked) or, for other sections,
                                  derived from the first relocation into that section
      none of these             = displacement 0 (field kept)
    12-/33-bit fields change only their low 11/32 bits (the top bit is the neighbour's); 17-bit as a whole.
    0x2016 branches/calls: local label -> (bit 22 as found) | (label + pc); UNDEF -> externals[name] (required).
    """
    bases, deltas, sym_bind, externals = bases or {}, deltas or {}, sym_bind or {}, externals or {}
    out, log, prov = list(words), [], {}
    for va, sym, t in relocs:
        if sym is None:
            raise ValueError(f"va {va}: relocation {t:#06x} has no symbol")
        sn = dspdump.sec_name(mod, sym['sec'])
        add = sym.get('addend', 0)
        shift, width = decode(t)
        L = window(t, words[va])
        old = get_field(out, va, L, shift, width)
        if t & ~0x4000 == 0x2016:
            if sn == 'UNDEF':
                if sym['name'] not in externals:
                    raise ValueError(f"va {va}: call to external {sym['name']} - address not given")
                tgt = externals[sym['name']]
            else:
                if old & 0x3FFFFF != sym['value'] + LINK_PC:
                    raise ValueError(f"va {va}: branch field {old:#x} != [0x400000|]({sym['value']}+0x10)")
                tgt = sym['value'] + pc
            new = (old & 0x400000) | (tgt & 0x3FFFFF)
            put_field(out, va, L, shift, width, new)
            log.append((va, t, sym['name'], sn, old, new))
            continue
        nb = addr_bits(width)
        amask = (1 << nb) - 1
        oaddr = old & amask
        if (sn, sym['name']) in sym_bind:
            naddr = (sym_bind[(sn, sym['name'])] + add) & amask
        else:
            if sn in deltas:
                d = deltas[sn]
            elif sn in bases:
                if sn not in prov:
                    prov[sn] = PROVISIONAL[sn] if sn in PROVISIONAL else (oaddr - sym['value'] - add) & amask
                if sn in PROVISIONAL and oaddr != (PROVISIONAL[sn] + sym['value'] + add) & amask:
                    raise ValueError(f"va {va}: field {oaddr:#x} != {sn} base {PROVISIONAL[sn]:#x} + "
                                     f"{sym['value']} + {add}")
                d = bases[sn] - prov[sn]
            else:
                d = 0
            naddr = (oaddr + d) & amask
            if width == 12 and d and not (0 <= oaddr + d <= amask):
                raise ValueError(f"va {va}: address {oaddr + d:#x} does not fit 11 bits")
        new = (old & ~amask) | naddr
        put_field(out, va, L, shift, width, new)
        log.append((va, t, sym['name'], sn, oaddr, naddr))
    return out, log


def data_sections(mod, relocs):
    return {dspdump.sec_name(mod, sym['sec']) for _, sym, t in relocs
            if sym and t & ~0x4000 != 0x2016}


def externals_of(mod, relocs):
    return {sym['name'] for _, sym, t in relocs
            if sym and t & ~0x4000 == 0x2016 and dspdump.sec_name(mod, sym['sec']) == 'UNDEF'}


def selftest(mod, words, relocs):
    ext = {n: 0x123 + i for i, n in enumerate(sorted(externals_of(mod, relocs)))}
    same, _ = link(mod, words, relocs, LINK_PC, externals={n: 0 for n in ext})
    calls = [va for va, sym, t in relocs if sym and t & ~0x4000 == 0x2016 and dspdump.sec_name(mod, sym['sec']) == 'UNDEF']
    if not calls:
        assert same == words, "identity link changed the code"
    secs = data_sections(mod, relocs)
    for d in (0x40, 0x10, 0x4, -0x4):
        try:
            moved, log = link(mod, words, relocs, 0x1000, deltas={sn: d for sn in secs}, externals=ext)
            break
        except ValueError:
            continue
    else:
        raise AssertionError("no displacement fits")
    touched = set()
    for va, t, name, sn, _, new in log:
        shift, width = decode(t)
        L = window(t, words[va])
        nb = width if t & ~0x4000 == 0x2016 else addr_bits(width)
        got = get_field(moved, va, L, shift, width) & ((1 << nb) - 1)
        assert got == new & ((1 << nb) - 1), f"va {va} readback {got:#x} != {new:#x}"
        for b in range(shift, shift + nb):
            touched.add((va + L - 1 - b // 25, b % 25))
    for i, (a, b) in enumerate(zip(words, moved)):
        for bit in range(25):
            if (a ^ b) >> bit & 1 and (i, bit) not in touched:
                raise AssertionError(f"word {i} bit {bit} changed outside any reloc field")
    print(f"selftest OK: identity link unchanged{' (no externals)' if not calls else ' except external calls'}; "
          f"displacement {d:#x} / pc 0x1000 changed only reloc fields ({len(log)} relocs, sections {sorted(secs)})")


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    args = sys.argv[3:]
    opt = lambda k, d=None: args[args.index(k) + 1] if k in args else d
    pc = int(opt('--pc', hex(LINK_PC)), 16)
    bases, externals = {}, {}
    for i, a in enumerate(args):
        if a in ('--base', '--ext'):
            k, v = args[i + 1].split('=')
            (bases if a == '--base' else externals)[k] = int(v, 16)
    data = open(sys.argv[1], 'rb').read()
    mod, syms, words, relocs = load_module(data, sys.argv[2])
    if '--selftest' in args:
        selftest(mod, words, relocs)
        return
    missing = sorted(data_sections(mod, relocs) - set(PROVISIONAL) - set(bases))
    if missing:
        sys.exit(f"sections without a known base: {', '.join(missing)} - pass --base <section>=0x<addr> for each")
    try:
        linked, log = link(mod, words, relocs, pc, bases, externals=externals)
    except ValueError as e:
        sys.exit(f"link failed: {e}")
    if PROG_HOST + 4 * (pc + len(linked) - 1) > PROG_END:
        sys.exit("module would extend past DSPMICRO_END")
    entry = {s['name']: s['value'] for s in syms if s and dspdump.sec_name(mod, s['sec']) == '.text'}

    lines = [f"# {ms.progname(data, mod)} @ {mod['hdr']:#x}  pc={pc:#x}  words={len(linked)}",
             f"# entry={pc + entry.get('Entry', 0):#x}  epilog={pc + entry['Epilog']:#x}" if 'Epilog' in entry
             else f"# entry={pc + entry.get('Entry', 0):#x}",
             "# bases: " + ", ".join(f"{k}={bases.get(k, v):#05x}" for k, v in PROVISIONAL.items())]
    for va, t, name, sn, old, new in log:
        lines.append(f"# reloc va={va:3d} {t:#06x} {name:<40} {sn:<8} {old:#x} -> {new:#x}")
    for i, w in enumerate(linked):
        lines.append(f"{pc + i:04x} {w:07x}")
    text = "\n".join(lines) + "\n"
    out = opt('--out')
    if out:
        open(out, 'w').write(text)
        print(f"wrote {out}: {len(linked)} words at PC {pc:#x}..{pc + len(linked) - 1:#x} "
              f"(host {PROG_HOST + 4 * pc:#08x}..{PROG_HOST + 4 * (pc + len(linked) - 1):#08x})")
    else:
        print(text, end='')


if __name__ == '__main__':
    main()
