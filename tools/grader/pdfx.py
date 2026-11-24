"""PDF channel extraction for the local grader.

extract(path) -> dict with every channel a grader reads without OCR:
  pdftotext (-layout), PyMuPDF page text and word boxes, Info (all keys), XMP, outline,
  annotations, link URIs, form fields, attachment names and text, page count, open errors.
extract(path, deep=True, values=[(id, value)]) also reads the output the way a strict scorer does:
  pdfminer.six and pypdf text (first 40 pages), and the raw file: every byte outside streams,
  every Flate stream inflated (all objects, also those left behind by incremental updates),
  hex strings decoded, UTF-16 text with its NULs dropped. Only the ids found there are stored.
"""
import os
import re
import signal
import subprocess
import sys
import zlib

_LIB = os.path.expanduser("~/grader/pylib")  # pypdf + pdfminer.six, kept out of the pipeline venv
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.append(_LIB)
DEEP_PAGES = 20
RAW_CAP = 16 << 20
DEEP_MAX_SIZE = 40 << 20  # bigger PDFs get the raw scan but not pdfminer/pypdf (memory)
STREAM = re.compile(rb"stream\r?\n")
HEXSTR = re.compile(rb"<([0-9A-Fa-f\s]{4,})>")

MAX_PAGES = 300
WORD_PAGES = 60


def _run(cmd, timeout):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -9, ""
    except Exception:
        return -1, ""


def _info(path):
    import pikepdf
    res = {"info": {}, "xmp": "", "pikepdf_error": ""}
    try:
        with pikepdf.open(path) as pdf:
            info = pdf.trailer.get("/Info")
            if info is not None:
                for k in info.keys():
                    try:
                        res["info"][str(k)] = str(info[k])
                    except Exception:
                        pass
            m = pdf.Root.get("/Metadata")
            if m is not None:
                try:
                    res["xmp"] = m.read_bytes().decode("utf-8", "replace")
                except Exception:
                    pass
    except Exception as exc:
        res["pikepdf_error"] = type(exc).__name__
    return res


def _mupdf(path):
    import pymupdf as fitz
    fitz.TOOLS.mupdf_display_errors(False)
    res = {"pages": 0, "text": "", "words": [], "toc": [], "annots": [], "links": [], "widgets": [],
           "embfiles": [], "error": "", "rotations": [], "image_only_pages": 0}
    try:
        doc = fitz.open(path)
    except Exception as exc:
        res["error"] = type(exc).__name__
        return res
    try:
        if doc.needs_pass or doc.is_encrypted:
            res["error"] = "encrypted"
            return res
        res["pages"] = len(doc)
        try:
            res["toc"] = [t[1] for t in doc.get_toc(simple=True)]
        except Exception:
            pass
        texts = []
        for pno in range(min(len(doc), MAX_PAGES)):
            try:
                page = doc[pno]
            except Exception as exc:
                res["error"] = "page %d: %s" % (pno, type(exc).__name__)
                break
            try:
                t = page.get_text("text")
            except Exception:
                t = ""
            texts.append(t)
            res["rotations"].append(page.rotation)
            if pno < WORD_PAGES:
                try:
                    for w in page.get_text("words"):
                        res["words"].append([pno, round(w[0], 1), round(w[1], 1), round(w[2], 1), round(w[3], 1), w[4]])
                except Exception:
                    pass
            try:
                for a in page.annots() or []:
                    info = a.info or {}
                    res["annots"].append(" ".join([info.get("content", ""), info.get("title", ""), info.get("subject", "")]))
            except Exception:
                pass
            try:
                for link in page.get_links():
                    if link.get("uri"):
                        res["links"].append(link["uri"])
                    elif link.get("file"):
                        res["links"].append("file:" + str(link["file"]))
            except Exception:
                pass
            try:
                for wdg in page.widgets() or []:
                    res["widgets"].append(" ".join([wdg.field_name or "", str(wdg.field_value or ""), wdg.field_label or ""]))
            except Exception:
                pass
            if not t.strip():
                try:
                    if page.get_images():
                        res["image_only_pages"] += 1
                except Exception:
                    pass
        res["text"] = "\f".join(texts)
        try:
            for name in doc.embfile_names():
                info = doc.embfile_info(name)
                data = doc.embfile_get(name)
                txt = ""
                if data[:5] == b"%PDF-":
                    try:
                        sub = fitz.open(stream=data, filetype="pdf")
                        txt = "\n".join(p.get_text() for p in sub)
                        sub.close()
                    except Exception:
                        pass
                elif data[:200].isascii():
                    txt = data[:200000].decode("latin-1")
                res["embfiles"].append(" ".join([name, info.get("filename", ""), info.get("desc", ""), txt[:200000]]))
        except Exception:
            pass
    finally:
        doc.close()
    return res


class _Timeout(Exception):
    pass


def _alarm(_sig, _frm):
    raise _Timeout()


def _limited(fn, secs):
    old = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(secs)
    try:
        return fn()
    except _Timeout:
        return None
    except Exception:
        return None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _pdfminer(path):
    from pdfminer.high_level import extract_text
    return extract_text(path, maxpages=DEEP_PAGES)


def _pypdf(path):
    import pypdf
    r = pypdf.PdfReader(path, strict=False)
    out = []
    for k, page in enumerate(r.pages):
        if k >= DEEP_PAGES:
            break
        try:
            out.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(out)


def raw_view(path):
    """Bytes outside streams + every inflatable stream (images skipped) + decoded hex strings."""
    with open(path, "rb") as f:
        data = f.read(RAW_CAP * 4)
    parts, pos, total = [], 0, 0
    for m in STREAM.finditer(data):
        end = data.find(b"endstream", m.end())
        if end < 0:
            break
        head = data[max(pos, m.start() - 400):m.start()]
        parts.append(data[pos:m.start()])
        body = data[m.end():end]
        if b"/Image" not in head and b"DCTDecode" not in head and b"JPXDecode" not in head and total < RAW_CAP:
            try:
                inf = zlib.decompressobj().decompress(body, RAW_CAP)
            except Exception:
                inf = body if b"/Filter" not in head else b""
            parts.append(inf)
            total += len(inf)
        pos = end + 9
    parts.append(data[pos:])
    raw = b"\n".join(parts)
    hexes = []
    for m in HEXSTR.finditer(raw[:RAW_CAP]):
        h = re.sub(rb"\s", b"", m.group(1))
        if len(h) % 2 == 0 and len(h) <= 4000:
            try:
                hexes.append(bytes.fromhex(h.decode()))
            except ValueError:
                pass
    raw = raw[:RAW_CAP * 2] + b"\n" + b"\n".join(hexes)
    r16 = raw.replace(b"\x00", b"").decode("latin-1") if b"\x00" in raw else ""
    return raw.decode("latin-1"), r16


def word_rx(value):
    toks = [t for t in re.split(r"[\s\-_.]+", value) if t]
    body = r"[\s\-_.]*".join(re.escape(t) for t in toks)
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", re.I)


def extract(path, deep=False, values=()):
    rec = {"size": os.path.getsize(path) if os.path.exists(path) else -1}
    if rec["size"] < 0:
        rec["missing"] = True
        return rec
    rc, out = _run(["pdftotext", "-layout", "-enc", "UTF-8", "-l", str(MAX_PAGES), path, "-"], 120)
    rec["pdftotext"] = out
    rec["pdftotext_rc"] = rc
    rec.update(_info(path))
    try:
        rec["mupdf"] = _mupdf(path)
    except Exception as exc:  # a damaged file must not stop the grading
        rec["mupdf"] = {"error": type(exc).__name__, "text": "", "words": [], "pages": 0}
    if deep:
        if rec["size"] <= DEEP_MAX_SIZE:
            rec["pdfminer"] = (_limited(lambda: _pdfminer(path), 45) or "")[:2 << 20]
            rec["pypdf"] = (_limited(lambda: _pypdf(path), 45) or "")[:2 << 20]
        hits = []
        try:
            r8, r16 = _limited(lambda: raw_view(path), 60) or ("", "")
            for ident, value in values:
                rx = word_rx(value)
                if rx.search(r8) or rx.search(r16):
                    hits.append(ident)
        except Exception:
            pass
        rec["raw_hits"] = hits
    return rec


def channels(rec, path_str):
    """Named text channels of one extracted PDF (input or output side)."""
    m = rec.get("mupdf", {})
    return {
        "page:pdftotext": rec.get("pdftotext", ""),
        "page:mupdf": m.get("text", ""),
        "page:pdfminer": rec.get("pdfminer", ""),
        "page:pypdf": rec.get("pypdf", ""),
        "meta:info": "\n".join("%s=%s" % kv for kv in rec.get("info", {}).items()),
        "meta:xmp": rec.get("xmp", "") or "",
        "outline": "\n".join(m.get("toc", [])),
        "annots": "\n".join(m.get("annots", [])),
        "links": "\n".join(m.get("links", [])),
        "forms": "\n".join(m.get("widgets", [])),
        "attachments": "\n".join(m.get("embfiles", [])),
        "path": path_str,
    }
