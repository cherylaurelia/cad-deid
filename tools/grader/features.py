"""Per-file comparisons of the local grader (input file vs output file): OLE containers
(saved-account fields, name residue, previews, stream list), file references of assemblies
and drawings, and engineering fingerprints (geometry numbers, entity types, units, materials).
Every function is read-only and never raises."""
import collections
import hashlib
import io
import re
import struct
import zlib

import views

OLE_MAGIC = views.OLE_MAGIC
IMG_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"BM", b"GIF8", b"\x01\x00\x00\x00", b"\xd7\xcd\xc6\x9a")
PREVIEW_NAME = re.compile(r"(?i)preview|thumbnail|thumb|bitmap")
# OLE property ids that hold the account that last saved the file / the author
SAVED = {("e0859ff2", 8), ("39de383d", 8)}
AUTHOR = {("e0859ff2", 4), ("39de383d", 4), ("0f3f8532", 41), ("30fb61d8", 16)}
SAVED_NAME = re.compile(r"(?i)saved by|last saved|lastsavedby|zuletzt gespeichert")
DATE_RX = re.compile(r"(?i)^(\w+day, )?\w+ \d{1,2}, \d{4}|^\d{1,4}[./-]\d{1,2}[./-]\d{1,4}")
SAVER_STR = re.compile(rb"\xff\xfe\xff([\x01-\x40])((?:[^\x00]\x00){2,64})")
DATE_NAME = re.compile(r"(?i)date|datum|time|zeit|日|期")  # "Last Saved Date" is not an account
AUTHOR_NAME = re.compile(r"(?i)^(sw-)?author|designer|drawn|zeichner|ersteller|created by|engineer|checked")
ENG_PROP = re.compile(r"(?i)material|mass|density|volume|weight|units?$|finish|thickness|config|revision|rev$|"
                      r"part ?number|partno|description|quantity|qty|tolerance|scale|sheet|werkstoff|masse|dichte|"
                      r"surface|area|cost|stock|length|width|height|diameter")
GENERIC_ACCT = set("administrator admin user owner default none unknown pc guest student - . utente usuario "
                   "benutzer utilisateur".split())


def ole_streams(data):
    import olefile
    o = olefile.OleFileIO(io.BytesIO(data))
    out = []
    try:
        for e in o.listdir(streams=True, storages=False):
            try:
                out.append(("/".join(e), o.openstream(e).read()))
            except Exception:
                out.append(("/".join(e), None))
    finally:
        o.close()
    return out


def ole_props(streams):
    """[(stream, fmtid8, pid or name, value)] of every string property."""
    out = []
    for name, b in streams:
        if b is None or not (name.split("/")[-1].startswith("\x05") or b[:4] == b"\xfe\xff\x00\x00"):
            continue
        for fmtid, pid, v in views.propsets(b):
            out.append((name, fmtid[:8], pid, v))
    return out


def inflated(b):
    for off in (0, 2, 4, 8, 12, 16, 20, 24):
        if len(b) > off + 2 and b[off] == 0x78 and b[off + 1] in (0x01, 0x5e, 0x9c, 0xda):
            try:
                return zlib.decompressobj().decompress(b[off:])
            except Exception:
                return None
    if b[:4] == b"#\x1d\xd5q":  # SolidWorks __ZLB stream: raw deflate after a small header
        for off in (12, 16, 20, 24, 28):
            try:
                return zlib.decompressobj(-15).decompress(b[off:])
            except Exception:
                continue
    return None


def find_name(b, name):
    """Case-insensitive, word-bounded occurrence of name in latin-1 / UTF-16LE / UTF-8."""
    if not b or not name:
        return None
    low = b.lower()
    for enc in ("latin-1", "utf-16le", "utf-8", "utf-16be"):
        try:
            pat = name.lower().encode(enc)
        except UnicodeEncodeError:
            continue
        if len(pat) < 3:
            continue
        step = 2 if enc.startswith("utf-16") else 1
        i = low.find(pat)
        while i >= 0:
            before = low[i - step:i] if i >= step else b""
            after = low[i + len(pat):i + len(pat) + step]
            if enc == "utf-16be":
                bc, ac = before[1:2], after[1:2]
                bz, az = before[:1] == b"\x00", after[:1] == b"\x00"
            else:
                bc, ac = before[:1], after[:1]
                bz = step == 1 or before[1:2] == b"\x00"
                az = step == 1 or after[1:2] == b"\x00"
            if not (bc.isalpha() and bz) and not (ac.isalpha() and az) or not name[:1].isascii():
                return enc
            i = low.find(pat, i + 1)
    return None


def img_info(b):
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(b))
        im.load()
        return [im.format, im.size[0], im.size[1]]
    except Exception:
        return None


def preview_streams(streams):
    out = {}
    for name, b in streams:
        if b is None:
            continue
        leaf = name.split("/")[-1]
        if PREVIEW_NAME.search(leaf) or (len(b) > 64 and b[:4] == b"\x89PNG"):
            out[name] = (hashlib.md5(b).hexdigest(), img_info(b), len(b))
    return out


def account_values(props, streams):
    """{slot: value} of saved-account and author slots of one OLE file. Values come from the parsed
    property sets only (ISolidWorksInformation is a property set too); its stream text holds labels
    such as "SW-Last Saved By(Last Saved By)", which are not accounts. Dates are skipped."""
    saved, author = {}, {}
    for stream, fm, pid, v in props:
        key = "%s:%s:%s" % (stream.replace("\x05", "^E"), fm, pid)
        if isinstance(pid, str) and DATE_NAME.search(pid):
            continue
        if re.fullmatch(r"(?i)(sw-)?(last saved by|author|saved by)", v.strip()) or DATE_RX.match(v.strip()):
            continue
        if (fm, pid) in SAVED or isinstance(pid, str) and SAVED_NAME.search(pid):
            saved[key] = v
        elif (fm, pid) in AUTHOR or isinstance(pid, str) and AUTHOR_NAME.search(pid):
            author[key] = v
    return saved, author


def saver_history(streams):
    """SolidWorks Header2: the su_CStringArray right after its class name lists every account that
    saved the file (UTF-16, 0xFF 0xFE 0xFF + length). The feature history that follows uses the same
    string encoding, so only the contiguous run of strings that starts at the array is read."""
    out = []
    for n, d in streams:
        if d is None or not n.endswith("Header2"):
            continue
        i = d.find(b"su_CStringArray")
        if i < 0:
            continue
        pos = i + len(b"su_CStringArray")
        for m in SAVER_STR.finditer(d, pos):
            if m.start() - pos > 24:
                break
            s = m.group(2).decode("utf-16le")
            if len(s) != m.group(1)[0]:
                break
            out.append(s)
            pos = m.end()
    return [s for s in out if re.fullmatch(r"[^\x00-\x1f]{2,40}", s) and s.lower() not in GENERIC_ACCT]


def name_pieces(v):
    out = []
    for piece in re.split(r"[\\/;,]+", v):
        piece = piece.strip()
        if len(piece) >= 3 and piece.lower() not in GENERIC_ACCT and not piece.isdigit():
            out.append(piece)
    return out


def ole_compare(src, dst):
    """Saved-account / residue / preview / container results of one OLE file pair."""
    res = {"kind": "ole"}
    try:
        a = open(src, "rb").read()
    except Exception as exc:
        return {"kind": "ole", "error": "input " + repr(exc)[:80]}
    try:
        b = open(dst, "rb").read()
    except Exception:
        return {"kind": "ole", "missing": True}
    try:
        sa = ole_streams(a)
    except Exception as exc:
        return {"kind": "ole", "error": "input not OLE " + repr(exc)[:60]}
    try:
        sb = ole_streams(b)
    except Exception as exc:
        res["container"] = "broken: " + repr(exc)[:60]
        res["saved"] = res["residue"] = res["preview"] = "unopenable"
        return res
    na, nb = [n for n, _ in sa], [n for n, _ in sb]
    if na != nb:
        res["container"] = "stream list changed %d->%d" % (len(na), len(nb))
    else:
        sizes = [n for (n, d1), (_, d2) in zip(sa, sb) if (d1 is None) != (d2 is None) or d1 is not None and len(d1) != len(d2)]
        res["container"] = "stream size changed: " + sizes[0][:60] if sizes else "ok"
    pa, pb = preview_streams(sa), preview_streams(sb)
    if not pa:
        res["preview"] = "none"
    else:
        bad = []
        for n, (h, info, ln) in pa.items():
            o = pb.get(n)
            if o is None:
                bad.append(n + ":missing")
            elif o[0] != h and (info is None or o[1] != info):
                bad.append(n + ":changed")
        res["preview"] = "ok" if not bad else "bad " + ",".join(bad)[:80]
        res["preview_changed_bytes"] = sum(1 for n, (h, _i, _l) in pa.items() if n in pb and pb[n][0] != h)
    props_a, props_b = ole_props(sa), ole_props(sb)
    saved_a, author_a = account_values(props_a, sa)
    saved_b, author_b = account_values(props_b, sb)
    slots = []
    names = set()
    for kind, va, vb in (("saved", saved_a, saved_b), ("author", author_a, author_b)):
        for k, v in va.items():
            pieces = name_pieces(v)
            if not pieces:
                continue
            nv = vb.get(k)
            st = "missing" if nv is None else ("blank" if not nv.strip() else ("UNCHANGED" if any(
                find_name(nv.encode("utf-8"), p) for p in pieces) else "ok"))
            slots.append((kind, k, v, nv or "", st))
            names.update(pieces)
    res["slots"] = slots
    # saver history (SolidWorks Header2: every account that saved the file)
    saver = set(saver_history(sa))
    res["saver"] = sorted(saver)
    hits = []
    for nm in sorted(names | saver):
        where = []
        for n, d in sb:
            if d is None:
                continue
            e = find_name(d, nm)
            if e:
                where.append(n.replace("\x05", "^E") + ":" + e)
            else:
                inf = inflated(d)
                if inf and find_name(inf, nm):
                    where.append(n.replace("\x05", "^E") + ":zlib")
        if not where and find_name(b, nm):
            where.append("slack")
        if where:
            hits.append((nm, "saver" if nm in saver and nm not in names else "field", where[:6]))
    res["residue_hits"] = hits
    return res


# ------------------------------------------------------------------ newer SolidWorks: does the file open?
def sw_crc_bad(path):
    """Number of newer-SolidWorks chunks whose header CRC-32 (ZIP local-header layout, +10) no longer
    matches the inflated data. SolidWorks refuses such a file; the scorer's reader does too (the
    official attempt-2/3 losses in relationships, engineering and drawing/model)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return None
    if views.SW_MARK not in data[:64]:
        return None
    bad = 0
    pos = data.find(views.SW_MARK)
    while pos >= 0:
        nxt = pos + 6
        if pos + 26 <= len(data):
            crc, csize, usize, nlen = struct.unpack_from("<IIII", data, pos + 10)
            start = pos + 26 + nlen
            if nlen < 512 and 0 < csize and start + csize <= len(data) and usize < (1 << 31):
                try:
                    raw = zlib.decompressobj(-15).decompress(data[start:start + csize])
                except zlib.error:
                    raw = None
                if raw is not None and len(raw) == usize:
                    if zlib.crc32(raw) & 0xFFFFFFFF != crc:
                        bad += 1
                    nxt = start + csize
        pos = data.find(views.SW_MARK, nxt)
    return bad


# ------------------------------------------------------------------ references
REF_EXTS = "sldprt|sldasm|slddrw|ipt|iam|idw|ipn|par|psm|asm|dft|catpart|catproduct|catdrawing|prt|drw|step|stp|x_t|x_b|fcstd|stl|dwg|dxf|igs|iges|sat|jt|3dm|obj"
REF_EXT_LIST = [e.encode() for e in REF_EXTS.split("|")]
BAD = set(b'\x00\r\n\t"*<>|?:\\/') | set(range(0, 32))
ASM_EXT = set(".sldasm .slddrw .iam .idw .ipn .asm .dft .catproduct .catdrawing .drw .fcstd".split())


def _names_before(low, raw, anchor, wide):
    step = 2 if wide else 1
    i = low.find(anchor)
    while i >= 0:
        e = i + len(anchor)
        nxt = raw[e:e + step]
        if not (nxt[:1].isalnum() and (not wide or nxt[1:2] == b"\x00")):
            s, n = i, 0
            while s - step >= 0 and n < 120:
                c = raw[s - step]
                if wide and raw[s - step + 1] != 0:
                    break
                if c in BAD:
                    break
                s -= step
                n += 1
            if n:
                yield raw[s:e].decode("utf-16le" if wide else "latin-1", "replace").strip()
        i = low.find(anchor, i + 1)


def refs_of(path):
    out = set()
    try:
        for loc, kind, b in views.views(path):
            if kind not in ("raw", "ole-stream", "sw-chunk") or len(b) > (200 << 20):
                continue
            low = b.lower()
            for ext in REF_EXT_LIST:
                out.update(_names_before(low, b, b"." + ext, False))
                out.update(_names_before(low, b, (b"." + ext).decode().encode("utf-16le"), True))
            if kind == "ole-stream":
                inf = inflated(b)
                if inf:
                    il = inf.lower()
                    for ext in REF_EXT_LIST:
                        out.update(_names_before(il, inf, b"." + ext, False))
                        out.update(_names_before(il, inf, (b"." + ext).decode().encode("utf-16le"), True))
    except Exception:
        pass
    return sorted(r for r in out if 3 <= len(r) <= 120)


# ------------------------------------------------------------------ engineering fingerprints
NUM = re.compile(rb"(?<![A-Za-z0-9_.#$])[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?(?![A-Za-z0-9_])")
STEP_STR = re.compile(rb"'(?:[^']|'')*'")
STEP_ENT = re.compile(rb"=\s*([A-Z][A-Z0-9_]*)\s*\(")
STEP_ENT2 = re.compile(rb"\(\s*([A-Z][A-Z0-9_]*)\s*\(")
STEP_ENUM = re.compile(rb"\.([A-Z][A-Z0-9_]*)\.")


def _h(c):
    return hashlib.md5(repr(sorted(c.items())).encode()).hexdigest()[:16]


def step_fp(b):
    i = b.find(b"DATA;")
    body = b[i:] if i >= 0 else b
    nostr = STEP_STR.sub(b"''", body)
    ents = collections.Counter(STEP_ENT.findall(nostr)) + collections.Counter(STEP_ENT2.findall(nostr))
    enums = collections.Counter(STEP_ENUM.findall(nostr))
    nums = collections.Counter(NUM.findall(re.sub(rb"#\d+", b"#", nostr)))
    units = sorted(set(re.findall(rb"SI_UNIT\s*\([^;]*\)|CONVERSION_BASED_UNIT\s*\('[^']*'", body)))
    return {"ents": _h(ents), "enums": _h(enums), "nums": _h(nums), "units": hashlib.md5(b"|".join(units)).hexdigest()[:12],
            "n_ents": sum(ents.values())}


def text_fp(b, ext):
    if ext in (".igs", ".iges"):
        lines = [l[:72] for l in b.split(b"\n") if len(l) >= 73 and l[72:73] in (b"P", b"D")]
        body = b"\n".join(lines)
        body = re.sub(rb"\d+H", b"H", body)
        return {"nums": _h(collections.Counter(NUM.findall(body))), "n": len(lines)}
    if ext == ".sat":
        nostr = re.sub(rb"@\d+ [^\n]*?(?= [-$#@]|\n)", b"@", b)
        toks = re.findall(rb"(?m)^-?\d*\s*([a-z][a-z0-9_-]*)", nostr)
        return {"ents": _h(collections.Counter(toks)), "nums": _h(collections.Counter(NUM.findall(nostr)))}
    if ext == ".dxf":
        lines = b.replace(b"\r\n", b"\n").split(b"\n")
        pairs = collections.Counter()
        ents = collections.Counter()
        for k in range(0, len(lines) - 1, 2):
            code = lines[k].strip()
            if not code.lstrip(b"-").isdigit():
                continue
            c = int(code)
            if 10 <= c <= 59 or 110 <= c <= 149 or 210 <= c <= 239:
                pairs[(c, lines[k + 1].strip())] += 1
            elif c == 0:
                ents[lines[k + 1].strip()] += 1
        return {"nums": _h(pairs), "ents": _h(ents)}
    if ext in (".stl",):
        if b[:5].lower() == b"solid" and b"facet" in b[:2000]:
            return {"nums": _h(collections.Counter(NUM.findall(b[b.find(b"\n"):])))}
        return {"geom": hashlib.md5(b[80:]).hexdigest()[:16]}
    if ext in (".obj", ".wrl", ".vrml", ".x_t", ".xmt_txt", ".off", ".ply", ".brep", ".unv", ".neu", ".scad", ".svg", ".gcode", ".nc"):
        nostr = re.sub(rb'"[^"\n]*"', b'""', b) if ext in (".wrl", ".vrml", ".svg") else b
        if ext == ".svg":
            nostr = re.sub(rb">[^<]*<", b"><", nostr)
        return {"nums": _h(collections.Counter(NUM.findall(nostr)))}
    return None


def zip_fp(b):
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(b)) as z:
            names = z.infolist()
            out = {"n_members": len(names)}
            doc = None
            for i in names:
                if i.filename.lower() in ("document.xml", "3d/3dmodel.model"):
                    doc = z.read(i)
            if doc is not None:
                nostr = re.sub(rb'(?i)(value|name|label|file|creator|company|comment|createdby|lastmodifiedby)="[^"]*"', b"", doc)
                nostr = re.sub(rb"<metadata[^>]*>[^<]*</metadata>", b"", nostr)
                out["nums"] = _h(collections.Counter(NUM.findall(nostr)))
            return out
    except Exception:
        return None


def ole_eng(b):
    try:
        st = ole_streams(b)
    except Exception:
        return None
    props = ole_props(st)
    eng = collections.Counter()
    for stream, fm, pid, v in props:
        if isinstance(pid, str) and ENG_PROP.search(pid) or (fm, pid) in {("0f3f8532", 20), ("0f3f8532", 5), ("0f3f8532", 29)}:
            eng[(fm, str(pid), v)] += 1
    geo = []
    for n, d in st:
        leaf = n.split("/")[-1].lower()
        if d is not None and re.search(r"partition|parasolid|stream\d*\.x_b|body|brep|psmcluster|geometr", leaf):
            geo.append((n, len(d)))
    return {"props": _h(eng), "geo": hashlib.md5(repr(sorted(geo)).encode()).hexdigest()[:12], "n_props": sum(eng.values())}


def eng_fp(path, ext):
    try:
        with open(path, "rb") as f:
            b = f.read()
    except Exception:
        return {"error": "missing"}
    if ext in (".step", ".stp", ".p21") or b[:13] == b"ISO-10303-21;":
        return step_fp(b)
    if b[:8] == OLE_MAGIC:
        return ole_eng(b)
    if ext in (".fcstd", ".3mf"):
        return zip_fp(b)
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"):
        info = img_info(b)
        return {"img": info}
    return text_fp(b, ext)
