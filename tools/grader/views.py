"""Container walker for the local grader (from the corpus auditor's walk.py): yields
(location, kind, bytes) views of a file, recursing into zip/tar/gz/Z/7z/rar, OLE streams and
property sets, newer-SolidWorks deflate chunks, PDF Info/XMP and attachments. Read-only."""
import gzip
import io
import os
import struct
import subprocess
import tarfile
import tempfile
import zipfile
import zlib

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
SW_MARK = b"\x14\x00\x06\x00\x08\x00"
SW_EXT = set(".sldprt .sldasm .slddrw .slddrt .sldlfp .prtdot .asmdot .drwdot".split())
MAX_MEMBER = 400 << 20
MAX_DEPTH = 5


def sw_chunks(data):
    out, pos = [], data.find(SW_MARK)
    while pos >= 0:
        nxt = pos + 6
        if pos + 26 <= len(data):
            csize, usize, nlen = struct.unpack_from("<III", data, pos + 14)
            start = pos + 26 + nlen
            if nlen < 512 and 0 < csize and start + csize <= len(data) and usize < (1 << 31):
                try:
                    raw = zlib.decompressobj(-15).decompress(data[start:start + csize])
                except zlib.error:
                    raw = None
                if raw is not None and len(raw) == usize:
                    name = bytes(((b << 4) | (b >> 4)) & 0xFF for b in data[pos + 26:start]).decode("latin-1")
                    out.append((name, raw))
                    nxt = start + csize
        pos = data.find(SW_MARK, nxt)
    return out


def propsets(buf):
    """All string values of an OLE property-set stream: list of (fmtid_hex, pid_or_name, value)."""
    out = []
    try:
        nsets = struct.unpack_from("<I", buf, 24)[0]
        for k in range(min(nsets, 8)):
            fmtid = bytes(buf[28 + 20 * k:44 + 20 * k]).hex()
            base = struct.unpack_from("<I", buf, 44 + 20 * k)[0]
            nprops = struct.unpack_from("<I", buf, base + 4)[0]
            if nprops > 5000:
                continue
            index = [struct.unpack_from("<II", buf, base + 8 + 8 * j) for j in range(nprops)]
            codepage = 1252
            for pid, off in index:
                if pid == 1:
                    vt = struct.unpack_from("<H", buf, base + off)[0]
                    if vt == 2:
                        codepage = struct.unpack_from("<H", buf, base + off + 4)[0]
            names = {}
            for pid, off in index:
                if pid != 0:
                    continue
                pos = base + off
                count = struct.unpack_from("<I", buf, pos)[0]
                pos += 4
                for _ in range(min(count, 5000)):
                    cid, length = struct.unpack_from("<II", buf, pos)
                    if codepage == 1200:
                        name = bytes(buf[pos + 8:pos + 8 + 2 * length]).decode("utf-16le", "replace")
                        pos += 8 + 2 * length + (-2 * length) % 4
                    else:
                        name = bytes(buf[pos + 8:pos + 8 + length]).decode("latin-1")
                        pos += 8 + length
                    names[cid] = name.rstrip("\x00")
            for pid, off in index:
                if pid in (0, 1):
                    continue
                pos = base + off
                vt = struct.unpack_from("<I", buf, pos)[0] & 0xFFFF
                vals = []
                if vt in (30, 31):
                    vals.append((vt, pos + 4))
                elif vt in (0x101E, 0x101F):
                    n = struct.unpack_from("<I", buf, pos + 4)[0]
                    p = pos + 8
                    for _ in range(min(n, 500)):
                        cnt = struct.unpack_from("<I", buf, p)[0]
                        vals.append((vt & 0xFF, p))
                        p += 4 + (cnt if vt == 0x101E else 2 * cnt)
                        p += (-p) % 4
                for t, p in vals:
                    cnt = struct.unpack_from("<I", buf, p)[0]
                    if cnt > 100000:
                        continue
                    raw = bytes(buf[p + 4:p + 4 + (cnt if t == 30 else 2 * cnt)])
                    if t == 31 or codepage == 1200:
                        v = raw.decode("utf-16le", "replace")
                    else:
                        try:
                            v = raw.decode("cp%d" % codepage)
                        except Exception:
                            v = raw.decode("latin-1")
                    v = v.rstrip("\x00").strip()
                    if v:
                        out.append((fmtid, names.get(pid, pid), v))
    except (struct.error, IndexError, ValueError):
        pass
    return out


def views(path, data=None, loc="", depth=0, tmp=None):
    """Yield (loc, kind, bytes). kind: file, member-name, ole-stream, sw-chunk, prop, meta, ..."""
    if data is None:
        if os.path.getsize(path) > 2 << 30:
            return
        with open(path, "rb") as f:
            data = f.read()
    ext = os.path.splitext(path)[1].lower()
    yield loc or "file", "raw", data
    if depth >= MAX_DEPTH or not data:
        return
    head = data[:8]
    try:
        if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if z.comment:
                    yield loc + "|zipcomment", "meta", z.comment
                for info in z.infolist():
                    yield loc + "|zipname", "name", info.filename.encode("utf-8", "surrogateescape")
                    if info.is_dir() or info.file_size > MAX_MEMBER or info.flag_bits & 1:
                        continue
                    try:
                        b = z.read(info)
                    except Exception:
                        continue
                    yield from views(info.filename, b, loc + "|zip:" + info.filename, depth + 1, tmp)
            return
        if head.startswith(b"\x1f\x8b"):
            try:
                b = gzip.decompress(data)
            except Exception:
                b = None
            if b is not None:
                if len(data) > 10 and data[3] & 8:
                    j = data.find(b"\x00", 10)
                    yield loc + "|gzname", "name", data[10:j]
                inner = path[:-3] if path.lower().endswith(".gz") else path[:-4] + ".tar" if path.lower().endswith(".tgz") else path + ".bin"
                yield from views(inner, b, loc + "|gz", depth + 1, tmp)
            return
        if head.startswith(b"\x1f\x9d"):
            p = subprocess.run(["gzip", "-dc"], input=data, capture_output=True)
            if p.stdout:
                yield from views(path[:-2], p.stdout, loc + "|Z", depth + 1, tmp)
            return
        if len(data) > 512 and data[257:262] == b"ustar" or ext == ".tar":
            try:
                with tarfile.open(fileobj=io.BytesIO(data)) as t:
                    for info in t:
                        yield loc + "|tarname", "name", info.name.encode("utf-8", "surrogateescape")
                        if info.uname or info.gname:
                            yield loc + "|tarowner", "meta", ("%s %s" % (info.uname, info.gname)).encode()
                        if info.isreg() and info.size <= MAX_MEMBER:
                            f = t.extractfile(info)
                            if f:
                                yield from views(info.name, f.read(), loc + "|tar:" + info.name, depth + 1, tmp)
                return
            except tarfile.TarError:
                pass
        if head.startswith(b"7z\xbc\xaf\x27\x1c") or head.startswith(b"Rar!"):
            d = tempfile.mkdtemp(dir=tmp)
            try:
                a = os.path.join(d, "a" + (".7z" if head.startswith(b"7z") else ".rar"))
                with open(a, "wb") as f:
                    f.write(data)
                lst = subprocess.run(["7z", "l", "-slt", "-p-", a], capture_output=True).stdout.decode("utf-8", "replace")
                for line in lst.splitlines():
                    if line.startswith("Path = ") and not line.endswith(a):
                        yield loc + "|arcname", "name", line[7:].encode("utf-8")
                x = os.path.join(d, "x")
                subprocess.run(["7z", "x", "-y", "-p-", "-o" + x, a], capture_output=True)
                for dp, _dn, fn in os.walk(x):
                    for fname in fn:
                        fp = os.path.join(dp, fname)
                        if os.path.getsize(fp) <= MAX_MEMBER:
                            rel = os.path.relpath(fp, x)
                            with open(fp, "rb") as f:
                                b = f.read()
                            yield from views(rel, b, loc + "|arc:" + rel, depth + 1, tmp)
            finally:
                subprocess.run(["rm", "-rf", d])
            return
        if head.startswith(OLE_MAGIC):
            import olefile
            o = olefile.OleFileIO(io.BytesIO(data))
            try:
                for entry in o.listdir(streams=True, storages=False):
                    name = "/".join(entry)
                    try:
                        b = o.openstream(entry).read()
                    except Exception:
                        continue
                    sloc = loc + "|ole:" + name.replace("\x05", "^E")
                    yield sloc, "ole-stream", b
                    if entry[-1].startswith("\x05"):
                        for fmtid, pid, v in propsets(b):
                            yield sloc + "#%s:%s" % (fmtid[:8], pid), "prop", v.encode("utf-8", "replace")
                    if b[:4] == b"\xd0\xcf\x11\xe0" or b[:2] == b"PK" or b[:2] == b"\x89P" or b[:3] == b"\xff\xd8\xff":
                        yield from views(name, b, sloc, depth + 1, tmp)
            finally:
                o.close()
            return
        if ext in SW_EXT and SW_MARK in data[:64]:
            for name, raw in sw_chunks(data):
                cloc = loc + "|sw:" + name
                yield cloc, "sw-chunk", raw
                if raw[:2] == b"\xfe\xff":
                    for fmtid, pid, v in propsets(raw):
                        yield cloc + "#%s:%s" % (fmtid[:8], pid), "prop", v.encode("utf-8", "replace")
            return
        if ext == ".pdf" or head.startswith(b"%PDF"):
            import pikepdf
            try:
                with pikepdf.open(io.BytesIO(data)) as pdf:
                    info = pdf.trailer.get("/Info")
                    if info is not None:
                        for k in info.keys():
                            try:
                                yield loc + "|pdfinfo" + str(k), "prop", str(info[k]).encode("utf-8", "replace")
                            except Exception:
                                pass
                    meta = pdf.Root.get("/Metadata")
                    if meta is not None:
                        yield loc + "|xmp", "meta", meta.read_bytes()
                    for name, att in pdf.attachments.items():
                        yield loc + "|pdfattname", "name", name.encode("utf-8", "replace")
                        try:
                            b = att.get_file().read_bytes()
                            yield from views(name, b, loc + "|pdfatt:" + name, depth + 1, tmp)
                        except Exception:
                            pass
            except Exception:
                pass
            return
    except Exception as exc:  # unreadable container: the raw view was already yielded
        yield loc + "|error", "error", repr(exc).encode()
