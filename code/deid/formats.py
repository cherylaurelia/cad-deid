"""Per-format handling: which bytes of a file are scanned and how the result is written."""
import gzip
import os
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import zipfile
import zlib

from . import swnative
from .identities import _curated_list
from .scan import OVERLAP, document_text_rules, is_generic, name_matcher, replace_names

CHUNK = 32 * 1024 * 1024
MAX_DEPTH = 4  # archives inside archives

# Scanned as text over the whole file.
TEXT_EXT = set("""
.step .stp .p21 .sat .igs .iges .wrl .vrml .obj .mtl .dxf .svg .x_t .xmt_txt .brep .brp
.neu .unv .scad .off .txt .csv .json .xml .html .htm .md .py .gcode .amf .x3d
.dae .ifc .scr .ini .cfg .yaml .yml .url .bom .log .tex .kicad_pcb .kicad_mod .ply .model
.rels
""".split())
# Never treated as text even if the first block looks like it.
BINARY_EXT = set("""
.sldprt .sldasm .slddrw .slddrt .sldlfp .prtdot .asmdot .drwdot .ipt .iam .idw .ipn .ipj
.dwg .pdf .png .jpg .jpeg .tif .tiff .gif .bmp .zip .gz .z .7z .rar .tar .tgz .3mf .fcstd
.f3d .f3z .jt .x_b .xmt_bin .u3d .par .psm .asm .dft .prt .drw .catpart .catproduct
.catdrawing .3ds .odg .sab .skp .blend .mf1
""".split())
# ZIP-based CAD/document containers: members are scanned, never renamed.
ZIP_CONTAINERS = set(".3mf .fcstd .f3d .f3z .odg .fcstd1 .docx .xlsx .pptx .ods .odt".split())
# Binary native formats without checksums: stored paths and names are replaced in place
# (same length). DWG, JT and images are not touched this way.
RAW_BINARY_EXT = set("""
.sldprt .sldasm .slddrw .slddrt .sldlfp .prtdot .asmdot .drwdot .x_b .xmt_bin .catpart
.catproduct .catdrawing .par .psm .asm .dft .prt .ipt .iam .idw .ipn
""".split())
IMAGE_EXT = set(".png .jpg .jpeg .tif .tiff .gif .bmp".split())
PDF_NAME_KEYS = ("/Author", "/Company", "/Manager", "/LastModifiedBy")
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
SW_MARK = b"\x14\x00\x06\x00\x08\x00"
SW_EXT = set(".sldprt .sldasm .slddrw .slddrt .sldlfp .prtdot .asmdot .drwdot".split())


class Ctx:
    """What a handler needs: the scanners, the path mapper, the current project's
    in-tree rename rules, and a scratch folder for archive members."""

    def __init__(self, text, binary, mapper, tmp):
        self.text, self.binary, self.mapper, self.tmp = text, binary, mapper, tmp
        self.rules = ()
        self.project = ()  # account / project names of the folder the current file is in
        self.parts = ((), ())  # their distinctive pieces: (any case, exact case)
        self.global_map = {}  # every dictionary identifier -> pseudonym, for the PDF rewriter

    def member(self, name):
        parts = name.split("/")
        out = [self.mapper.dir_name(p, self.rules) if p else p for p in parts[:-1]]
        out.append(self.mapper.file_name(parts[-1], self.rules) if parts[-1] else "")
        return "/".join(out)


def looks_text(path):
    with open(path, "rb") as f:
        head = f.read(8192)
    if not head or b"\x00" in head:
        return False
    odd = sum(1 for b in head if b < 9 or 13 < b < 32)
    return odd * 50 < len(head)


def place(src, dst):
    """Output is byte-identical to the input: hard-link it (same disk) or copy it."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def scan_stream(src, dst, scanner):
    """Scan a whole file in overlapping windows; write dst. Returns replacements."""
    total = 0
    tmp = dst + ".part"
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        carry, first = b"", True
        while True:
            block = fin.read(CHUNK)
            last = len(block) < CHUNK
            window = bytearray(carry + block)
            zone = len(window) if last else len(window) - OVERLAP
            total += scanner.scan(window, zone, first)
            first = False
            if last:
                fout.write(window)
                break
            fout.write(window[:zone])
            carry = bytes(window[zone:])
    if total == 0:
        os.remove(tmp)
        place(src, dst)
    else:
        os.replace(tmp, dst)
    return total


def scan_head(src, dst, scanner, nbytes):
    """Scan only the first nbytes (a fixed-size header); the rest is copied as is."""
    with open(src, "rb") as f:
        head = bytearray(f.read(nbytes))
    if not scanner.scan(head, first=False):
        place(src, dst)
        return 0
    shutil.copyfile(src, dst)
    with open(dst, "r+b") as f:
        f.write(head)
    return 1


def scan_comment_lines(src, dst, scanner):
    """PostScript: scan only the % comment lines (title, creator, owner); the program
    and its encoded image data are left alone."""
    with open(src, "rb") as f:
        lines = f.read().splitlines(keepends=True)
    total = 0
    for i, line in enumerate(lines):
        if line.startswith(b"%") and len(line) < 4000:
            buf = bytearray(line)
            n = scanner.scan(buf, first=False)
            if n:
                lines[i] = bytes(buf)
                total += n
    if not total:
        place(src, dst)
    else:
        with open(dst, "wb") as f:
            f.writelines(lines)
    return total


def combine(original, *versions):
    """Merge same-length rewrites of one text: each character comes from the first
    version that changed it. Every rule works on the original, so nothing is replaced twice."""
    out = list(original)
    for version in versions:
        if version != original and len(version) == len(original):
            for i, (a, b) in enumerate(zip(original, version)):
                if a != b and out[i] == a:
                    out[i] = b
    return "".join(out)


INITIALS_NOT_PEOPLE = {"PRD", "TDP", "CIT"}  # document-type codes in title-block slots


INNER_ACRONYMS = tuple(_curated_list(kind="inner"))
INNER_ACRONYMS_LOW = {a.lower() for a in INNER_ACRONYMS}
_INNER = {}


def _inner_rx(ctx):
    """DEID_PDF_INNER=1: distinctive identifiers (one word of 5+ letters that is not an
    ordinary word, or a known acronym) are also replaced inside longer tokens of PDF text
    ("xACME", "acmelabs.com", "Acmewerk"). None when off."""
    if os.environ.get("DEID_PDF_INNER") != "1":
        return None
    if "rx" not in _INNER:
        everyday = getattr(ctx.mapper, "everyday", ()) or ()
        words = {w.lower() for w in getattr(ctx, "global_map", {})
                 if w.isalpha() and w.isascii() and len(w) >= 5 and w.lower() not in everyday}
        words = {w for w in words if w not in INNER_STOP and not _in_dictionary_word(w)}
        words |= {a.lower() for a in INNER_ACRONYMS}
        _INNER["rx"] = re.compile("|".join(map(re.escape, sorted(words, key=len, reverse=True))), re.I) if words else None
    return _INNER["rx"]


# German words that are pieces of everyday compounds (Benutzeroberfläche, Unterbaugruppen)
INNER_STOP = {"benutzer", "unterbau", "baugruppe", "baugruppen", "oberflaeche", "zeichnung"}


def _in_dictionary_word(w):
    """True when w is an ordinary word (pdfids word test) or a piece of a longer English word
    ("lectro" in electronic, "nvention" in convention): never an identifier inside a token."""
    from . import pdfids
    if pdfids._is_word(w):
        return True
    if "grams" not in _INNER:
        grams = {}
        for d in pdfids._english() | pdfids.DRAWING_WORDS:
            for i in range(len(d) - 4):
                grams.setdefault(d[i:i + 5], []).append(d)
        _INNER["grams"] = grams
    return any(w in d for d in _INNER["grams"].get(w[:5], ()))


def _inner_sub(m, pseudo):
    """A non-acronym piece needs a word boundary on at least one side: "acmelabs" in
    www.acmelabs.com, "Acmewerk" in Acmewerkstatt, not "nutzer" in Benutzer."""
    s, a, b = m.string, m.start(), m.end()
    if m.group().lower() in INNER_ACRONYMS_LOW:
        if not m.group().isupper():  # "unam" in unambiguous
            return m.group()
    elif (a > 0 and s[a - 1].isalpha()) and (b < len(s) and s[b].isalpha()):
        return m.group()
    return pseudo.span(m.group())


class PdfFix:
    """Text rewriting for one PDF: the document's own identifiers (title-block names,
    part numbers, named entities found by pdfids), web hosts and labelled phone numbers,
    and the global identifier dictionary."""

    def __init__(self, ctx, path):
        self.ctx = ctx
        found = []
        try:
            from . import pdfids
            found = pdfids.find_detailed(path)
        except Exception:
            found = []
        found = [(kind, s.strip()) for kind, s in found if 3 <= len(s.strip()) <= 80]
        try:  # the document's own author field: its parts are names in the page text too
            import pymupdf
            with pymupdf.open(path) as doc:
                author = (doc.metadata or {}).get("author") or ""
            if author and not is_generic(author):
                found += [("person", piece.strip()) for piece in re.split(r"[\\/()@;,]+", author)
                          if len(piece.strip()) >= 3 and not is_generic(piece)]
        except Exception:
            pass
        ctx.text.found.extend(("pdf-" + kind, s) for kind, s in found)
        # part and drawing numbers also name files and models: they are only replaced
        # when the whole corpus agrees (the global dictionary), never in one PDF alone
        own = sorted({s for kind, s in found if kind not in ("part", "initials")}, key=len, reverse=True)[:1500]
        # "J. SMITH" also as "J SMITH"; a person's family name also on its own
        bodies = [r"\.?\s*".join(re.escape(w.rstrip(".")) for w in s.split() if w.rstrip(".")) for s in own]
        everyday = getattr(ctx.mapper, "everyday", ())
        for kind, s in found:
            words = s.split()
            if kind == "person" and len(words) >= 2:
                last = words[-1].strip(".,")
                if len(last) >= 4 and last.isalpha() and last.lower() not in everyday:
                    bodies.append(re.escape(last))
        # the project's own account / project names, however their words are separated
        bodies += [r"[\s_.-]*".join(re.escape(w) for w in re.findall(r"[A-Za-z0-9]+", name)) for name in ctx.project]
        bodies = [b for b in bodies if b]
        self.rx = None
        if bodies:
            self.rx = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(bodies) + r")(?![A-Za-z0-9])", re.I)
        # distinctive pieces of the account / project name on their own ("Smith" of JaneSmith,
        # "hamish" of hamish2014): bounded by letters only, so "PAROL" is also found in "PAROL6"
        loose, exact = getattr(ctx, "parts", ((), ()))
        self.rx_parts = [re.compile(r"(?<![A-Za-z])(?:" + "|".join(map(re.escape, words)) + r")(?![A-Za-z])", flags)
                         for words, flags in ((loose, re.I), (exact, 0)) if words]
        # initials in a title-block slot (3+ capitals): this document only, exact case
        marks = sorted({s for kind, s in found if kind == "initials" and len(s) >= 3 and s.isalpha() and s.isupper()
                        and s not in INITIALS_NOT_PEOPLE})
        if marks:
            self.rx_parts.append(re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(marks) + r")(?![A-Za-z0-9])"))

    def __call__(self, text):
        pseudo = self.ctx.text.pseudo
        versions = [document_text_rules(text, pseudo), self.ctx.text.text(text)]
        inner = _inner_rx(self.ctx)
        if inner is not None:
            versions.append(inner.sub(lambda m: _inner_sub(m, pseudo), text))
        if self.rx is not None:
            versions.insert(0, self.rx.sub(lambda m: pseudo.span(m.group()), text))
        versions += [rx.sub(lambda m: pseudo.span(m.group()), text) for rx in self.rx_parts]
        return combine(text, *versions)


def pdf_metadata(src, dst, scanner, ctx=None, depth=0, fix=None):
    """Pseudonymize the PDF Info dictionary and the XMP packet. Returns replacements."""
    from .pdfpost import is_generic_name
    import pikepdf

    count = 0
    with pikepdf.open(src) as pdf:
        info = pdf.trailer.get("/Info")
        if info is not None:
            for key in list(info.keys()):
                value = info[key]
                if not isinstance(value, pikepdf.String):
                    continue
                old = str(value)
                if key in PDF_NAME_KEYS and not is_generic_name(old):
                    new = scanner.pseudo.span_all(old)
                    scanner.found.append(("person", old))
                elif key in ("/Creator", "/Producer") and is_generic(old):
                    new = scanner.text(old)  # software string: known names only, not its vendor's host
                else:
                    new = fix(old) if fix else scanner.text(old)
                if new != old:
                    info[key] = pikepdf.String(new)
                    count += 1
        meta = pdf.Root.get("/Metadata")
        if meta is not None:
            before = meta.read_bytes()
            packet = bytearray(before)
            scanner.scan(packet, first=False)
            text = before.decode("utf-8", "surrogateescape")
            if b"\x00" not in before[:64]:
                # name slots (dc:creator, pdf:Author, ...) are names whatever the dictionary holds,
                # like the Info /Author: the same value gets the same pseudonym in both
                from .pdfpost import _packet
                named = _packet(text, lambda t: t, scanner.pseudo)
            else:
                named = text
            merged = combine(text, named, bytes(packet).decode("utf-8", "surrogateescape"),
                             fix(text) if fix is not None else text)
            packet = bytearray(merged.encode("utf-8", "surrogateescape"))
            if bytes(packet) != before:
                meta.write(bytes(packet))
                count += 1
        if count:
            pdf.save(dst, deterministic_id=True)
    if not count:
        place(src, dst)
    return count



# -- archives and containers ------------------------------------------------------

def _member_ext(name):
    return os.path.splitext(name)[1].lower()


def zip_file(src, dst, ctx, rename, depth):
    """Rewrite a ZIP: every member goes through process(); member names are renamed like
    corpus paths when rename is set. Order, compression method and dates are kept."""
    total = 0
    part = dst + ".part"
    tmpdir = tempfile.mkdtemp(dir=ctx.tmp)
    try:
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(part, "w", allowZip64=True) as zout:
            infos = zin.infolist()
            if any(i.flag_bits & 1 for i in infos):
                raise ValueError("encrypted members")
            comment = bytearray(zin.comment)
            if comment and ctx.text.scan(comment, first=False):
                total += 1
            zout.comment = bytes(comment)
            seen = set()
            for i, info in enumerate(infos):
                name = info.filename
                new_name = ctx.member(name) if rename else name
                if new_name in seen:
                    new_name = name
                seen.add(new_name)
                total += new_name != name
                out = zipfile.ZipInfo(new_name, info.date_time)
                out.compress_type = info.compress_type if info.compress_type in (0, 8) else 8
                out.external_attr, out.create_system, out.comment = info.external_attr, info.create_system, info.comment
                if info.is_dir():
                    zout.writestr(out, b"")
                    continue
                ext = _member_ext(name)
                a, b = os.path.join(tmpdir, "in%d%s" % (i, ext)), os.path.join(tmpdir, "out%d%s" % (i, ext))
                with zin.open(info) as f, open(a, "wb") as g:
                    shutil.copyfileobj(f, g, 1 << 20)
                total += process(a, b, ext, ctx, depth + 1)[1]
                with open(b, "rb") as f, zout.open(out, "w", force_zip64=os.path.getsize(b) > 0xF0000000) as g:
                    shutil.copyfileobj(f, g, 1 << 20)
                os.remove(a)
                os.remove(b)
    except Exception:
        if os.path.lexists(part):
            os.remove(part)
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return _finish(src, dst, part, total)


def _finish(src, dst, part, total):
    if total == 0:
        os.remove(part)
        place(src, dst)
    else:
        os.replace(part, dst)
    return total


def tar_file(src, dst, ctx, depth):
    total = 0
    part = dst + ".part"
    tmpdir = tempfile.mkdtemp(dir=ctx.tmp)
    try:
        with tarfile.open(src, "r:*") as tin:
            gz = isinstance(tin.fileobj, gzip.GzipFile)
            # own gzip layer with the source's header time and no stored name: tarfile's "w:gz"
            # stamps the current time and the ".part" file name, so no two runs would match
            raw = open(part, "wb")
            out = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=_gzip_mtime(src)) if gz else raw
            with raw, out, tarfile.open(fileobj=out, mode="w") as tout:
                for i, info in enumerate(tin):
                    new_name = ctx.member(info.name)
                    total += new_name != info.name
                    info.name = new_name
                    for attr in ("uname", "gname"):
                        owner = getattr(info, attr)
                        if owner and owner.lower() not in ("root", "wheel", "staff", "users", "nobody", "runner"):
                            setattr(info, attr, ctx.text.pseudo.span(owner))
                            total += 1
                    info.pax_headers = {}
                    if not info.isreg():
                        tout.addfile(info)
                        continue
                    ext = _member_ext(new_name)
                    a, b = os.path.join(tmpdir, "in%d%s" % (i, ext)), os.path.join(tmpdir, "out%d%s" % (i, ext))
                    with tin.extractfile(info) as f, open(a, "wb") as g:
                        shutil.copyfileobj(f, g, 1 << 20)
                    total += process(a, b, ext, ctx, depth + 1)[1]
                    info.size = os.path.getsize(b)
                    with open(b, "rb") as f:
                        tout.addfile(info, f)
                    os.remove(a)
                    os.remove(b)
    except Exception:
        if os.path.lexists(part):
            os.remove(part)
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return _finish(src, dst, part, total)


def _gzip_mtime(path):
    """The modification time stored in a gzip header (0 when there is none)."""
    with open(path, "rb") as f:
        head = f.read(8)
    return struct.unpack_from("<I", head, 4)[0] if len(head) == 8 else 0


def _gzip_name(path):
    """The original file name stored in a gzip header, or ''."""
    with open(path, "rb") as f:
        head = f.read(4096)
    if len(head) < 10 or not head[3] & 8:
        return ""
    i = 10
    if head[3] & 4:
        i += 2 + struct.unpack_from("<H", head, i)[0]
    j = head.find(b"\x00", i)
    return head[i:j].decode("latin-1") if j > 0 else ""


def gzip_file(src, dst, ctx, depth):
    """A single compressed file (.gz) or a compressed tar."""
    try:
        with tarfile.open(src, "r:gz") as t:
            t.next()
        return "tar", tar_file(src, dst, ctx, depth)
    except tarfile.TarError:
        pass
    stored = _gzip_name(src)
    inner = stored or os.path.basename(src)[:-3]
    ext = _member_ext(inner)
    tmpdir = tempfile.mkdtemp(dir=ctx.tmp)
    part = dst + ".part"
    try:
        a, b = os.path.join(tmpdir, "in" + ext), os.path.join(tmpdir, "out" + ext)
        with gzip.open(src, "rb") as f, open(a, "wb") as g:
            shutil.copyfileobj(f, g, 1 << 20)
            mtime = f.mtime
        total = process(a, b, ext, ctx, depth + 1)[1]
        new_stored = ctx.mapper.file_name(stored, ctx.rules) if stored else ""
        total += new_stored != stored
        if total:
            with open(part, "wb") as raw, open(b, "rb") as f, \
                    gzip.GzipFile(filename=new_stored, mode="wb", fileobj=raw, mtime=mtime or 0) as g:
                shutil.copyfileobj(f, g, 1 << 20)
        else:
            open(part, "wb").close()
    except Exception:
        if os.path.lexists(part):
            os.remove(part)
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return "gzip", _finish(src, dst, part, total)


def compress_file(src, dst, ctx, depth):
    """Unix compress (.Z): decompress, process, recompress with the compress tool."""
    ext = _member_ext(os.path.basename(src)[:-2])
    tmpdir = tempfile.mkdtemp(dir=ctx.tmp)
    try:
        a, b = os.path.join(tmpdir, "in" + ext), os.path.join(tmpdir, "out" + ext)
        with open(a, "wb") as g:
            subprocess.run(["gzip", "-dc", src], stdout=g, check=True, stderr=subprocess.DEVNULL)
        total = process(a, b, ext, ctx, depth + 1)[1]
        if not total:
            place(src, dst)
            return 0
        with open(b, "rb") as f, open(dst + ".part", "wb") as g:
            done = subprocess.run(["compress", "-c"], stdin=f, stdout=g, stderr=subprocess.DEVNULL)
        if done.returncode not in (0, 2) or os.path.getsize(dst + ".part") == 0:
            os.remove(dst + ".part")
            raise RuntimeError("compress failed")
        os.replace(dst + ".part", dst)
        return total
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# -- native binary CAD -----------------------------------------------------------

# Property ids that hold a person or organisation, by property-set FMTID (hex, as stored).
PROPSET_NAMES = {
    "e0859ff2f94f6810ab9108002b27b3d9": {4: "person", 8: "person"},            # SummaryInformation
    "02d5cdd59c2e1b10939708002b2cf9ae": {14: "person", 15: "org"},             # DocumentSummaryInformation
    "0f3f85324434d1119e930060b03c1ca6": {10: "person", 12: "person", 30: "org", 34: "person",
                                         41: "person", 42: "person", 43: "person", 48: "org"},  # Inventor design tracking
}
NAME_HINTS = ("author", "drawn", "design", "engineer", "checked", "approv", "company", "customer",
              "client", "vendor", "supplier", "manager", "owner", "creator", "dessin", "zeichner",
              "konstrukt", "gezeichnet", "bearbeiter", "firma", "societe", "responsable", "saved by")


def _names_nobody(value):
    """is_generic, except the Windows default account: as an author or saved-by value it is
    the account that saved the file, so it gets a pseudonym like any other account."""
    return is_generic(value) and value.strip().lower() != "administrator"


def propset_names(buf, scanner, collect=None):
    """Pseudonymize the person/organisation string properties of an OLE property set
    stream, in place. Returns the number of values replaced; the original values are
    appended to collect."""
    done = 0
    try:
        nsets = struct.unpack_from("<I", buf, 24)[0]
        for k in range(min(nsets, 4)):
            fmtid = bytes(buf[28 + 20 * k:44 + 20 * k]).hex()
            base = struct.unpack_from("<I", buf, 44 + 20 * k)[0]
            nprops = struct.unpack_from("<I", buf, base + 4)[0]
            if nprops > 4000:
                continue
            index = [struct.unpack_from("<II", buf, base + 8 + 8 * j) for j in range(nprops)]
            wanted = dict(PROPSET_NAMES.get(fmtid, {}))
            for pid, off in index:
                if pid == 0:  # dictionary of custom property names
                    pos = base + off
                    count = struct.unpack_from("<I", buf, pos)[0]
                    pos += 4
                    for _ in range(min(count, 2000)):
                        cid, length = struct.unpack_from("<II", buf, pos)
                        raw = bytes(buf[pos + 8:pos + 8 + length])
                        name = raw.decode("latin-1").replace("\x00", "").lower()
                        step = length
                        if len(raw) >= 2 and raw[1:2] == b"\x00" and length * 2 + pos + 8 <= len(buf):
                            wide_raw = bytes(buf[pos + 8:pos + 8 + 2 * length])
                            name = wide_raw.decode("utf-16le", "replace").replace("\x00", "").lower()
                            step = 2 * length + (-2 * length) % 4
                        pos += 8 + step
                        if any(h in name for h in NAME_HINTS):
                            wanted[cid] = "org" if any(h in name for h in ("company", "customer", "client", "vendor", "supplier", "firma", "societe")) else "person"
            for pid, off in index:
                kind = wanted.get(pid)
                if not kind:
                    continue
                pos = base + off
                vt, count = struct.unpack_from("<II", buf, pos)
                vt &= 0xFFFF
                if vt == 30:      # VT_LPSTR: byte count, bytes
                    a, b = pos + 8, pos + 8 + count
                elif vt == 31:    # VT_LPWSTR: character count, UTF-16
                    a, b = pos + 8, pos + 8 + 2 * count
                else:
                    continue
                if b > len(buf) or count > 4000:
                    continue
                raw = bytes(buf[a:b])
                wide_text = vt == 31 or (count > 3 and raw[1:2] == b"\x00" and raw[3:4] == b"\x00")
                text = raw.decode("utf-16le", "replace") if wide_text else raw.decode("latin-1")
                value = text.rstrip("\x00")
                if _names_nobody(value) or "\\" in value:
                    continue
                new = scanner.pseudo.span(value) + text[len(value):]
                new_raw = new.encode("utf-16le") if wide_text else new.encode("latin-1", "replace")
                if len(new_raw) == len(raw) and new_raw != raw:
                    buf[a:b] = new_raw
                    if collect is None:
                        scanner.found.append((kind, value))
                    else:
                        collect.extend(p.strip() for p in value.replace("\\", "\n").split("\n"))
                    done += 1
    except (struct.error, IndexError, ValueError):
        pass
    return done


def _own_names(scanner, mark):
    """Names this file itself revealed (author, saved-by, company, user folders)."""
    names = set()
    for kind, value in scanner.found[mark:]:
        if kind in ("person", "org", "user"):
            for piece in value.replace("\\", "\n").split("\n"):
                piece = piece.strip()
                if len(piece) >= 3 and not _names_nobody(piece):
                    names.add(piece)
    return name_matcher(sorted(names, key=len, reverse=True))


IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"BM", b"GIF8", b"II*\x00", b"MM\x00*")


def _is_picture(name, data):
    """Preview and thumbnail streams: pictures, never scanned (they stay byte-identical)."""
    low = name.lower()
    return "preview" in low or "thumbnail" in low or low.endswith(("png", "bmp", "jpg", "jpeg")) \
        or data[:4].startswith(IMAGE_MAGIC) or data[:3] == b"\xff\xd8\xff"


def _zlib_tail(data):
    """(offset, inflated bytes) when data is a short header followed by one zlib stream
    that runs to its end (SolidWorks *__ZLB, Solid Edge viewer info); else None."""
    for off in range(0, min(17, len(data) - 2)):
        if data[off] == 0x78 and data[off + 1] in (0x01, 0x5E, 0x9C, 0xDA):
            d = zlib.decompressobj()
            try:
                raw = d.decompress(bytes(data[off:]))
            except zlib.error:
                continue
            if d.eof and not d.unused_data and len(raw) >= 16:
                return off, raw
    return None


SW_ZLB_MAGIC = b"#\x1d\xd5q"


def _sw_zlb(data):
    """(block sizes, inflated bytes) of a SolidWorks *__ZLB stream: a 16-byte id, then
    blocks of [u32 inflated size][u32 zlib size][zlib data], then 8 zero bytes; else None."""
    if len(data) < 32 or data[:4] != SW_ZLB_MAGIC:
        return None
    pos, sizes, out = 16, [], []
    while pos + 8 <= len(data):
        usize, csize = struct.unpack_from("<II", data, pos)
        if usize == 0 and csize == 0:
            return (sizes, b"".join(out)) if sizes else None
        start = pos + 8
        if csize == 0 or start + csize > len(data):
            return None
        try:
            raw = zlib.decompress(bytes(data[start:start + csize]))
        except zlib.error:
            return None
        if len(raw) != usize:
            return None
        sizes.append(usize)
        out.append(raw)
        pos = start + csize
    return None


def _sw_zlb_pack(data, sizes, buf):
    """buf (same length as the inflated stream) packed in the *__ZLB layout into exactly
    len(data) bytes: real block sizes, then the 8-byte end mark, zero padded; None if it
    does not fit."""
    out, off = bytearray(data[:16]), 0
    for usize in sizes:
        piece = bytes(buf[off:off + usize])
        off += usize
        packed = swnative.deflate_small(piece, raw=False)
        out += struct.pack("<II", usize, len(packed)) + packed
    out += b"\x00" * 8
    if len(out) > len(data):
        return None
    return bytes(out) + b"\x00" * (len(data) - len(out))


def _deflate_fit(buf, room, raw_deflate):
    """buf compressed to at most room bytes (zero padded), or None if it does not fit."""
    best = None
    for level, strategy in ((9, zlib.Z_DEFAULT_STRATEGY), (6, zlib.Z_DEFAULT_STRATEGY), (9, zlib.Z_FILTERED)):
        c = zlib.compressobj(level, zlib.DEFLATED, -15 if raw_deflate else 15, 9, strategy)
        packed = c.compress(bytes(buf)) + c.flush()
        if best is None or len(packed) < len(best):
            best = packed
    if len(best) <= room:
        return best + b"\x00" * (room - len(best))
    return swnative.deflate_fit(bytes(buf), room, raw_deflate)  # 7-Zip's encoder, if smaller


# DEID_NATIVE_SCOPE=strict: streams of native CAD files that are not metadata are never
# changed (no names replaced in them at all); default: they get the file's own names only
NATIVE_SCOPE = os.environ.get("DEID_NATIVE_SCOPE", "")
METADATA_PART = re.compile(r"(?i)summaryinformation|docprops/|isolidworksinformation|customproperties|"
                           r"header2|biography|keywords|swdoccontentmgr|^\x05")


def _metadata_part(name, raw):
    """Streams and chunks that hold document properties, save history and keywords (the
    places where a CAD file names people and organisations), as opposed to model data."""
    return bool(METADATA_PART.search(name)) or raw[:4] == b"\xfe\xff\x00\x00"


def _rewrite_parts(parts, ctx, sw=False):
    """parts: list of [name, original bytes, bytearray]. Two passes over all of them:
    property sets and the scanner first, then every name the file revealed about itself
    (author, last saved by, user folders) wherever it appears. Returns (count, matcher)."""
    total = 0
    mark = len(ctx.binary.found)
    for name, raw, buf in parts:
        if raw[:4] == b"\xfe\xff\x00\x00" or name[:1] == "\x05":
            total += propset_names(buf, ctx.binary)
        if _metadata_part(name, raw):
            total += ctx.binary.scan(buf, first=False)
        else:
            # model, feature, configuration and display data: the corpus dictionary is not
            # applied (it renamed configurations, features and vendor part names); the scan
            # only collects the accounts, field values and user folders it sees, and those
            # are replaced below like every other name the file reveals about itself
            ctx.binary.scan(bytearray(buf), first=False)
    matcher = _own_names(ctx.binary, mark)
    if NATIVE_SCOPE == "strict" or (NATIVE_SCOPE == "strict-sw" and sw):
        # model data stays byte-identical; names only in metadata ("strict-sw": newer
        # SolidWorks containers only; OLE files keep the full rewrite, whose account slots
        # in model streams the OLE checks count)
        parts = [p for p in parts if _metadata_part(p[0], p[1])]
    for name, raw, buf in parts:
        total += replace_names(buf, matcher, ctx.binary.pseudo)
    total += swnative.fix_parts(parts, ctx)  # save histories, account slots, nested parts
    return total, _own_names(ctx.binary, mark)


def ole_file(src, dst, ctx):
    """OLE compound file (older SolidWorks, Inventor, Solid Edge, NX): every stream is
    scanned and written back at the same size, so the container structure is untouched.
    zlib-compressed streams are inflated, rewritten and deflated back into their space.
    Names the file reveals about itself (author, last saved by, user folders) are replaced
    in all of its streams and in the unused space between them. Preview pictures are
    not touched. Returns (replacements, streams that could not be rewritten in place)."""
    import olefile

    shutil.copyfile(src, dst)
    total = skipped = 0
    try:
        ole = olefile.OleFileIO(dst, write_mode=True)
        try:
            streams, parts = [], []
            for entry in ole.listdir(streams=True, storages=False):
                try:
                    data = ole.openstream(entry).read()
                except Exception:
                    continue
                if len(data) < 8 or _is_picture(entry[-1], data):
                    continue
                zlb = _sw_zlb(data)
                packed = None if zlb else _zlib_tail(data)
                raw = zlb[1] if zlb else (packed[1] if packed else data)
                parts.append([entry[-1], raw, bytearray(raw)])
                streams.append((entry, data, zlb[0] if zlb else (packed[0] if packed else None)))
            total, matcher = _rewrite_parts(parts, ctx)
            for (entry, data, offset), (name, raw, buf) in zip(streams, parts):
                if buf == raw or len(buf) != len(raw):
                    continue
                if offset is None:
                    ole.write_stream(entry, bytes(buf))
                    continue
                if isinstance(offset, list):  # SolidWorks *__ZLB block list
                    fitted = _sw_zlb_pack(data, offset, buf)
                    if fitted is None:
                        skipped += 1
                    else:
                        ole.write_stream(entry, fitted)
                    continue
                fitted = _deflate_fit(buf, len(data) - offset, False)
                if fitted is None:
                    skipped += 1
                else:
                    ole.write_stream(entry, data[:offset] + fitted)
            # unused sectors of the container (old copies of streams); never the directory,
            # the allocation tables or a live stream
            size, free, run = ole.sector_size, [], None
            for i, nxt in enumerate(ole.fat):
                if nxt == 0xFFFFFFFF:
                    if run is not None and run[1] == (i + 1) * size:
                        run[1] = (i + 2) * size
                    else:
                        run = [(i + 1) * size, (i + 2) * size]
                        free.append(run)
        finally:
            ole.close()
        if matcher is not None and free:
            with open(dst, "r+b") as f:
                limit = os.path.getsize(dst)
                for a, b in free:
                    if a >= limit:
                        break
                    f.seek(a)
                    block = bytearray(f.read(min(b, limit) - a))
                    k = replace_names(block, matcher, ctx.binary.pseudo)
                    if k:
                        f.seek(a)
                        f.write(block)
                        total += k
        with open(dst, "r+b") as f:
            image = bytearray(f.read())
            k = swnative.fix_slack(image, ctx, matcher)
            if k:
                f.seek(0)
                f.write(image)
                total += k
            if len(image) > os.path.getsize(src):  # olefile can pad the last sector
                f.truncate(os.path.getsize(src))
        if matcher is not None:
            total += _ole_leftovers(dst, matcher, ctx)
    except Exception:
        os.remove(dst)
        raise
    if not total:
        os.remove(dst)
        place(src, dst)
    return total, skipped


def _ole_stream_bytes(path):
    import olefile
    ole = olefile.OleFileIO(path)
    try:
        return [(e, ole.openstream(e).read()) for e in ole.listdir(streams=True, storages=False)]
    finally:
        ole.close()


def _ole_leftovers(dst, matcher, ctx):
    """The file's own names that are still in its bytes after every stream was rewritten
    sit outside live stream data (free mini sectors, sector tails, old stream copies).
    Replace them there; kept only if every stream still reads back byte-identical."""
    with open(dst, "rb") as f:
        data = bytearray(f.read())
    before = bytes(data)
    k = replace_names(data, matcher, ctx.binary.pseudo)
    if not k:
        return 0
    streams = _ole_stream_bytes(dst)
    with open(dst, "wb") as f:
        f.write(data)
    try:
        same = _ole_stream_bytes(dst) == streams
    except Exception:
        same = False
    if not same:
        with open(dst, "wb") as f:
            f.write(before)
        return 0
    return k


def sw_container(src, dst, ctx):
    """Newer SolidWorks files: a sequence of raw-deflate chunks. Each chunk is inflated,
    scanned, and deflated again into exactly the space it had (padded if smaller); a
    chunk that would need more space is left as it was. Preview pictures are not touched.
    Returns (replacements, skipped)."""
    with open(src, "rb") as f:
        data = bytearray(f.read())
    chunks, parts = [], []
    pos = data.find(SW_MARK)
    while pos >= 0:
        nxt = pos + 6
        if pos + 26 <= len(data):
            csize, usize, nlen = struct.unpack_from("<III", data, pos + 14)
            start = pos + 26 + nlen
            if nlen < 512 and 0 < csize and start + csize <= len(data) and usize < (1 << 31):
                try:
                    raw = zlib.decompressobj(-15).decompress(bytes(data[start:start + csize]))
                except zlib.error:
                    raw = None
                if raw is not None and len(raw) == usize:
                    nxt = start + csize
                    name = bytes(((c << 4) | (c >> 4)) & 0xFF for c in data[pos + 26:start]).decode("latin-1")
                    if len(raw) >= 8 and not _is_picture(name, raw):
                        chunks.append((start, csize, pos))
                        parts.append([name, raw, bytearray(raw)])
        pos = data.find(SW_MARK, nxt)
    total, _matcher = _rewrite_parts(parts, ctx, sw=True)
    skipped, resealed = 0, []
    for (start, csize, pos), (name, raw, buf) in zip(chunks, parts):
        if buf == raw or len(buf) != len(raw):
            continue
        fitted = _deflate_fit(buf, csize, True)
        if fitted is None:
            skipped += 1
            continue
        data[start:start + csize] = fitted
        # the chunk header is a ZIP local header without its signature: CRC-32 of the
        # inflated data at +10 (kept only if the source followed that layout)
        old_crc = struct.unpack_from("<I", data, pos + 10)[0]
        if old_crc == zlib.crc32(raw) & 0xFFFFFFFF:
            new_crc = zlib.crc32(bytes(buf)) & 0xFFFFFFFF
            struct.pack_into("<I", data, pos + 10, new_crc)
            resealed.append((pos, old_crc, new_crc, csize, len(raw)))
    if resealed:
        # directory entries (ZIP central-directory layout, same 6-byte mark): CRC at +10,
        # sizes at +14 / +18, and at +36 the position of the chunk header minus 12
        heads = {pos for pos, *_ in resealed}
        wanted = {(pos - 12, old, csize, usize): new for pos, old, new, csize, usize in resealed}
        q = data.find(SW_MARK)
        while q >= 0:
            if q not in heads and q + 40 <= len(data):
                crc, csize, usize = struct.unpack_from("<III", data, q + 10)
                new = wanted.get((struct.unpack_from("<I", data, q + 36)[0], crc, csize, usize))
                if new is not None:
                    struct.pack_into("<I", data, q + 10, new)
            q = data.find(SW_MARK, q + 6)
    if total:
        with open(dst, "wb") as f:
            f.write(data)
    else:
        place(src, dst)
    return total, skipped


# -- image metadata --------------------------------------------------------------

TIFF_NAME_TAGS = {315: "person", 316: "user", 40093: "person", 42032: "person"}  # Artist, HostComputer, XPAuthor, CameraOwnerName
TIFF_TEXT_TAGS = (269, 270, 285, 33432, 37510, 40091, 40092, 40094, 40095)       # names, description, copyright, comments
TIFF_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def _name_bytes(data, a, b, ctx, kind):
    """Pseudonymize a name stored at data[a:b] (8-bit or UTF-16LE), same length."""
    raw = bytes(data[a:b])
    is_wide = len(raw) > 3 and raw[1:2] == b"\x00" and raw[3:4] == b"\x00"
    text = raw.decode("utf-16le", "replace") if is_wide else raw.decode("latin-1")
    value = text.rstrip("\x00 ")
    if is_generic(value):
        return 0
    new = ctx.text.pseudo.span(value) + text[len(value):]
    packed = new.encode("utf-16le") if is_wide else new.encode("latin-1", "replace")
    if len(packed) != len(raw) or packed == raw:
        return 0
    data[a:b] = packed
    ctx.text.found.append((kind, value))
    return 1


def _scan_slice(data, a, b, scanner):
    part = bytearray(data[a:b])
    n = scanner.scan(part, first=False)
    if n:
        data[a:b] = part
    return n


def tiff_names(data, base, ctx, end=None):
    """Walk the IFDs of a TIFF structure at data[base:]: pseudonymize artist / owner /
    host fields, scan descriptive text, zero GPS positions. Returns replacements."""
    end = len(data) if end is None else end
    order = {b"II": "<", b"MM": ">"}.get(bytes(data[base:base + 2]))
    if not order:
        return 0
    total, seen = 0, set()
    todo = [(struct.unpack_from(order + "I", data, base + 4)[0], "main")]
    while todo and len(seen) < 64:
        off, which = todo.pop()
        pos = base + off
        if off == 0 or off in seen or pos + 2 > end:
            continue
        seen.add(off)
        count = struct.unpack_from(order + "H", data, pos)[0]
        for i in range(count):
            e = pos + 2 + 12 * i
            if e + 12 > end:
                break
            tag, typ, n = struct.unpack_from(order + "HHI", data, e)
            size = TIFF_SIZES.get(typ, 0) * n
            a = e + 8 if size <= 4 else base + struct.unpack_from(order + "I", data, e + 8)[0]
            if size == 0 or a + size > end:
                continue
            if which == "gps":
                if tag in (2, 4, 6, 7, 29) and any(data[a:a + size]):
                    data[a:a + size] = bytes(size)
                    total += 1
            elif tag in (34665, 34853) and typ in (4, 13):
                todo.append((struct.unpack_from(order + "I", data, e + 8)[0], "gps" if tag == 34853 else "exif"))
            elif tag in TIFF_NAME_TAGS and typ in (1, 2, 7):
                total += _name_bytes(data, a, a + size, ctx, TIFF_NAME_TAGS[tag])
            elif tag in TIFF_TEXT_TAGS and typ in (1, 2, 7):
                total += _scan_slice(data, a, a + size, ctx.binary)
            elif tag == 700 and typ in (1, 7):  # XMP packet
                total += _scan_slice(data, a, a + size, ctx.text)
        nxt = pos + 2 + 12 * count
        if which == "main" and nxt + 4 <= end:
            todo.append((struct.unpack_from(order + "I", data, nxt)[0], "main"))
    return total


def image_file(src, dst, ctx):
    """JPEG / PNG / TIFF: metadata only (EXIF, XMP, IPTC by-line, PNG text chunks,
    comments). Pixels are not touched; PNG chunk checksums are recomputed."""
    with open(src, "rb") as f:
        data = bytearray(f.read())
    total = 0
    if data[:2] == b"\xff\xd8":
        pos = 2
        while pos + 4 <= len(data) and data[pos] == 0xFF and data[pos + 1] not in (0xDA, 0xD9):
            length = struct.unpack_from(">H", data, pos + 2)[0]
            a, b = pos + 4, pos + 2 + length
            if b > len(data) or length < 2:
                break
            marker = data[pos + 1]
            if marker == 0xE1 and data[a:a + 6] == b"Exif\x00\x00":
                total += tiff_names(data, a + 6, ctx, b)
            elif marker == 0xE1 or marker == 0xFE:      # XMP packet, comment
                total += _scan_slice(data, a, b, ctx.text)
            elif marker == 0xED:                         # Photoshop / IPTC
                q = data.find(b"\x1c\x02", a, b)
                while 0 <= q < b - 5:
                    rec, n = data[q + 2], struct.unpack_from(">H", data, q + 3)[0]
                    if q + 5 + n > b:
                        break
                    if rec in (80, 110, 115, 122):       # by-line, credit, source, writer
                        total += _name_bytes(data, q + 5, q + 5 + n, ctx, "person")
                    elif rec in (5, 105, 116, 120):      # title, headline, copyright, caption
                        total += _scan_slice(data, q + 5, q + 5 + n, ctx.binary)
                    q = data.find(b"\x1c\x02", q + 5 + n, b)
            pos = b
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        pos = 8
        while pos + 12 <= len(data):
            n = struct.unpack_from(">I", data, pos)[0]
            kind = bytes(data[pos + 4:pos + 8])
            a, b = pos + 8, pos + 8 + n
            if b + 4 > len(data):
                break
            k = 0
            if kind == b"tEXt":
                z = data.find(b"\x00", a, b)
                if z > 0 and bytes(data[a:z]).lower() in (b"author", b"artist"):
                    k = _name_bytes(data, z + 1, b, ctx, "person")
                elif z > 0:
                    k = _scan_slice(data, z + 1, b, ctx.text)
            elif kind == b"iTXt":
                k = _scan_slice(data, a, b, ctx.text)
            elif kind == b"eXIf":
                k = tiff_names(data, a, ctx, b)
            if k:
                struct.pack_into(">I", data, b, zlib.crc32(bytes(data[pos + 4:b])) & 0xFFFFFFFF)
                total += k
            if kind == b"IEND":
                break
            pos = b + 4
    elif data[:4] in (b"II*\x00", b"MM\x00*"):
        total += tiff_names(data, 0, ctx)
    if total:
        with open(dst, "wb") as f:
            f.write(data)
    else:
        place(src, dst)
    return total


# -- text inside pictures (OCR, optional module) ----------------------------------

def _previews():
    """The OCR module, or None when it or the tesseract program is not available."""
    try:
        from . import previews
        return previews if previews.TESSERACT else None
    except Exception:
        return None


def _natives2():
    """The module for Creo, JT, CATIA files and 7z / RAR archives, or None."""
    try:
        from . import natives2
        return natives2
    except Exception:
        return None


def ocr_pdf(dst, ctx):
    """Replace identifying text inside the page images of the PDF at dst. Reading every
    page image of every scanned document takes hours on the full corpus, so this step
    runs only when DEID_OCR_PDF=1 is set; image files are always read."""
    pv = _previews()
    if pv is None:
        return 0
    k = 0
    if os.environ.get("DEID_OCR_RENDER") == "1":
        k += ocr_step(pv.pdf_render, dst, ctx)
    if os.environ.get("DEID_OCR_PDF") == "1":
        k += ocr_step(pv.pdf_images, dst, ctx)
    return k


def ocr_step(step, dst, ctx):
    """Run step(path, dst, ctx) on the PDF at dst; keep the original when it fails."""
    tmp = dst + ".pvtmp"
    os.replace(dst, tmp)
    try:
        k = step(tmp, dst, ctx)
    except Exception:
        k = 0
    if k and os.path.isfile(dst):
        os.remove(tmp)
        return k
    for leftover in (dst, dst + ".part"):
        if os.path.lexists(leftover):
            os.remove(leftover)
    os.replace(tmp, dst)
    return 0


def ocr_image(src, dst, ctx):
    """Replace identifying text inside the image at dst (src names the original)."""
    pv = _previews()
    if pv is None:
        return 0
    try:
        with open(dst, "rb") as f:
            data = f.read()
        logo, label = pv._logo_label(src, ctx)
        new, k = pv.image_bytes(data, ctx, whole=logo, label=label)
    except Exception:
        return 0
    if not k or not new:
        return 0
    os.remove(dst)  # dst may be a hard link to the input: never write through it
    with open(dst, "wb") as f:
        f.write(new)
    return k


def magic(path):
    with open(path, "rb") as f:
        head = f.read(1024)
    if head.startswith(OLE_MAGIC):
        return "ole"
    if b"%PDF-" in head:
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head.startswith(b"\x1f\x9d"):
        return "compress"
    if SW_MARK in head and os.path.splitext(path)[1].lower() in SW_EXT:
        return "sw"
    return ""


_EDGE = re.compile(r"^[^A-Za-z0-9]*|[^A-Za-z0-9]*$")


def _collect(words, fix, total, changed, pair, runs):
    """Feed one sequence of words to the rewriting rules; record which words change."""
    if not words:
        return
    new_words = fix(" ".join(words)).split(" ")
    if len(new_words) != len(words):
        return
    run = []
    for old, new in zip(words, new_words):
        key = _EDGE.sub("", old).lower()
        total[key] += 1
        if old != new:
            changed[key] += 1
            lead = len(old) - len(old.lstrip("".join(c for c in old if not c.isalnum())))
            core_old, core_new = _EDGE.sub("", old), new[lead:lead + len(_EDGE.sub("", old))]
            pair[key] = (core_old, core_new)
            run.append((old, new, key))
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)


def pdf_mapping(path, ctx, fix):
    """{original text: pseudonym} for one PDF, from its page words, bookmarks, annotations,
    form fields and link targets. A word that the rules change everywhere in the document
    is one entry; a word they change only inside a longer name is entered with that name."""
    import collections
    import pymupdf as fitz

    total, changed, pair, runs = collections.Counter(), collections.Counter(), {}, []
    mapping = {}
    with fitz.open(path) as doc:
        if doc.needs_pass:
            return {}
        strings = [title for _lvl, title, _page in doc.get_toc(simple=True)]
        for page in doc:
            _collect([w[4] for w in page.get_text("words")], fix, total, changed, pair, runs)
            for annot in page.annots() or ():
                info = annot.info
                strings += [info.get("content", ""), info.get("subject", "")]
                author = info.get("title", "")
                if author and not is_generic(author):
                    mapping[author] = ctx.text.pseudo.span(author)
            strings += [link["uri"] for link in page.get_links() if link.get("uri")]
            strings += [str(w.field_value) for w in page.widgets() or () if w.field_value]
    for text in strings:
        if text:
            _collect(text.split(), fix, total, changed, pair, runs)
    for key, (old, new) in pair.items():
        if key and changed[key] == total[key] and old != new:
            mapping[old] = new
    for run in runs:
        if any(changed[key] != total[key] or not key for _o, _n, key in run):
            old, new = " ".join(o for o, _n, _k in run), " ".join(n for _o, n, _k in run)
            lead = len(old) - len(old.lstrip("".join(c for c in old if not c.isalnum() and c != " ")))
            core = _EDGE.sub("", old)
            if core and len(old) == len(new):
                mapping[core] = new[lead:lead + len(core)]
    return {k: v for k, v in mapping.items() if k and k != v and len(k) >= 2}


def _already(old, new, produced):
    """True when every word the rules would change in old is itself a pseudonym written by the
    first pass (a host or e-mail name looks like one again): changing it would give that
    identity a second pseudonym."""
    a, b = re.findall(r"[A-Za-z0-9]+", old), re.findall(r"[A-Za-z0-9]+", new)
    return len(a) == len(b) and all(x == y or x.lower() in produced for x, y in zip(a, b))


def pdf_extras(doc, ctx, fix, produced=frozenset()):
    """Bookmarks, annotation authors and notes. Returns replacements."""
    total = 0
    if produced:
        rules = fix
        fix = lambda text: text if _already(text, rules(text), produced) else rules(text)  # noqa: E731
    try:
        toc = doc.get_toc(simple=True)
        new_toc = [[lvl, fix(title), page] for lvl, title, page in toc]
        if new_toc != toc:
            doc.set_toc(new_toc)
            total += 1
    except Exception:
        pass
    for page in doc:
        try:
            for annot in page.annots() or ():
                info = annot.info
                title, content = info.get("title", ""), info.get("content", "")
                new_title = ctx.text.pseudo.span(title) if title and not is_generic(title) else title
                new_content = fix(content) if content else content
                if (new_title, new_content) != (title, content):
                    annot.set_info(title=new_title, content=new_content)
                    total += 1
        except Exception:
            continue
    return total


def _link_key(link):
    r = link.get("from")
    return (link.get("kind"), round(r.x0), round(r.y0), round(r.x1), round(r.y1)) if r is not None else None


def pdf_text(path, ctx, fix, produced=frozenset()):
    """Replace identifiers still readable in the page text of the PDF at path (rewritten
    in place): each affected word is removed and its same-length pseudonym is drawn in its
    place. Drawings, images, annotations and links are kept. Returns the number of words."""
    import pymupdf as fitz

    total = 0
    doc = fitz.open(path)
    try:
        for page in doc:
            if page.rotation:
                page.remove_rotation()  # same appearance, plain coordinates
            words = page.get_text("words")
            if not words:
                continue
            joined = " ".join(w[4] for w in words)
            new_words = fix(joined).split(" ")
            if len(new_words) != len(words):
                continue
            _join_split_words(words, new_words, ctx)
            notes = [a.rect for a in page.annots() or ()]
            jobs = []
            for w, new in zip(words, new_words):
                x0, y0, x1, y1 = w[:4]
                if w[4] == new or x1 - x0 < 1 or y1 - y0 < 1:
                    continue
                if produced and _already(w[4], new, produced):
                    continue  # written by the content-stream pass: not a second pseudonym
                middle = fitz.Point((x0 + x1) / 2, (y0 + y1) / 2)
                if any(middle in r for r in notes):
                    continue  # text of an annotation: handled as annotation content
                vertical = y1 - y0 > 2.2 * (x1 - x0) and len(new) > 2
                # a thin band through the middle of the word: removes its glyphs only
                if vertical:
                    band = fitz.Rect((x0 + x1) / 2 - 0.4, y0 + 0.5, (x0 + x1) / 2 + 0.4, y1 - 0.5)
                else:
                    band = fitz.Rect(x0 + 0.5, (y0 + y1) / 2 - 0.4, x1 - 0.5, (y0 + y1) / 2 + 0.4)
                page.add_redact_annot(band, fill=False, cross_out=False)
                jobs.append((x0, y0, x1, y1, new, vertical))
            if not jobs:
                continue
            links = page.get_links()
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
            kept = {_link_key(link) for link in page.get_links()}
            for link in links:  # links under a replaced word are removed with it: put them back
                if _link_key(link) not in kept:
                    try:
                        page.insert_link({k: v for k, v in link.items() if k not in ("xref", "id")})
                    except Exception:
                        pass
            for x0, y0, x1, y1, new, vertical in jobs:
                length, thick = (y1 - y0, x1 - x0) if vertical else (x1 - x0, y1 - y0)
                size = min(0.75 * thick, 48.0)
                wide = fitz.get_text_length(new, fontname="helv", fontsize=size)
                if wide > length:
                    size *= length / wide
                size = max(size, 2.0)
                if vertical:
                    page.insert_text((x1 - 0.22 * thick, y1), new, fontsize=size, fontname="helv", rotate=90)
                else:
                    page.insert_text((x0, y1 - 0.22 * thick), new, fontsize=size, fontname="helv")
            total += len(jobs)
        total += pdf_extras(doc, ctx, fix, produced)
        if total:
            doc.save(path + ".part", garbage=1, deflate=True, no_new_id=True)
    finally:
        doc.close()
    if total:
        os.replace(path + ".part", path)
    return total


def _join_split_words(words, new_words, ctx):
    """DEID_PDF_INNER=1: an identifier split over two adjacent words of one line ("www.acme"
    "labs.com" read as www.acmelabs.com) is replaced in both, its pseudonym split at the same
    place. Only words the rules left unchanged; edits new_words in place."""
    rx = _inner_rx(ctx)
    if rx is None:
        return
    pseudo = ctx.text.pseudo
    for i in range(len(words) - 1):
        a, b = words[i], words[i + 1]
        h = a[3] - a[1]
        if (new_words[i] != a[4] or new_words[i + 1] != b[4] or h <= 0
                or abs(a[1] - b[1]) > 0.3 * h or abs(a[3] - b[3]) > 0.3 * h or not -0.2 * h < b[0] - a[2] < 0.8 * h):
            continue  # not side by side on one line, or already rewritten
        s, cut = a[4] + b[4], len(a[4])
        out, last = [], 0
        for m in rx.finditer(s):
            if m.start() < cut < m.end():
                new = _inner_sub(m, pseudo)
                if len(new) == len(m.group()):
                    out += [s[last:m.start()], new]
                    last = m.end()
        if out:
            t = "".join(out) + s[last:]
            new_words[i], new_words[i + 1] = t[:cut], t[cut:]


def _same_pages(a, b):
    import pymupdf as fitz
    try:
        with fitz.open(a) as x, fitz.open(b) as y:
            return len(x) == len(y)
    except Exception:
        return False


def pdf_content(path, ctx, fix):
    """Rewrite the text of the PDF at path (a private working file). First inside the
    content streams themselves (pdftext: reading order, fonts, links and annotations are
    kept); whatever is still readable afterwards is removed and redrawn."""
    total = 0
    mapping = dict(getattr(ctx, "global_map", None) or {})
    mapping.update(pdf_mapping(path, ctx, fix))  # the document's own findings win
    produced = set()
    if mapping:
        tmp = path + ".ptx"
        try:
            from . import pdftext
            k = pdftext.replace(path, tmp, mapping)
            if k and _same_pages(path, tmp):
                os.replace(tmp, path)
                total += k
                produced = {t.lower() for old, new in mapping.items()
                            for s, t in zip(re.findall(r"[A-Za-z0-9]+", old), re.findall(r"[A-Za-z0-9]+", new)) if s != t}
        except Exception:
            pass
        if os.path.lexists(tmp):
            os.remove(tmp)
    return total + pdf_text(path, ctx, fix, produced)


def pdf_file(src, dst, ctx, depth):
    fix = PdfFix(ctx, src)
    n = pdf_metadata(src, dst, ctx.text, ctx, depth, fix)
    work = dst
    if os.path.samefile(src, dst):  # unchanged so far: work on a private copy
        work = dst + ".copy"
        shutil.copyfile(src, work)
    try:
        k = pdf_content(work, ctx, fix)
    except (ImportError, AttributeError):
        if work != dst:
            os.remove(work)
        raise
    except Exception:
        k = 0
    if work != dst:
        if k:
            os.remove(dst)
            os.replace(work, dst)
        else:
            os.remove(work)
    for leftover in (work + ".part", work + ".ptx"):
        if os.path.lexists(leftover):
            os.remove(leftover)
    from . import pdfpost
    total = n + k + pdfpost.finish(src, dst, fix, ctx.text.pseudo)
    if os.environ.get("DEID_PDF_OCR") == "1":  # identifiers only the rendered page shows
        from . import pdfocr
        total += pdfocr.redact(dst, ctx, fix)
    return total


def process(src, dst, ext, ctx, depth=0):
    """Write the de-identified copy of src at dst. Returns (action, replacements, defect)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.lexists(dst):
        os.remove(dst)
    size = os.path.getsize(src)
    if size == 0:
        place(src, dst)
        return "empty", 0, "empty file in the source (0 bytes); kept as is"
    scanner = ctx.text
    kind = magic(src)
    natives = _natives2() if depth < MAX_DEPTH else None
    if natives is not None:
        try:
            family = natives.sniff(src)
        except Exception:
            family = ""
        if family in ("creo", "jt", "catia", "archive", "fusion"):
            # Creo / Pro-E, JT, CATIA files and 7z / RAR archives: natives2 (it never raises)
            n = natives.handle(src, dst, ctx)
            notes = "; ".join(sorted(set(natives.NOTES)))[:300] if getattr(natives, "NOTES", None) and not n else ""
            return family, n, notes
    try:
        if ext == ".pdf" and kind == "pdf":
            n = pdf_file(src, dst, ctx, depth)
            return "pdf", n + ocr_pdf(dst, ctx), ""
        if kind == "ole":
            n, skipped = ole_file(src, dst, ctx)
            note = "%d internal block(s) could not be rewritten in place; left as in the source" % skipped if skipped else ""
            return "ole", n, note
        if kind == "zip" and depth < MAX_DEPTH and (ext == ".zip" or ext in ZIP_CONTAINERS):
            return "zip", zip_file(src, dst, ctx, ext == ".zip", depth), ""
        if kind == "gzip" and depth < MAX_DEPTH and ext in (".gz", ".tgz"):
            return gzip_file(src, dst, ctx, depth) + ("",)
        if kind == "compress" and depth < MAX_DEPTH and ext == ".z":
            return "compress", compress_file(src, dst, ctx, depth), ""
        if ext == ".tar" and depth < MAX_DEPTH:
            return "tar", tar_file(src, dst, ctx, depth), ""
        if kind == "sw":
            n, skipped = sw_container(src, dst, ctx)
            note = "%d internal block(s) could not be rewritten in place; left as in the source" % skipped if skipped else ""
            return "solidworks", n, note
    except Exception as exc:  # damaged or unsupported container: keep the bytes, report it
        for leftover in (dst, dst + ".part", dst + ".copy"):
            if os.path.lexists(leftover):
                os.remove(leftover)
        place(src, dst)
        return "unreadable", 0, "%s container could not be parsed (%s); kept as is" % (
            (ext or kind).lstrip(".").upper(), type(exc).__name__)
    if ext in IMAGE_EXT:
        try:
            n = image_file(src, dst, ctx)
        except (struct.error, IndexError, ValueError):
            if os.path.lexists(dst):
                os.remove(dst)
            place(src, dst)
            n = 0
        return "image", n + ocr_image(src, dst, ctx), ""
    if ext == ".stl":
        if looks_text(src):
            return "text", scan_stream(src, dst, scanner), ""
        return "stl-header", scan_head(src, dst, scanner, 80), ""
    if ext == ".svg" and _previews() is not None:
        try:
            return "svg", _previews().svg_file(src, dst, ctx), ""
        except Exception:
            for leftover in (dst, dst + ".part", dst + ".pvtmp"):
                if os.path.lexists(leftover):
                    os.remove(leftover)
    if ext in (".ps", ".eps", ".ai"):
        return "postscript", scan_comment_lines(src, dst, scanner), ""
    if ext in TEXT_EXT or (ext not in IMAGE_EXT and looks_text(src)):
        scanner.step, scanner.sat = ext in (".step", ".stp", ".p21"), ext == ".sat"
        try:
            return "text", scan_stream(src, dst, scanner), ""
        finally:
            scanner.step = scanner.sat = False
    if ext in RAW_BINARY_EXT:
        return "binary", scan_stream(src, dst, ctx.binary), ""
    place(src, dst)
    return "copied", 0, ""
