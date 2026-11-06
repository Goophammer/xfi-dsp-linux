#!/usr/bin/env python3
"""fw_seq.py - the Windows driver's DSP LOAD SEQUENCE without Creative's code in it.

A recorded Windows driver start (analysis/<mode>/winmode_full.txt, not included in the package) replays Creative's whole driver start, including ~525k writes
of DSP program/data that came from Creative's files. This rewrites every such value as a REFERENCE into the driver
files, so the sequence can be shipped and replayed with the user's own copy of the files:
    @S:k:f    = word k of catalog section S; program words: (file word & ~reloc mask) | f   (f = relocation fields)
                                             data words:    (file word + f) mod 2^32        (f = relocation delta)
Values that are not file content (fill, zeros, pointer tables, descriptors, parameters, core registers) stay
literal. The catalog (#SEC lines) names each section by file / module header offset / section index / part (XY =
interleaved X/Y pairs -> parts X and Y). Files are identified by SHA-256 (#FILE lines).

How words are matched (the load is not a static image - the kernel builds its schedule while modules are linked in):
a SHADOW of DSP memory remembers which section word each address holds. Contiguous runs of >= 8 writes are matched
against the catalog (anchored search, then every word checked); that sets the owners of the whole section range.
Shorter runs (the linker patching single instructions) are resolved through the owners. Anything else is literal.

Usage:
  python3 fw_seq.py make  analysis/winmode_full.txt analysis/winmode_seq.txt win_drivers/ha20x2k.sys src/CTEXFIFX.sys
  python3 fw_seq.py check analysis/winmode_seq.txt DRIVERDIR [--against analysis/winmode_full.txt]
Library: fw_seq.resolve(seq_path, driver_dir) -> rows (t, addr, value, block) as the firmware builder needs them
"""
import collections
import hashlib
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import modscan as ms        # noqa: E402
import prog_match as pm     # noqa: E402

DATA = {'X', 'Y', 'XY', 'AX', 'AY', 'XGP', 'YGP'}
SKIP = {'.attrib', '.input', '.output', '.inparm', '.outparm'}
FILLS = {0, 0x230000, 0xBADDADA0, 0xBADDADA1, 0xDADEEE00, 0xDAAADADA, 0xDAAADEAD}
MIN_RUN = 8


def region(a):
    """memory region of a host address, or None if not DSP memory"""
    if 0x080000 <= a < 0x0C0000:
        return 'prog'
    if a < 0x014000:
        return 'X'
    if 0x020000 <= a < 0x024000:
        return 'AX'
    if 0x040000 <= a < 0x050000:
        return 'Y'
    if 0x050000 <= a < 0x080000:
        return 'AY'
    if 0x100000 <= a < 0x108000:
        off = a & 0x1FFF
        if 0x1000 <= off < 0x1800:
            return 'XGP'
        if 0x1800 <= off < 0x2000:
            return 'YGP'
    return None


def catalog(files):
    """[(key, kind, part, words, mask_or_relset)] for every code and data section of every module"""
    cat = []
    for name, data in files.items():
        for mod in [m for m in ms.find_modules(data) if m['machine'] == 0x14C and m['optsize'] == 0]:
            for si, s in enumerate(mod['secs']):
                if s['name'] in SKIP or not s['rsize'] or not s['vsize']:
                    continue
                base = mod['hdr'] + s['rptr']
                rel_va = [struct.unpack_from('<I', data, mod['hdr'] + s['relptr'] + ms.RELOC_SIZE * k)[0]
                          for k in range(s['nrel'])]
                rel_t = [struct.unpack_from('<H', data, mod['hdr'] + s['relptr'] + ms.RELOC_SIZE * k + 8)[0]
                         for k in range(s['nrel'])]
                if s['name'] in DATA:
                    if s['rsize'] < 4 * s['vsize']:
                        continue
                    w = list(struct.unpack_from(f"<{s['vsize']}I", data, base))
                    rel = set(rel_va)
                    parts = [('X', w[0::2], {k // 2 for k in rel if k % 2 == 0}),
                             ('Y', w[1::2], {k // 2 for k in rel if k % 2 == 1})] if s['name'] == 'XY' \
                        else [(s['name'], w, rel)]
                    for part, pw, prel in parts:
                        cat.append(((name, mod['hdr'], si, part), 'data', part, pw, prel))
                else:
                    raw = data[base: base + s['rsize']]
                    n = min(s['vsize'], len(raw) * 8 // 25)
                    if n < 4:
                        continue
                    words = ms.unpack(raw, n, 'msb')
                    mask = pm.reloc_masks(n, [(va, None, t) for va, t in zip(rel_va, rel_t)])
                    cat.append(((name, mod['hdr'], si, 'code'), 'code', 'prog', words, mask))
    return cat


def word_ref(entry, k, v):
    """extra field for value v as word k of a catalog entry, or None if it doesn't fit"""
    _, kind, _, words, aux = entry
    if kind == 'code':
        m = aux[k]
        return v & m if not ((v ^ words[k]) & ~m & ms.MASK25) and not (v & ~ms.MASK25) else None
    return (v - words[k]) & 0xFFFFFFFF if (k in aux or v == words[k]) else None


def word_val(entry, k, f):
    _, kind, _, words, aux = entry
    if kind == 'code':
        return (words[k] & ~aux[k] & ms.MASK25) | f
    return (words[k] + f) & 0xFFFFFFFF


def load_rows(path):
    rows = []
    for line in open(path):
        if not line.startswith('#'):
            t, a, v, b = line.split()
            rows.append((t, int(a, 16), v, b))
    return rows


def make(full, out, paths):
    files = {os.path.basename(p): open(p, 'rb').read() for p in paths}
    cat = catalog(files)
    # anchor index per memory part: exact value -> (entry, k) for words not under relocation
    index = collections.defaultdict(lambda: collections.defaultdict(list))
    for e, (key, kind, part, words, aux) in enumerate(cat):
        for k, w in enumerate(words):
            fixed = (aux[k] == 0) if kind == 'code' else (k not in aux)
            if fixed and w not in FILLS:
                index[part][w].append((e, k))
    rows = load_rows(full)
    owner = {}                                       # (part, word address) -> (entry, k)
    out_vals = [None] * len(rows)                    # token per row (None = keep literal)
    stats = collections.Counter()
    i = 0
    while i < len(rows):
        a0 = rows[i][1]
        part = region(a0) if rows[i][3] not in ('WAIT', 'WAITEQ') else None   # handshake polls, not writes
        if part is None:
            i += 1
            continue
        j = i + 1
        while j < len(rows) and rows[j][3] not in ('WAIT', 'WAITEQ') and region(rows[j][1]) == part and \
                rows[j][1] == rows[j - 1][1] + 4:
            j += 1
        vals = [int(rows[r][2], 16) for r in range(i, j)]
        n = j - i
        gp_part = part if part in ('XGP', 'YGP') else None
        wa = [((rows[r][1] - 0x80000) // 4 if part == 'prog' else rows[r][1] & 0x1FFF if gp_part else
               (rows[r][1] - {'X': 0, 'AX': 0x20000, 'Y': 0x40000, 'AY': 0x50000}[part]) // 4, rows[r][1] >> 13)
              for r in range(i, j)]
        okey = lambda q: (part, wa[q][0], wa[q][1] if gp_part else 0)
        # walk the run: resolve through owners; where that fails, find the section that matches HERE (anchors in
        # the next 64 words), extend it as far as it fits, record it as owner of its whole range, continue.
        # (one run can span several sections, e.g. the kernel image written in one go)
        q = 0
        while q < n:
            own = owner.get(okey(q))
            if own is None or word_ref(cat[own[0]], own[1], vals[q]) is None:
                best = None
                win = range(q, min(n, q + 64))
                anchors = [p for p in win if vals[p] in index[part]]
                cnt = collections.Counter()
                for p in sorted(anchors, key=lambda p: len(index[part][vals[p]]))[:6]:
                    for e, k in index[part][vals[p]][:200]:
                        if k - (p - q) >= 0:
                            cnt[(e, k - (p - q))] += 1
                for (e, off), _ in cnt.most_common(8):
                    ln = 0
                    while q + ln < n and off + ln < len(cat[e][3]) and \
                            word_ref(cat[e], off + ln, vals[q + ln]) is not None:
                        ln += 1
                    if ln >= min(MIN_RUN, n - q) and (best is None or ln > best[2]):
                        best = (e, off, ln)
                if best:
                    e, off, ln = best
                    start_w = wa[q][0] - off
                    for k in range(len(cat[e][3])):
                        owner[(part, start_w + k, wa[q][1] if gp_part else 0)] = (e, k)
            own = owner.get(okey(q))
            f = word_ref(cat[own[0]], own[1], vals[q]) if own else None
            if f is not None:
                out_vals[i + q] = f"@{own[0]}:{own[1]}:{f:x}"
                stats[f'{part} ref'] += 1
            else:
                stats[f'{part} literal' + (' fill' if vals[q] in FILLS else '')] += 1
            q += 1
        i = j
    with open(out, 'w') as fo:
        fo.write("# fw_seq.py: Windows driver start (VM trace) with DSP code/data as references into Creative's files\n")
        for name, data in files.items():
            fo.write(f"#FILE {name} {len(data)} {hashlib.sha256(data).hexdigest()}\n")
        for e, (key, kind, part, words, aux) in enumerate(cat):
            fo.write(f"#SEC {e} {key[0]} {key[1]:#x} {key[2]} {key[3]}\n")
        for r, (t, a, v, b) in enumerate(rows):
            fo.write(f"{t} {a:#08x} {out_vals[r] or v} {b}\n")
    print(f"fw_seq make: {len(rows)} rows -> {out}")
    for k in sorted(stats):
        print(f"  {k:22s} {stats[k]:7d}")
    lit_code = stats['prog literal']
    print(f"  => program words left literal (not fill): {lit_code}")


def resolve(seq, root):
    meta_files, secs, rows = {}, {}, []
    for line in open(seq):
        if line.startswith('#FILE'):
            _, name, size, sha = line.split()
            meta_files[name] = sha
        elif line.startswith('#SEC'):
            _, e, name, hdr, si, part = line.split()
            secs[int(e)] = (name, int(hdr, 16), int(si), part)
        elif not line.startswith('#'):
            rows.append(line.split())
    files = {}
    for dirpath, _, names in os.walk(root):
        for nm in names:
            if nm in meta_files and nm not in files:
                d = open(os.path.join(dirpath, nm), 'rb').read()
                if hashlib.sha256(d).hexdigest() == meta_files[nm]:
                    files[nm] = d
    if set(meta_files) - set(files):
        sys.exit(f"fw_seq: no matching copy (SHA-256) of {sorted(set(meta_files) - set(files))} under {root}")
    cat = {c[0]: c for c in catalog(files)}
    entries = {e: cat[k] for e, k in secs.items()}
    out = []
    for t, a, v, b in rows:
        if v.startswith('@'):
            e, k, f = v[1:].split(':')
            val = word_val(entries[int(e)], int(k), int(f, 16))
        else:
            val = int(v, 16)
        out.append((float(t), int(a, 16), val, b))
    return out


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    if sys.argv[1] == 'make':
        make(sys.argv[2], sys.argv[3], sys.argv[4:])
    elif sys.argv[1] == 'check':
        rows = resolve(sys.argv[2], sys.argv[3])
        print(f"fw_seq check: {len(rows)} rows resolved against the driver files")
        if '--against' in sys.argv:
            ref = load_rows(sys.argv[sys.argv.index('--against') + 1])
            bad = [i for i, (r, x) in enumerate(zip(ref, rows)) if (r[1], int(r[2], 16)) != (x[1], x[2])]
            print("fw_seq check: " + ("EXACT - every row identical to the trace replay" if not bad and len(ref) == len(rows)
                                     else f"{len(bad)} rows differ, first at rows {bad[:5]}"))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
