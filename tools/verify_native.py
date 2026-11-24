#!/usr/bin/env python3
"""verify_native.py <private dir> <input dir> <output dir> [show]

Re-opens every changed container of a run and checks its structure against the input:
OLE (same streams and sizes), ZIP (CRC test, same member count), TAR/GZIP (readable),
newer SolidWorks (every chunk still inflates to its recorded size). With show > 0 prints
a few replaced fragments per kind (private review only). Exit 1 on any failure.
"""
import collections, gzip, os, struct, sys, tarfile, zipfile, zlib

MARK = b"\x14\x00\x06\x00\x08\x00"
CRC_STATS = None  # set to a Counter to count chunk checksums while walking output files

def sw_chunks(data):
    out, pos = [], data.find(MARK)
    while pos >= 0:
        nxt = pos + 6
        if pos + 26 <= len(data):
            csize, usize, nlen = struct.unpack_from("<III", data, pos + 14)
            start = pos + 26 + nlen
            if nlen < 512 and 0 < csize and start + csize <= len(data):
                try:
                    raw = zlib.decompressobj(-15).decompress(data[start:start + csize])
                    if len(raw) == usize and CRC_STATS is not None:
                        CRC_STATS["ok" if zlib.crc32(raw) & 0xFFFFFFFF == struct.unpack_from("<I", data, pos + 10)[0] else "stale"] += 1
                    if len(raw) == usize:
                        name = bytes(((b << 4) | (b >> 4)) & 0xFF for b in data[pos + 26:start]).decode("latin-1")
                        out.append((name, raw))
                        nxt = start + csize
                except zlib.error:
                    pass
        pos = data.find(MARK, nxt)
    return out

def fragments(a, b, limit):
    out, i = [], 0
    while i < len(a) and len(out) < limit:
        if a[i] != b[i]:
            j = i
            while j < len(a) and a[j:j + 8] != b[j:j + 8]:
                j += 1
            lo, hi = max(0, i - 30), min(len(a), j + 14)
            show = lambda x: x[lo:hi].replace(b"\x00", b"").decode("latin-1")
            out.append("%r -> %r" % (show(a), show(b)))
            i = j
        i += 1
    return out

def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    priv, inp, out = sys.argv[1:4]
    show = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    import olefile
    back = dict(l.rstrip("\n").split("\t")[::-1] for l in open(os.path.join(priv, "pathmap.tsv"), encoding="utf-8", errors="surrogateescape"))
    bad, counts, shown = [], collections.Counter(), collections.Counter()
    crc_out = collections.Counter()
    for line in open(os.path.join(priv, "results.tsv"), encoding="utf-8", errors="surrogateescape"):
        rel, action, n = line.rstrip("\n").split("\t")
        if n == "0" or action not in ("ole", "zip", "tar", "gzip", "solidworks", "binary", "compress"):
            continue
        src, dst = os.path.join(inp, back[rel]), os.path.join(out, rel)
        counts[action] += 1
        try:
            if action == "ole":
                a, b = olefile.OleFileIO(src), olefile.OleFileIO(dst)
                la, lb = a.listdir(), b.listdir()
                if la != lb:
                    bad.append("ole stream list differs: " + rel)
                for e in la:
                    da, db = a.openstream(e).read(), b.openstream(e).read()
                    if len(da) != len(db):
                        bad.append("ole stream size differs: " + rel)
                    elif da != db and shown[action] < show:
                        for frag in fragments(da, db, 2):
                            print("[ole %s] %s" % ("/".join(e)[-34:].encode("ascii", "replace").decode(), frag[:230]))
                            shown[action] += 1
                a.close(); b.close()
            elif action == "zip":
                with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst) as b:
                    if b.testzip() is not None or len(a.namelist()) != len(b.namelist()):
                        bad.append("zip broken: " + rel)
                    if shown[action] < show:
                        diff = [(x, y) for x, y in zip(a.namelist(), b.namelist()) if x != y][:2]
                        for x, y in diff:
                            print("[zip name] %r -> %r" % (x[-90:], y[-90:])); shown[action] += 1
                        for x, y in zip(a.infolist(), b.infolist()):
                            if x.file_size == y.file_size and x.CRC != y.CRC and x.file_size < 5_000_000 and shown[action] < show:
                                for frag in fragments(a.read(x), b.read(y), 2):
                                    print("[zip %s] %s" % (y.filename[-30:], frag[:230])); shown[action] += 1
            elif action == "tar":
                with tarfile.open(src) as a, tarfile.open(dst) as b:
                    if len(a.getnames()) != len(b.getnames()):
                        bad.append("tar member count differs: " + rel)
            elif action == "gzip":
                with gzip.open(dst) as g:
                    while g.read(1 << 20):
                        pass
            elif action == "solidworks":
                da, db = open(src, "rb").read(), open(dst, "rb").read()
                global CRC_STATS
                ca = sw_chunks(da)
                CRC_STATS = crc_out
                cb = sw_chunks(db)
                CRC_STATS = None
                if len(da) != len(db) or [(n, len(r)) for n, r in ca] != [(n, len(r)) for n, r in cb]:
                    bad.append("solidworks chunks differ: " + rel)
                for (name, ra), (_, rb) in zip(ca, cb):
                    if ra != rb and shown[action] < show:
                        for frag in fragments(ra, rb, 2):
                            print("[sw %s] %s" % (name[-34:], frag[:230])); shown[action] += 1
            elif action == "binary" and shown[action] < show:
                da, db = open(src, "rb").read(), open(dst, "rb").read()
                for frag in fragments(da, db, 2):
                    print("[binary %s] %s" % (os.path.splitext(rel)[1], frag[:230])); shown[action] += 1
        except Exception as exc:
            bad.append("%s check failed (%s: %s): %s" % (action, type(exc).__name__, str(exc)[:80], rel))
    if crc_out["stale"] > crc_out["ok"] // 1000 + 5:
        bad.append("solidworks chunk checksums: %d stale of %d" % (crc_out["stale"], sum(crc_out.values())))
    print("checked", dict(counts), "sw chunk checksums", dict(crc_out), "problems", len(bad))
    for b in bad[:15]:
        print("  " + b[:260])
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
