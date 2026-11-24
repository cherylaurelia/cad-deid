"""Identity presence scan of the local grader (from the corpus auditor's leakscan.py): finds every
original identity of the key in a file's path, archive member names and content views (raw bytes,
archive members, OLE streams and property sets, SolidWorks chunks, PDF Info/XMP), in UTF-8,
UTF-16LE/BE and Latin-1, case-insensitive, word-bounded, with separator variants."""
import os
import re
import tempfile

import ahocorasick

import views

WORD = re.compile(r"[A-Za-z0-9]+")
ALNUM = set(b"abcdefghijklmnopqrstuvwxyz0123456789")
AUTO = None


def variants(v):
    out = {v}
    words = WORD.findall(v)
    if len(words) > 1 and v.isascii():
        for sep in (" ", "-", "_", ".", ""):
            j = sep.join(words)
            if sep or len(j) >= 6:
                out.add(j)
    return {x for x in out if len(x) >= 3 or not x.isascii()}


def build(ids):
    """ids: iterable of (id, value)."""
    auto = ahocorasick.Automaton()
    for ident, value in ids:
        for var in variants(value):
            for enc in ("utf-8", "utf-16le", "utf-16be", "latin-1"):
                try:
                    pat = var.lower().encode(enc)
                except UnicodeEncodeError:
                    continue
                if enc == "latin-1" and var.isascii():
                    continue
                key = pat.decode("latin-1")
                prev = auto.get(key) if key in auto else ()
                auto.add_word(key, prev + ((ident, enc, len(pat)),))
    auto.make_automaton()
    return auto


def _bounded(b, s, e, enc):
    if enc in ("utf-16le", "utf-16be"):
        before = b[s - 2:s] if s >= 2 else b""
        after = b[e:e + 2]
        if enc == "utf-16le":
            lb, hb, la, ha = before[0:1], before[1:2], after[0:1], after[1:2]
        else:
            hb, lb, ha, la = before[0:1], before[1:2], after[0:1], after[1:2]
        if before and hb == b"\x00" and lb and lb.lower()[0] in ALNUM:
            return False
        if after and ha == b"\x00" and la and la.lower()[0] in ALNUM:
            return False
        return True
    if s > 0 and b[s - 1:s].lower()[0] in ALNUM:
        return False
    if e < len(b) and b[e:e + 1].lower()[0] in ALNUM:
        return False
    return True


def scan_bytes(b, auto, found, cls):
    if not b:
        return
    low = b.lower().decode("latin-1")
    for end, infos in auto.iter(low):
        for ident, enc, n in infos:
            s = end - n + 1
            if ident in found and cls in found[ident]:
                continue
            if _bounded(b, s, end + 1, enc):
                found.setdefault(ident, set()).add(cls)


def scan_file(path, rel, auto, content=True):
    """{id: set(classes)} with classes path / name / content / error."""
    found = {}
    scan_bytes(rel.encode("utf-8", "surrogateescape"), auto, found, "path")
    if not content or not os.path.isfile(path):
        return found
    tmp = tempfile.mkdtemp(prefix="gscan", dir=os.environ.get("GRADER_TMP", "/tmp"))
    try:
        for loc, kind, b in views.views(path, tmp=tmp):
            if kind == "error":
                continue
            scan_bytes(b, auto, found, "name" if kind == "name" else "content")
    except Exception:
        pass
    finally:
        os.system("rm -rf '%s'" % tmp)
    return found
