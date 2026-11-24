"""Replace identifier strings inside the text of a PDF, in the content streams themselves.

replace(src, dst, mapping, notes=None) -> int
leftover(path, mapping) -> [(page number, identifier)] still readable in the page text

mapping is {identifier: pseudonym}. Every occurrence of an identifier in the page text is
rewritten where it is stored: the string operands of Tj, TJ, ' and " in page content
streams, form XObjects, tiling patterns and annotation appearances are decoded with the
font in force, matched, and written back as glyph codes of the same font. Reading order,
fonts, positions and everything that is not a replaced string stay as they were. Fonts
read: simple fonts (WinAnsi / MacRoman / Standard / Differences, Type 3), composite fonts
with Identity-H/V (ToUnicode CMap, /ToUnicode /Identity-H, glyph order of fonts that are
not embedded, Adobe CJK collections; without any of these both the cmap of the embedded
TrueType program and the code-as-character reading that MuPDF reports are searched),
UCS2 / UTF16 CMaps and the GBK / Big5 / Shift-JIS / KSC code pages; a font set through an
extended graphics state counts too. A ToUnicode CMap wins when present.

Also replaced: /ActualText of marked content (inline, or on the structure element of the
sequence's MCID, which is what text extraction then reports) is kept in step with its
glyphs; other strings of marked-content property lists; annotation /Contents /T /RC
/Subj, outline titles, form field values, structure-tree /Alt /E /Title and link URIs.
The document information dictionary and XMP are left to the metadata step.

Matching ignores case (the pseudonym takes the case pattern of the text it replaces). Words
are told apart the way MuPDF's text extraction does it, from the glyph positions, so a
name split over TJ kerning, several Tj operators or single positioned glyphs is found, and
an identifier never matches inside a longer word or number; glyphs shown twice in the same
place (simulated bold) count once and both copies are rewritten. Identifiers that overlap
("John Smith", "Smith Ltd") are replaced together; "J. DOE" also covers "J DOE", and a
run-together name of 8+ characters ("northtoolworks") also covers its capitalised words
("North Tool Works"). An identifier that occurs nowhere as
given is searched once more with the accented letters of the page standing for any
character: in some files text extraction misreads those letters, so a name taken from
extracted text differs from the page in exactly those places.

A subset font only holds the glyphs its document uses. A code counts as available when the
same document already shows it with that font (or the font is not embedded and has a
width for it). When a pseudonym needs a glyph the font does not have, the word is written
with the standard font closest in style (Helvetica, Times or Courier, bold / italic kept)
at the same size, in the same place of the same text object; nothing is redacted or drawn
over. A new word is scaled horizontally (Tz) to the width of the word it replaces, so the
rest of the line keeps its place and word spacing, and text that is clipped piece by piece
(Word) stays inside its clip. A word whose glyphs are placed one by one (Inventor) is
written as a whole where it starts.

Returns the number of occurrences replaced. dst is always written (a copy of src when
nothing was replaced or the file cannot be parsed, e.g. needs a password); the function
does not raise. A page tree too deep for qpdf is flattened first (same page objects).
Pass a list as notes to receive remarks (fallback font used, unreadable streams, failures).

Nothing is removed: links, annotations, form fields, outlines and attachments stay (a
link target or field value that holds an identifier gets the pseudonym). The file is
written with a deterministic /ID and no time stamps: same input and mapping, same bytes.
"""
import bisect
import math
import os
import re
import shutil
import struct
import unicodedata

import pikepdf
from pikepdf import Array, Dictionary, Name, Stream, String

try:
    import ahocorasick
except ImportError:  # the regular-expression matcher below is used instead
    ahocorasick = None
try:
    import pymupdf
except ImportError:  # glyph names and standard-font widths then come from the small tables here
    pymupdf = None

_WS = rb"\x00\t\n\x0c\r "
_DELIM = _WS + rb"()<>\[\]{}/%"
_NUM = rb"[-+]?(?:\d+\.?\d*|\.\d+)"
# One content-stream token per match. "bore" swallows whole runs of path construction,
# painting and colour operators with their operands, so vector-heavy drawings cost little.
_TOKEN = re.compile(
    rb"(?P<ws>[" + _WS + rb"]+)"
    rb"|(?P<bore>(?:(?:" + _NUM + rb"[" + _WS + rb"]+)*"
    rb"(?:re|[mlcvyhSsfFBbnwJjMi]|[fBb]\*|rg|RG|[gGkK])(?![^" + _DELIM + rb"])[" + _WS + rb"]*)+)"
    rb"|(?P<num>" + _NUM + rb")(?![^" + _DELIM + rb"])"
    rb"|(?P<name>/[^" + _DELIM + rb"]*)"
    rb"|(?P<lit>\((?:[^()\\]|\\.|\((?:[^()\\]|\\.|\((?:[^()\\]|\\.)*\))*\))*\))"
    rb"|(?P<dopen><<)|(?P<dclose>>>)"
    rb"|(?P<hex><[0-9A-Fa-f" + _WS + rb"]*>)"
    rb"|(?P<aopen>\[)|(?P<aclose>\])"
    rb"|(?P<cmt>%[^\r\n]*)"
    rb"|(?P<op>[^" + _DELIM + rb"]+)"
    rb"|(?P<bad>.)", re.S)
_PAREN = re.compile(rb"\\.|[()]", re.S)
_EI = re.compile(rb"[" + _WS + rb"]EI(?![^" + _DELIM + rb"])")
_ESC = re.compile(rb"\\(?:([0-7]{1,3})|(\r\n|\r|\n)|([\s\S]))|(\r\n?)|\\$")
_ESC_CHAR = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
_LIT_ESC = re.compile(rb"[\\()\r\n]")
_LIT_OUT = {b"\\": b"\\\\", b"(": b"\\(", b")": b"\\)", b"\r": b"\\r", b"\n": b"\\n"}
_NAME_ESC = re.compile(rb"#([0-9A-Fa-f]{2})")
_TEXTY = re.compile(rb"T[jJ]|['\"]|BDC|DP")
_NONSPACE = re.compile(r"\S+")
_BF = re.compile(rb"beginbf(char|range)(.*?)endbf", re.S)
_BF_TOKEN = re.compile(rb"<([0-9A-Fa-f\s]*)>|(\[)|(\])")
_CODESPACE = re.compile(rb"begincodespacerange(.*?)endcodespacerange", re.S)
_SUBSET = re.compile(r"/[A-Z]{6}\+")
_ID = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_MISSING = "\ufffd"
_LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl",
              "\ufb05": "st", "\ufb06": "st", "\u00a0": " "}
JOIN_GAP = 0.15   # a glyph that starts less than this (in font sizes) after the last one,
JOIN_BACK = 0.8   # or no more than this before its end,
JOIN_RISE = 0.8   # and no further than this off its baseline, continues the same word
FAKE_BOLD = 0.1   # a glyph shown again within this distance of itself is a bold effect, not text
GAP = "\n"        # stands in the text where two glyphs are not adjacent (no space glyph)
WILD = "\ufffc"   # stands for an accented letter of the text in the second, tolerant search
_ACCENTED = re.compile("[\u00c0-\u024f\u1e00-\u1eff\ufffd]")
LOOSE_KEYS = 200  # the tolerant search is skipped when more identifiers than this found nothing
LOOSE_MIN = 8     # shortest run-together identifier that may match separate capitalised words

# Strings of these dictionary keys are text shown to a reader (or a link target).
_TEXT_KEYS = set("""/Contents /T /RC /Subj /Title /ActualText /Alt /E /TU /TM /V /DV /CA /RV
/Desc /Name /Reason /Location /ContactInfo /Lbl /URI /F /UF""".split())
_TEXT_ARRAYS = set("/Opt /V /DV".split())

# Helvetica advance widths of the characters 32..126 (Adobe font metrics).
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
         556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
         1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
         667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
         333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
         556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]

_HELV_BOLD = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
              556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
              975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
              667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
              333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
              611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]

_PUNCT = dict(zip("""space exclam quotedbl numbersign dollar percent ampersand quotesingle
parenleft parenright asterisk plus comma hyphen period slash zero one two three four five six
seven eight nine colon semicolon less equal greater question at""".split(),
                  " !\"#$%&'()*+,-./0123456789:;<=>?@"))
_PUNCT.update(zip("""bracketleft backslash bracketright asciicircum underscore grave braceleft bar
braceright asciitilde""".split(), "[\\]^_`{|}~"))
_PUNCT.update({"quoteright": "\u2019", "quoteleft": "\u2018", "quotedblleft": "\u201c",
               "quotedblright": "\u201d", "endash": "\u2013", "emdash": "\u2014", "bullet": "\u2022",
               "fi": "fi", "fl": "fl", "ff": "ff", "ffi": "ffi", "ffl": "ffl", "nbspace": " ",
               "minus": "\u2212", "germandbls": "\u00df", "degree": "\u00b0", "mu": "\u00b5",
               "periodcentered": "\u00b7", "multiply": "\u00d7", "Euro": "\u20ac"})
_ACCENTS = {"acute": "\u0301", "grave": "\u0300", "circumflex": "\u0302", "tilde": "\u0303",
            "dieresis": "\u0308", "ring": "\u030a", "caron": "\u030c", "cedilla": "\u0327",
            "macron": "\u0304", "breve": "\u0306", "ogonek": "\u0328", "dotaccent": "\u0307",
            "hungarumlaut": "\u030b"}
_GLYPHS = {}


def _s(obj):
    """str() of a PDF name; '' when its bytes are not UTF-8 (pikepdf refuses those)."""
    try:
        return str(obj)
    except UnicodeError:
        return ""


def _glyph_text(name):
    """Unicode text of a glyph name, '' when it says nothing (g12, cid34, a1 ...)."""
    out = _GLYPHS.get(name)
    if out is None:
        out = _GLYPHS[name] = _glyph_lookup(name)
    return out


def _glyph_lookup(name):
    base = name if name.startswith(".") else name.split(".", 1)[0]
    if not base:
        return ""
    if len(base) == 1 and base.isascii() and base.isalnum():
        return base
    if base in _PUNCT:
        return _PUNCT[base]
    m = re.fullmatch(r"uni((?:[0-9A-Fa-f]{4})+)", base)
    if m:
        return bytes.fromhex(m.group(1)).decode("utf-16-be", "ignore")
    m = re.fullmatch(r"u([0-9A-Fa-f]{4,6})", base)
    if m:
        cp = int(m.group(1), 16)
        return chr(cp) if cp < 0x110000 and not 0xD800 <= cp < 0xE000 else ""
    if "_" in base:
        parts = [_glyph_lookup(p) for p in base.split("_")]
        return "".join(parts) if all(parts) else ""
    if pymupdf is not None:
        try:
            cp = pymupdf.mupdf.fz_unicode_from_glyph_name(base)
        except Exception:  # a name MuPDF cannot take: treated as unknown
            cp = 0xFFFD
        return chr(cp) if 0 < cp < 0x110000 and cp != 0xFFFD and not 0xD800 <= cp < 0xE000 else ""
    for accent, mark in _ACCENTS.items():
        if base.endswith(accent) and len(base) == len(accent) + 1 and base[0].isalpha():
            return unicodedata.normalize("NFC", base[0] + mark)
    return ""


def _codec_table(codec, skip, fixes):
    table = {}
    for c in range(32, 256):
        if c in skip:
            continue
        try:
            table[c] = bytes([c]).decode(codec)
        except UnicodeDecodeError:  # a code the encoding leaves undefined
            continue
    table.update(fixes)
    return table


_WIN = _codec_table("cp1252", (0x7F,), {0xA0: " ", 0xAD: "-"})
_MAC = _codec_table("mac_roman", (0x7F,), {0xCA: " "})
_STD = {c: chr(c) for c in range(32, 127)}
_STD.update({0x27: "\u2019", 0x60: "\u2018"})
_STD.update(zip(
    (0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xAB, 0xAC, 0xAD, 0xAE, 0xAF,
     0xB1, 0xB2, 0xB3, 0xB4, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xBB, 0xBC, 0xBD, 0xBF, 0xD0, 0xE1,
     0xE3, 0xE8, 0xE9, 0xEA, 0xEB, 0xF1, 0xF5, 0xF8, 0xF9, 0xFA, 0xFB),
    ("\u00a1", "\u00a2", "\u00a3", "\u2044", "\u00a5", "\u0192", "\u00a7", "\u00a4", "'", "\u201c",
     "\u00ab", "\u2039", "\u203a", "fi", "fl", "\u2013", "\u2020", "\u2021", "\u00b7", "\u00b6",
     "\u2022", "\u201a", "\u201e", "\u201d", "\u00bb", "\u2026", "\u2030", "\u00bf", "\u2014",
     "\u00c6", "\u00aa", "\u0141", "\u00d8", "\u0152", "\u00ba", "\u00e6", "\u0131", "\u0142",
     "\u00f8", "\u0153", "\u00df")))
# Predefined CMaps whose codes are a legacy code page: one byte for ASCII, else two.
_CJK = (("GBK2K", "gb18030"), ("GBKp-EUC", "gbk"), ("GBK-EUC", "gbk"), ("GBpc-EUC", "gbk"), ("GB-EUC", "gbk"),
        ("HKscs-B5", "big5hkscs"), ("ETenms-B5", "cp950"), ("ETen-B5", "cp950"), ("B5pc", "big5"),
        ("90msp-RKSJ", "cp932"), ("90ms-RKSJ", "cp932"), ("90pv-RKSJ", "cp932"), ("83pv-RKSJ", "cp932"),
        ("KSCms-UHC", "cp949"), ("KSCpc-EUC", "euc_kr"), ("KSC-EUC", "euc_kr"), ("EUC", "euc_jp"))
_BASE14 = (("courier", None), ("helvetica", "helv"), ("arial", "helv"), ("times", "tiro"))
_METRICS = {}


def _standard_widths(basefont, dec):
    """Widths of a standard font that carries no /Widths, or None when unknown."""
    name = basefont.lower()
    for key, code in _BASE14:
        if key not in name:
            continue
        if code is None:
            return [600.0] * 256
        bold, slant = "bold" in name, "italic" in name or "oblique" in name
        if code == "helv":
            code = "hebo" if bold else "helv"
        else:
            code = ("tibi" if slant else "tibo") if bold else ("tiit" if slant else "tiro")
        if pymupdf is None:
            if code != "helv":
                return None
            return [float(_HELV[ord(dec[c]) - 32]) if len(dec.get(c, "")) == 1 and 32 <= ord(dec[c]) < 127
                    else 556.0 for c in range(256)]
        font = _METRICS.get(code)
        if font is None:
            font = _METRICS[code] = pymupdf.Font(code)
        out = []
        for c in range(256):
            ch = dec.get(c, "")
            out.append(font.glyph_advance(ord(ch)) * 1000.0 if len(ch) == 1 else 0.0)
        return out
    return None


# The standard fonts a word falls back to when its own font lacks a glyph: resource name
# and MuPDF's code for the metrics, by PDF base font name.
_FALLBACK = {
    "Helvetica": ("DeidHv", "helv"), "Helvetica-Bold": ("DeidHvB", "hebo"),
    "Helvetica-Oblique": ("DeidHvI", "heit"), "Helvetica-BoldOblique": ("DeidHvBI", "hebi"),
    "Courier": ("DeidCo", "cour"), "Courier-Bold": ("DeidCoB", "cobo"),
    "Courier-Oblique": ("DeidCoI", "coit"), "Courier-BoldOblique": ("DeidCoBI", "cobi"),
    "Times-Roman": ("DeidTi", "tiro"), "Times-Bold": ("DeidTiB", "tibo"),
    "Times-Italic": ("DeidTiI", "tiit"), "Times-BoldItalic": ("DeidTiBI", "tibi"),
}
_SERIF = ("times", "georgia", "garamond", "cambria", "palatino", "minion", "bookman", "antiqua", "nimbusrom",
          "liberationserif", "dejavuserif", "cmr", "century school")
_SANS = ("sans", "arial", "helvet", "gothic", "calibri", "verdana", "tahoma", "segoe", "roboto", "lato")
_MONO = ("courier", "mono", "consol", "typewriter", "cmtt")


def _fallback_for(base, desc):
    """Base font name of the standard font closest in style to a font."""
    low = base.lower()
    flags, weight, angle = 0, 0.0, 0.0
    if isinstance(desc, Dictionary):
        try:
            flags, weight = int(desc.get("/Flags", 0)), float(desc.get("/FontWeight", 0))
            angle = float(desc.get("/ItalicAngle", 0))
        except (TypeError, ValueError):  # odd values in the descriptor: judge by the name alone
            flags, weight, angle = 0, 0.0, 0.0
    bold = weight >= 600 or bool(flags & 0x40000) or any(w in low for w in ("bold", "black", "heavy", "demi"))
    slant = "italic" in low or "oblique" in low or angle != 0 or bool(flags & 0x40)
    if any(w in low for w in _MONO) or (flags & 1 and not any(w in low for w in _SANS + _SERIF)):
        return "Courier" + ("-BoldOblique" if bold and slant else "-Bold" if bold else "-Oblique" if slant else "")
    serif = any(w in low for w in _SERIF) or (flags & 2 and not any(w in low for w in _SANS))
    if serif and pymupdf is not None and "sans" not in low:
        return "Times-" + ("BoldItalic" if bold and slant else "Bold" if bold else "Italic" if slant else "Roman")
    return "Helvetica" + ("-BoldOblique" if bold and slant else "-Bold" if bold else "-Oblique" if slant else "")


def _fb_width(text, base):
    """Advance of text in a standard font, in 1/1000 of the font size."""
    if base.startswith("Courier"):
        return 600.0 * len(text)
    if base.startswith("Times") and pymupdf is not None:
        code = _FALLBACK[base][1]
        font = _METRICS.get(code)
        if font is None:
            font = _METRICS[code] = pymupdf.Font(code)
        return float(sum(font.glyph_advance(ord(ch)) * 1000.0 for ch in text))
    table = _HELV_BOLD if "Bold" in base else _HELV
    return float(sum(table[ord(ch) - 32] if 32 <= ord(ch) < 127 else 556 for ch in text))


def _u16(hexdigits):
    h = b"".join(hexdigits.split())
    if len(h) % 2:
        h += b"0"
    raw = bytes.fromhex(h.decode("ascii"))
    if len(raw) == 1:
        return chr(raw[0])
    return raw.decode("utf-16-be", "ignore")


def _tounicode(data):
    """(code -> text, [(first code, last code, first code point)]) of a ToUnicode CMap."""
    chars, ranges = {}, []
    for m in _BF.finditer(data):
        toks = _BF_TOKEN.findall(m.group(2))
        if m.group(1) == b"char":
            hexes = [t[0] for t in toks if not t[1] and not t[2]]
            for i in range(0, len(hexes) - 1, 2):
                code = b"".join(hexes[i].split())
                if code:
                    chars[int(code, 16)] = _u16(hexes[i + 1])
            continue
        i = 0
        while i + 2 < len(toks):
            lo, hi = b"".join(toks[i][0].split()), b"".join(toks[i + 1][0].split())
            if toks[i][1] or toks[i][2] or toks[i + 1][1] or toks[i + 1][2] or not lo or not hi:
                i += 1
                continue
            lo, hi = int(lo, 16), int(hi, 16)
            if toks[i + 2][1]:  # an array: one destination per code
                j, k = i + 3, 0
                while j < len(toks) and not toks[j][2]:
                    if not toks[j][1]:
                        chars[lo + k] = _u16(toks[j][0])
                        k += 1
                    j += 1
                i = j + 1
                continue
            text = _u16(toks[i + 2][0])
            i += 3
            if not text or hi < lo:
                continue
            if hi - lo < 512:
                head, last = text[:-1], ord(text[-1])
                for k in range(hi - lo + 1):
                    if last + k < 0x110000:
                        chars[lo + k] = head + chr(last + k)
            elif len(text) == 1:
                ranges.append((lo, hi, ord(text)))
    return chars, ranges


def _cmap_pairs(data, fmt, at):
    """(character code, glyph number) pairs of one cmap subtable."""
    if fmt == 0:
        return [(c, data[at + 6 + c]) for c in range(256)]
    if fmt == 6:
        first, count = struct.unpack_from(">HH", data, at + 6)
        return list(zip(range(first, first + count), struct.unpack_from(">%dH" % count, data, at + 10)))
    if fmt == 4:
        seg = struct.unpack_from(">H", data, at + 6)[0] // 2
        ends = struct.unpack_from(">%dH" % seg, data, at + 14)
        starts = struct.unpack_from(">%dH" % seg, data, at + 16 + 2 * seg)
        deltas = struct.unpack_from(">%dH" % seg, data, at + 16 + 4 * seg)
        base = at + 16 + 6 * seg
        offsets = struct.unpack_from(">%dH" % seg, data, base)
        out = []
        for i in range(seg):
            if len(out) > 70000:
                break
            for c in range(starts[i], min(ends[i], 0xFFFE) + 1):
                if offsets[i] == 0:
                    gid = (c + deltas[i]) & 0xFFFF
                else:
                    gid = struct.unpack_from(">H", data, base + 2 * i + offsets[i] + 2 * (c - starts[i]))[0]
                    if gid:
                        gid = (gid + deltas[i]) & 0xFFFF
                out.append((c, gid))
        return out
    if fmt == 12:
        groups = struct.unpack_from(">I", data, at + 12)[0]
        out = []
        for i in range(min(groups, 20000)):
            first, last, gid = struct.unpack_from(">III", data, at + 16 + 12 * i)
            if len(out) > 70000 or last < first:
                break
            out.extend((c, gid + c - first) for c in range(first, min(last, first + 70000) + 1))
        return out
    return []


def _sfnt_chars(data):
    """glyph number -> character from the cmap table of a TrueType / OpenType program."""
    out = {}
    try:
        base = struct.unpack_from(">I", data, 12)[0] if data[:4] == b"ttcf" else 0
        if data[base:base + 4] not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
            return out
        cmap = None
        for i in range(struct.unpack_from(">H", data, base + 4)[0]):
            tag, _sum, off, _length = struct.unpack_from(">4sIII", data, base + 12 + 16 * i)
            if tag == b"cmap":
                cmap = off
        if cmap is None:
            return out
        rank = {(3, 10): 0, (0, 4): 1, (0, 6): 1, (3, 1): 2, (0, 3): 3, (0, 2): 3, (0, 1): 3, (0, 0): 3,
                (3, 0): 5, (1, 0): 6}
        subs = []
        for i in range(struct.unpack_from(">H", data, cmap + 2)[0]):
            plat, enc, off = struct.unpack_from(">HHI", data, cmap + 4 + 8 * i)
            subs.append((rank.get((plat, enc), 9), plat, enc, cmap + off))
        for _rank, plat, enc, at in sorted(subs):
            for code, gid in _cmap_pairs(data, struct.unpack_from(">H", data, at)[0], at):
                if not gid or gid in out:
                    continue
                if plat == 1:
                    ch = bytes((code,)).decode("mac_roman") if 32 <= code < 256 else ""
                elif plat == 3 and enc == 0:
                    ch = chr(code & 0xFF) if 0xF020 <= code <= 0xF0FF or 32 <= code < 256 else ""
                else:
                    ch = chr(code) if 32 <= code < 0x110000 and not 0xD800 <= code < 0xE000 else ""
                if ch:
                    out[gid] = _LIGATURES.get(ch, ch)
            if out:
                break
    except (struct.error, IndexError, UnicodeDecodeError):  # a damaged font program: no table
        return {}
    return out


def _cid_widths(w):
    out = {}
    items = list(w)
    i, n = 0, len(items)
    while i < n:
        first = int(items[i])
        if i + 1 < n and isinstance(items[i + 1], Array):
            for k, v in enumerate(items[i + 1]):
                out[first + k] = float(v)
            i += 2
        elif i + 2 < n:
            last, v = int(items[i + 1]), float(items[i + 2])
            if 0 <= last - first < 70000:
                for c in range(first, last + 1):
                    out[c] = v
            i += 3
        else:
            break
    return out


class _Font:
    """How one font resource turns glyph codes into text and back."""

    def __init__(self, fobj):
        self.used = set()      # codes some content stream of the document shows with this font
        self.dec = {}          # code -> text
        self.rng = []          # large ToUnicode ranges: (first code, last code, first code point)
        self.rev = None        # character -> codes, built on first use
        self.two = False       # two-byte codes
        self.codec = None      # ... or a legacy code page: one byte for ASCII, else two
        self.ucs2 = False      # codes are UTF-16 code units (no ToUnicode needed)
        self.shift = None      # (offset, first, last): codes that are a character minus offset
        self.identity = self.cjk = self.cid2 = False
        self.gidmap = None
        self.cidtext = False   # nothing says what the codes mean: read as characters, like MuPDF
        self.alt = False       # ... and that reading exists next to the font program's own
        self.wl = [0.0] * 256  # advance widths of one-byte codes, in 1/1000 of the font size
        self.wd = {}           # ... of two-byte codes
        self.dw = 1000.0
        self.metric = False    # the widths are known
        self.sized = False     # ... from a /Widths array
        self.embedded = False
        self.type3 = False
        self.procs = ()
        self.gname = {}
        self.vertical = False
        self.table = None
        self.fallback = "Helvetica"
        sub = fobj.get("/Subtype")
        base = _s(fobj.get("/BaseFont", ""))
        if sub == Name.Type0:
            desc = self._composite(fobj)
        else:
            desc = fobj.get("/FontDescriptor")
            self._simple(fobj, sub, base, desc)
        if isinstance(desc, Dictionary):
            self.embedded = any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3"))
        self.fallback = _fallback_for(base, desc)
        tu = fobj.get("/ToUnicode")
        if isinstance(tu, Stream):
            chars, self.rng = _tounicode(tu.read_bytes())
            self.dec.update(chars)
        elif isinstance(tu, Name) and _s(tu).startswith("/Identity"):
            self.ucs2 = True  # /ToUnicode /Identity-H: the code is the character
        elif self.two and self.identity and not self.ucs2:
            if self.cjk:
                self.shift = (31, 1, 95)  # Adobe CJK collections: ASCII is CID 1..95
            elif self.cid2 and not self.embedded:
                # A TrueType font that is not embedded, shown by glyph number (SolidWorks).
                # The standard Windows fonts keep ASCII at glyph = character - 29; MuPDF
                # reads such text the same way.
                self.shift = (29, 3, 97)
            else:
                # No ToUnicode at all. MuPDF's text extraction then reports the code itself
                # as the character; an embedded TrueType program also tells the true one.
                # Both readings are searched; new text for such a font goes to a standard
                # font, the only way every reader gets the same word.
                if self.cid2 and isinstance(desc, Dictionary):
                    self._from_program(desc, self.gidmap)
                self.alt, self.cidtext = bool(self.dec), not self.dec
        for code, text in list(self.dec.items()):
            if any(ch in _LIGATURES for ch in text):
                self.dec[code] = "".join(_LIGATURES.get(ch, ch) for ch in text)
        if self.codec:
            self.two = False
        elif not self.two:
            if not self.metric:
                widths = _standard_widths(base, self.dec)
                if widths:
                    self.wl, self.metric = widths, True
            self.table = {c: self.dec.get(c, _MISSING) for c in range(256)}

    def _simple(self, fobj, sub, base, desc):
        enc = fobj.get("/Encoding")
        ename, diffs = "", None
        if isinstance(enc, Name):
            ename = _s(enc)
        elif isinstance(enc, Dictionary):
            ename, diffs = _s(enc.get("/BaseEncoding", "")), enc.get("/Differences")
        self.type3 = sub == Name.Type3
        low = base.lower()
        if self.type3:
            table = {}
        elif ename == "/WinAnsiEncoding":
            table = _WIN
        elif ename == "/MacRomanEncoding":
            table = _MAC
        elif ename == "/StandardEncoding":
            table = _STD
        elif ename == "/MacExpertEncoding" or any(s in low for s in ("symbol", "dingbat", "wingding", "webding")):
            table = {}
        else:
            table = _WIN if sub == Name.TrueType else _STD
        self.dec = dict(table)
        if isinstance(diffs, Array):
            code = 0
            for item in diffs:
                if isinstance(item, Name):
                    name = _s(item)[1:]
                    self.gname[code] = name
                    text = _glyph_text(name)
                    if text:
                        self.dec[code] = text
                    else:
                        self.dec.pop(code, None)
                    code += 1
                else:
                    code = int(item)
        scale = 1.0
        if self.type3:
            procs = fobj.get("/CharProcs")
            self.procs = set(k[1:] for k in procs.keys()) if isinstance(procs, Dictionary) else set()
            matrix = fobj.get("/FontMatrix")
            scale = abs(float(matrix[0])) * 1000.0 if isinstance(matrix, Array) and len(matrix) == 6 else 1.0
        widths = fobj.get("/Widths")
        if isinstance(widths, Array):
            first = int(fobj.get("/FirstChar", 0))
            missing = float(desc.get("/MissingWidth", 0)) if isinstance(desc, Dictionary) else 0.0
            self.wl = [missing * scale] * 256
            for k, w in enumerate(widths):
                if 0 <= first + k < 256:
                    self.wl[first + k] = float(w) * scale
            self.metric = self.sized = True

    def _composite(self, fobj):
        self.two = True
        enc = fobj.get("/Encoding")
        ename = _s(enc) if isinstance(enc, Name) else ""
        if isinstance(enc, Stream):
            ename = _s(enc.get("/CMapName", ""))
            self.vertical = int(enc.get("/WMode", 0)) == 1
            lengths = set()
            for m in _CODESPACE.finditer(enc.read_bytes()):
                lengths.update(len(b"".join(t[0].split())) // 2 for t in _BF_TOKEN.findall(m.group(1)) if t[0])
            if lengths == {1}:
                self.two = False
        self.vertical = self.vertical or ename.endswith("-V")
        self.ucs2 = "UCS2" in ename or "UTF16" in ename
        identity = self.identity = ename in ("/Identity-H", "/Identity-V")
        self.codec = next((codec for key, codec in _CJK if ename[1:].startswith(key)), None)
        desc = None
        kids = fobj.get("/DescendantFonts")
        if isinstance(kids, Array) and len(kids):
            kid = kids[0]
            desc = kid.get("/FontDescriptor")
            self.cid2 = kid.get("/Subtype") == Name.CIDFontType2
            self.gidmap = kid.get("/CIDToGIDMap")
            info = kid.get("/CIDSystemInfo")
            ordering = info.get("/Ordering") if isinstance(info, Dictionary) else None
            self.cjk = isinstance(ordering, String) and bytes(ordering) in (b"Japan1", b"GB1", b"CNS1", b"Korea1")
            self.dw = float(kid.get("/DW", 1000))
            w = kid.get("/W")
            if identity:
                self.metric = True
                if isinstance(w, Array):
                    self.wd = _cid_widths(w)
        if not self.two:
            self.wl = [self.dw] * 256
        return desc

    def _look(self, code):
        """Text of a code that is not in the table yet (large ranges, UTF-16 encodings)."""
        text = _MISSING
        for lo, hi, first in self.rng:
            if lo <= code <= hi:
                cp = first + code - lo
                if cp < 0x110000 and not 0xD800 <= cp < 0xE000:
                    text = _LIGATURES.get(chr(cp), chr(cp))
                break
        else:
            if self.ucs2 and not 0xD800 <= code < 0xE000:
                text = chr(code)
            elif self.shift and self.shift[1] <= code <= self.shift[2]:
                text = chr(code + self.shift[0])
            elif self.cidtext and code >= 32 and not 0xD800 <= code < 0xE000:
                text = chr(code)
        self.dec[code] = text
        return text

    def _coded(self, raw):
        """[(code, text)] of a string in a legacy code page."""
        out, i, n, codec, dec = [], 0, len(raw), self.codec, self.dec
        while i < n:
            b = raw[i]
            if b < 0x80 or i + 1 >= n or (codec == "cp932" and 0xA0 <= b <= 0xDF):
                out.append((b, dec.get(b) or (chr(b) if 32 <= b < 0x7F else _MISSING)))
                i += 1
                continue
            code = (b << 8) | raw[i + 1]
            text = dec.get(code)
            if text is None:
                try:
                    text = raw[i:i + 2].decode(codec)
                except UnicodeDecodeError:  # not a character of the code page
                    text = _MISSING
            out.append((code, text))
            i += 2
        return out

    def _from_program(self, desc, gidmap):
        """Glyph numbers to characters from the embedded TrueType program's own cmap."""
        program = desc.get("/FontFile2")
        if not isinstance(program, Stream):
            program = desc.get("/FontFile3")
        if not isinstance(program, Stream):
            return
        chars = _sfnt_chars(program.read_bytes())
        if not chars:
            return
        if isinstance(gidmap, Stream):
            table = gidmap.read_bytes()
            for cid in range(len(table) // 2):
                text = chars.get((table[2 * cid] << 8) | table[2 * cid + 1])
                if text:
                    self.dec[cid] = text
        else:
            self.dec.update(chars)

    def measure(self, raw):
        """(text, summed advance in 1/1000 of the font size, glyphs, one-byte spaces)."""
        if self.codec:
            glyphs = self._coded(raw)
            self.used.update([g[0] for g in glyphs])
            return ("".join([g[1] for g in glyphs]), sum([500.0 if g[0] < 256 else 1000.0 for g in glyphs]),
                    len(glyphs), raw.count(b" "))
        if not self.two:
            self.used.update(raw)
            return (raw.decode("latin-1").translate(self.table), sum(map(self.wl.__getitem__, raw)),
                    len(raw), raw.count(b" "))
        n = len(raw) >> 1
        codes = struct.unpack(">%dH" % n, raw[:n + n])
        self.used.update(codes)
        dec, look = self.dec, self._look
        text = "".join([dec[c] if c in dec else look(c) for c in codes])
        wd, dw = self.wd, self.dw
        return text, (sum([wd.get(c, dw) for c in codes]) if wd else n * dw), n, 0

    def second(self, raw):
        """MuPDF's reading of a string in a font whose codes have no stated meaning."""
        n = len(raw) >> 1
        return "".join([chr(c) if c >= 32 and not 0xD800 <= c < 0xE000 else _MISSING
                        for c in struct.unpack(">%dH" % n, raw[:n + n])])

    def glyphs(self, raw):
        """[(code, text)] of a shown string."""
        if self.codec:
            return self._coded(raw)
        if not self.two:
            table = self.table
            return [(c, table[c]) for c in raw]
        n = len(raw) >> 1
        dec, look = self.dec, self._look
        return [(c, dec[c] if c in dec else look(c)) for c in struct.unpack(">%dH" % n, raw[:n + n])]

    def pack(self, code):
        return struct.pack(">H", code) if self.two or code > 255 else bytes((code,))

    def width(self, code):
        if self.codec:
            return 500.0 if code < 256 else 1000.0
        return self.wd.get(code, self.dw) if self.two else self.wl[code]

    def _usable(self, code, invisible):
        if self.type3:
            return self.gname.get(code) in self.procs
        if invisible or code in self.used:
            return True
        if self.embedded or self.two:
            return False
        # not embedded: the viewer's own font has the glyph, but a zero in /Widths (written
        # for characters the document does not use) would stack it on the next one
        return not self.sized or self.wl[code] > 0

    def encode(self, ch, invisible=False):
        """A code of this font that shows ch and whose glyph the font has, or None."""
        if self.alt or self.cidtext:
            return None
        if self.codec:
            code = ord(ch)
            return code if 32 <= code < 0x7F and self._usable(code, invisible) else None
        if self.rev is None:
            rev = {}
            for code in sorted(self.dec):
                text = self.dec[code]
                if len(text) == 1 and text != _MISSING and (self.two or code < 256):
                    rev.setdefault(text, []).append(code)
            self.rev = rev
        for code in self.rev.get(ch, ()):
            if self._usable(code, invisible):
                return code
        cp = ord(ch)
        for lo, hi, first in self.rng:
            if first <= cp <= first + hi - lo and self._usable(lo + cp - first, invisible):
                return lo + cp - first
        if self.ucs2 and not self.rng and cp < 0x10000 and self._usable(cp, invisible):
            return cp
        if self.shift and 32 <= cp <= 126 and self._usable(cp - self.shift[0], invisible):
            return cp - self.shift[0]
        return None


class _NoFont:
    """Text shown with a font that cannot be resolved: counted, never matched."""
    two = metric = vertical = type3 = alt = False

    def measure(self, raw):
        return _MISSING * len(raw), 0.0, len(raw), 0

    def glyphs(self, raw):
        return [(c, _MISSING) for c in raw]


_NOFONT = _NoFont()


class _Tz(float):
    """A change of horizontal scaling between the pieces of a rewritten operator."""


class _Op:
    """One text-showing operator: where it is in the stream and what it shows."""
    __slots__ = ("start", "end", "name", "items", "font", "fname", "fsize", "tr", "pre", "moved", "tc", "tw",
                 "th", "over", "clip")


class _Span:
    """A marked-content sequence with /ActualText: its glyphs stand for that text. The text
    is a string of the BDC property list (kind lit / hex, at start..end of the stream) or of
    the structure element that owns the sequence's MCID (kind struct, elem). plain: the
    text repeats what the glyphs show; otherwise the first glyph is taken to carry all of it."""
    __slots__ = ("text", "kind", "start", "end", "elem", "head", "members", "pos", "plain")

    def __init__(self, text, kind, start, end, elem=None):
        self.text = "".join([_LIGATURES.get(ch, ch) for ch in text])
        self.kind, self.start, self.end, self.elem = kind, start, end, elem
        self.head, self.members, self.pos, self.plain = None, [], 0, True


def _text_of(raw):
    """A PDF text string as str ('' when it is not text in the encoding it claims)."""
    try:
        if raw[:2] == b"\xfe\xff":
            return raw[2:].decode("utf-16-be")
        if raw[:2] == b"\xff\xfe":
            return raw[2:].decode("utf-16-le")
        if raw[:3] == b"\xef\xbb\xbf":
            return raw[3:].decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return raw.decode("latin-1")


def _text_bytes(text):
    try:
        return text.encode("ascii")
    except UnicodeEncodeError:  # needs more than PDFDocEncoding's ASCII part
        return b"\xfe\xff" + text.encode("utf-16-be")


def _unescape(raw):
    if b"\\" not in raw and b"\r" not in raw:
        return raw

    def sub(m):
        octal, eol, ch, bare = m.groups()
        if octal:
            return bytes((int(octal, 8) & 0xFF,))
        if eol:
            return b""
        if ch:
            return _ESC_CHAR.get(ch, ch)
        return b"\n" if bare else b""
    return _ESC.sub(sub, raw)


def _unhex(raw):
    h = b"".join(raw.split())
    if len(h) % 2:
        h += b"0"
    try:
        return bytes.fromhex(h.decode("ascii"))
    except ValueError:  # not hexadecimal after all: shows nothing we can read
        return b""


def _string(raw, as_hex):
    if as_hex:
        return b"<" + raw.hex().upper().encode("ascii") + b">"
    return b"(" + _LIT_ESC.sub(lambda m: _LIT_OUT[m.group()], raw) + b")"


def _fmt(v):
    if v == int(v):
        return b"%d" % int(v)
    return (b"%.4f" % v).rstrip(b"0").rstrip(b".")


def _lit_end(data, start):
    """End of the literal string that opens at data[start], or -1 when it never closes."""
    depth = 0
    for m in _PAREN.finditer(data, start):
        c = m.group()
        if c == b"(":
            depth += 1
        elif c == b")":
            depth -= 1
            if depth == 0:
                return m.end()
    return -1


def _interpret(data, getfont):
    """Walk a content stream. Returns (text, runs, ops, dstr, alts, dups):
    text  the shown text in stream order, GAP where two glyphs are not adjacent
    runs  (offset in text, operator number, item number, first glyph, glyphs, joined) per
          shown string; joined: it starts where the string before it ended
    ops   the text-showing operators (_Op)
    dstr  (kind, start, end) of strings in marked-content property lists
    alts  (offset in text, other reading) for strings MuPDF reads differently (_Font.alt)
    dups  (operator, item) of a glyph shown again in the same place -> those of its first copy"""
    ctm = _ID
    saved = []
    font, fname, fsize = None, b"", 0.0
    tc = tw = tl = ts = 0.0
    th, tr = 1.0, 0
    tm = tlm = _ID
    opnd = []
    parts, plen = [], 0
    runs, ops, dstr, alts = [], [], [], []
    lag, dups = None, {}  # the last single glyph shown; (op, item) of its repeat -> (op, item) of it
    pen = None    # where the previous glyph ended, in page space (its advance, no extra spacing)
    moved = True  # a text-positioning operator came since the last shown string
    marks, span = [], None  # open marked-content sequences; the innermost one with ActualText
    clip = clips = 0  # the clipping path in force, numbered: W / W* make a new one, Q restores
    getgs = getattr(getfont, "gs", None)
    getmc = getattr(getfont, "mc", None)

    def num(i):
        k, s, e = opnd[i]
        if k != "num":
            raise ValueError("operand is not a number")
        return float(data[s:e])

    def show(items, start, end, name, pre):
        nonlocal tm, pen, plen, moved, lag
        f = font if font is not None else _NOFONT
        a, b, c, d, e0, f0 = tm
        ca, cb, cc, cd, ce, cf = ctm
        ma, mb = a * ca + b * cc, a * cb + b * cd
        mc, md = c * ca + d * cc, c * cb + d * cd
        ox = ts * mc + e0 * ca + f0 * cc + ce
        oy = ts * md + e0 * cb + f0 * cd + cf
        ulen = math.hypot(ma, mb)
        # Word breaks follow MuPDF's text extraction: the size is the expansion of the text
        # rendering matrix, and the pen stops at the glyph's own advance (no Tc / Tw).
        size = abs(fsize) * math.sqrt(abs(th) * abs(ma * md - mb * mc))
        sign = -1.0 if fsize * th < 0 else 1.0
        ux, uy = (sign * ma / ulen, sign * mb / ulen) if ulen else (1.0, 0.0)
        near, behind, rise = JOIN_GAP * size, -JOIN_BACK * size, JOIN_RISE * size
        spaced = size > 0 and abs(tc * th) * ulen >= near  # letter-spaced: every glyph is a word
        t = 0.0
        rec, shown, over = [], False, None
        opi = len(ops)
        for k, s, e in items:
            if k == "num":
                try:
                    t -= float(data[s:e]) / 1000.0 * fsize * th
                except ValueError:  # not a number after all: no movement
                    continue
                rec.append(data[s:e])
                continue
            if k == "lit":
                raw = _unescape(data[s + 1:e - 1])
            elif k == "hex":
                raw = _unhex(data[s + 1:e - 1])
            else:
                continue
            rec.append((raw, k == "hex", s, e))
            if not raw:
                continue
            text, wsum, count, spaces = f.measure(raw)
            if count == 1 and span is None and size > 0:
                # Bold made by showing every glyph twice, a hair apart. MuPDF's text extraction
                # drops a glyph that repeats the one before it in the same place, so the
                # repeat is no text here either; it follows its first copy.
                x, y = t * ma + ox, t * mb + oy
                if (lag is not None and lag[0] is f and lag[1] == raw
                        and (lag[2] - x) ** 2 + (lag[3] - y) ** 2 < (FAKE_BOLD * size) ** 2):
                    dups[(opi, len(rec) - 1)] = lag[4]
                    shown = True
                    t += (wsum / 1000.0 * fsize + count * tc + spaces * tw) * th
                    continue
                lag = (f, raw, x, y, (opi, len(rec) - 1))
            else:
                lag = None
            if span is not None:
                if not span.members:
                    span.plain = span.text.startswith(text)
                if span.plain:  # the ActualText repeats the glyphs: it is kept in step with them
                    if span.text.startswith(text, span.pos):
                        span.pos += len(text)
                else:  # the glyphs stand for the ActualText: the first string carries all of it
                    text = ""
                    if span.head is None:
                        span.head, text = (opi, len(rec) - 1), span.text
                span.members.append((opi, len(rec) - 1))
                if over is None:
                    over = {}
                over[len(rec) - 1] = span
            joined = False
            if pen is not None and size > 0:
                dx, dy = t * ma + ox - pen[0], t * mb + oy - pen[1]
                joined = behind < dx * ux + dy * uy < near and abs(dx * uy - dy * ux) < rise
            if text and spaced and count > 1 and (span is None or span.plain):
                for gi, (_code, one) in enumerate(f.glyphs(raw)):
                    if not one:
                        continue
                    if not joined and plen and not parts[-1][-1:].isspace() and not one[0].isspace():
                        parts.append(GAP)
                        plen += 1
                    runs.append((plen, opi, len(rec) - 1, gi, 1, joined))
                    parts.append(one)
                    plen += len(one)
                    joined = False
            elif text:
                if not joined and plen and not parts[-1][-1:].isspace() and not text[0].isspace():
                    parts.append(GAP)
                    plen += 1
                runs.append((plen, opi, len(rec) - 1, 0, count, joined))
                if f.alt and span is None:
                    other = f.second(raw)
                    if len(other) == len(text) and other != text:
                        alts.append((plen, other))
                parts.append(text)
                plen += len(text)
            shown = True
            t += (wsum / 1000.0 * fsize + count * tc + spaces * tw) * th
            back = t - (tc + (tw if spaces and raw[-1:] == b" " else 0.0)) * th
            pen = (back * ma + ox, back * mb + oy)
        tm = (a, b, c, d, e0 + t * a, f0 + t * b)
        if shown:
            op = _Op()
            op.start, op.end, op.name, op.items = start, end, name, rec
            op.font, op.fname, op.fsize, op.tr, op.pre = f, fname, fsize, tr, pre
            op.moved, op.tc, op.tw, op.th, op.over, op.clip = moved, tc, tw, th, over, clip
            ops.append(op)
            moved = False

    pos, n = 0, len(data)
    finditer = _TOKEN.finditer
    while pos < n:
        jump = -1
        for m in finditer(data, pos):
            kind = m.lastgroup
            if kind == "ws" or kind == "cmt":
                continue
            if kind == "bore":
                if opnd:
                    opnd = []
                continue
            if kind != "op":
                if kind == "bad":
                    if data[m.start()] == 0x28:  # a string the pattern could not take in one piece
                        end = _lit_end(data, m.start())
                        if end < 0:
                            jump = n
                        else:
                            opnd.append(("lit", m.start(), end))
                            jump = end
                        break
                    continue
                opnd.append((kind, m.start(), m.end()))
                continue
            s, e = m.span()
            op = data[s:e]
            try:
                if op == b"Tj":
                    if opnd and opnd[-1][0] in ("lit", "hex"):
                        show(opnd[-1:], opnd[-1][1], e, op, None)
                elif op == b"TJ":
                    j = len(opnd) - 1
                    if j >= 0 and opnd[j][0] == "aclose":
                        i = j - 1
                        while i >= 0 and opnd[i][0] != "aopen":
                            i -= 1
                        if i >= 0:
                            show(opnd[i + 1:j], opnd[i][1], e, op, None)
                elif op == b"Td" or op == b"TD":
                    tx, ty = num(-2), num(-1)
                    if op == b"TD":
                        tl = -ty
                    a, b, c, d, e0, f0 = tlm
                    tm = tlm = (a, b, c, d, tx * a + ty * c + e0, tx * b + ty * d + f0)
                    moved = True
                elif op == b"Tm":
                    tm = tlm = (num(-6), num(-5), num(-4), num(-3), num(-2), num(-1))
                    moved = True
                elif op == b"Tf":
                    fsize = num(-1)
                    k, a, b = opnd[-2]
                    if k == "name":
                        fname = data[a + 1:b]
                        font = getfont(fname)
                elif op == b"BT":
                    tm = tlm = _ID
                    moved = True
                elif op == b"q":
                    saved.append((ctm, font, fname, fsize, tc, tw, th, tl, ts, tr, clip))
                elif op == b"Q":
                    if saved:
                        ctm, font, fname, fsize, tc, tw, th, tl, ts, tr, clip = saved.pop()
                elif op == b"W" or op == b"W*":
                    clips += 1
                    clip = clips
                elif op == b"cm":
                    a, b, c, d, e0, f0 = num(-6), num(-5), num(-4), num(-3), num(-2), num(-1)
                    ca, cb, cc, cd, ce, cf = ctm
                    ctm = (a * ca + b * cc, a * cb + b * cd, c * ca + d * cc, c * cb + d * cd,
                           e0 * ca + f0 * cc + ce, e0 * cb + f0 * cd + cf)
                elif op == b"T*" or op == b"'" or op == b'"':
                    pre = None
                    if op == b'"' and len(opnd) >= 3:
                        tw, tc = num(-3), num(-2)
                        pre = (data[opnd[-3][1]:opnd[-3][2]], data[opnd[-2][1]:opnd[-2][2]])
                    a, b, c, d, e0, f0 = tlm
                    tm = tlm = (a, b, c, d, -tl * c + e0, -tl * d + f0)
                    moved = True
                    if op != b"T*" and opnd and opnd[-1][0] in ("lit", "hex"):
                        show(opnd[-1:], opnd[-3][1] if pre else opnd[-1][1], e, op, pre)
                elif op == b"Tc":
                    tc = num(-1)
                elif op == b"Tw":
                    tw = num(-1)
                elif op == b"Tz":
                    th = num(-1) / 100.0
                elif op == b"TL":
                    tl = num(-1)
                elif op == b"Ts":
                    ts = num(-1)
                elif op == b"Tr":
                    tr = int(num(-1))
                elif op == b"BDC" or op == b"DP":
                    depth, found, mcid = 0, None, None
                    for i, (k, a, b) in enumerate(opnd):
                        if k == "dopen":
                            depth += 1
                        elif k == "dclose":
                            depth -= 1
                        elif depth > 0 and (k == "lit" or k == "hex"):
                            dstr.append((k, a, b))
                            if depth == 1 and i and data[opnd[i - 1][1]:opnd[i - 1][2]] == b"/ActualText":
                                found = (k, a, b)
                        elif depth == 1 and k == "num" and data[opnd[i - 1][1]:opnd[i - 1][2]] == b"/MCID":
                            mcid = int(float(data[a:b]))
                    if op == b"BDC":
                        new = None
                        if found:
                            k, a, b = found
                            raw = _unescape(data[a + 1:b - 1]) if k == "lit" else _unhex(data[a + 1:b - 1])
                            new = _Span(_text_of(raw), k, a, b)
                        elif mcid is not None and getmc is not None:
                            new = getmc(mcid)
                        if new is not None:
                            span = new
                        marks.append(new)
                elif op == b"BMC":
                    marks.append(None)
                elif op == b"EMC":
                    if marks:
                        marks.pop()
                        span = next((m for m in reversed(marks) if m is not None), None)
                elif op == b"gs":
                    k, a, b = opnd[-1]
                    got = getgs(data[a + 1:b]) if k == "name" and getgs is not None else None
                    if got:
                        font, fsize, fname = got
                elif op == b"ID":
                    end = _EI.search(data, e)
                    jump = end.end() if end else n
            except (IndexError, ValueError):  # an operator without its operands: skipped
                pass
            opnd = []
            if jump >= 0:
                break
        else:
            break
        if jump < 0:
            break
        pos = jump
    return "".join(parts), runs, ops, dstr, alts, dups


def _lower(text):
    low = text.lower()
    if len(low) != len(text):  # a few characters lower-case to two; keep those as they are
        low = "".join([c.lower() if len(c.lower()) == 1 else c for c in text])
    return low


def _recase(value, seen):
    """value in the case pattern of the text it replaces."""
    if len(value) != len(seen):
        return value
    return "".join([v.upper() if s.isupper() else v.lower() if s.islower() else v for v, s in zip(value, seen)])


def _clash(inner, outer):
    return (inner.isalpha() and outer.isalpha()) or (inner.isdigit() and outer.isdigit())


class _Matcher:
    """Finds the identifiers of a mapping in text, ignoring case and white space."""

    def __init__(self, mapping):
        self.keys = {}  # squeezed lower-case key -> [(squeezed key, squeezed value, spaced lower-case key)]
        plain = []
        for key, value in mapping.items():
            if not isinstance(key, str) or not isinstance(value, str) or key == value:
                continue
            spaced = " ".join(key.split())
            squeezed = spaced.replace(" ", "")
            if len(squeezed) < 2:
                continue
            self.keys.setdefault(_lower(squeezed), []).append((squeezed, "".join(value.split()), _lower(spaced)))
            # "J. DOE" is also written "J DOE": the same pair without the periods
            # that follow a letter (initials, abbreviations), unless that is a key itself
            new = " ".join(value.split())
            drop = [i for i, ch in enumerate(spaced) if ch == "." and i and spaced[i - 1].isalpha()]
            if drop and len(new) == len(spaced):
                plain.append(("".join([ch for i, ch in enumerate(spaced) if i not in drop]),
                              "".join([ch for i, ch in enumerate(new) if i not in drop])))
        for spaced, new in plain:
            spaced, squeezed = " ".join(spaced.split()), "".join(spaced.split())
            if len(squeezed) >= 4 and _lower(squeezed) not in self.keys:
                self.keys[_lower(squeezed)] = [(squeezed, "".join(new.split()), _lower(spaced))]
        self.auto = self.rx = None
        self.hit = set()  # keys found at least once
        if not self.keys:
            return
        if ahocorasick is not None:
            self.auto = ahocorasick.Automaton()
            for key in self.keys:
                self.auto.add_word(key, key)
            self.auto.make_automaton()
        else:
            self.rx = re.compile("|".join(re.escape(k) for k in sorted(self.keys, key=len, reverse=True)))

    def find(self, text):
        """[(start, end, per-character replacement or None)] of the identifiers in text."""
        if not self.keys or len(text) < 2:
            return []
        low = _lower(text)
        spans = [m.span() for m in _NONSPACE.finditer(low)]
        if not spans:
            return []
        if len(spans) == 1:
            squeezed, starts = low[spans[0][0]:spans[0][1]], [0]
        else:
            squeezed = "".join([low[a:b] for a, b in spans])
            starts, at = [], 0
            for a, b in spans:
                starts.append(at)
                at += b - a
        if self.auto is not None:
            cands = [(end - len(key) + 1, end + 1, key) for end, key in self.auto.iter(squeezed)]
        else:
            cands = [(m.start(), m.end(), m.group()) for m in self.rx.finditer(squeezed)]
        if not cands:
            return []
        cands.sort(key=lambda c: (c[0], c[0] - c[1]))
        exact, loose = [], []
        for s, e, key in cands:
            k = bisect.bisect_right(starts, s) - 1
            a = spans[k][0] + s - starts[k]
            k = bisect.bisect_right(starts, e - 1) - 1
            b = spans[k][0] + e - starts[k]
            if a > 0 and _clash(text[a], text[a - 1]):
                continue
            if b < len(text) and _clash(text[b - 1], text[b]):
                continue
            entries = self.keys[key]
            seen = text[a:b]
            pieces = low[a:b].split()
            strict = len(pieces) == 1 or any(" ".join(pieces) == ent[2] for ent in entries)
            if not strict:
                # The words are spaced differently from the identifier. Fine for a name
                # written out in capitalised words ("northtoolworks" / "North Tool Works"),
                # and when the text is letter-spaced (every glyph placed on its own) and
                # its real spaces agree; otherwise these are other words that only happen
                # to run together ("for man" is not "Forman").
                named = len(key) >= LOOSE_MIN and all(
                    len(piece) > 1 and (piece[0].isupper() or piece[0].isdigit()) for piece in seen.split())
                real = " ".join(low[a:b].replace(GAP, "").split())
                if not named and (max(map(len, pieces)) > 2
                                  or (" " in real and not any(real == ent[2] for ent in entries))):
                    continue
            flat, spaced = "".join(seen.split()), " ".join(pieces)
            ranked = sorted(entries, key=lambda ent: (ent[0] != flat, ent[2] != spaced))
            value = ranked[0][1] if ranked[0][0] == flat else _recase(ranked[0][1], flat)
            repl, i, last = [], 0, -1
            for at, ch in enumerate(seen):
                if ch.isspace():
                    repl.append(None)
                else:
                    repl.append(value[i] if i < len(value) else "")
                    i += 1
                    last = at
            if i < len(value) and last >= 0:
                repl[last] += value[i:]
            if all(r is None or r == ch for r, ch in zip(repl, seen)):
                continue
            self.hit.add(key)
            (exact if strict else loose).append((a, b, repl))
        # Identifiers that overlap ("John Smith", "Smith Ltd") become one replacement; where
        # they disagree, the one that starts first decides.
        out = []
        for a, b, repl in exact:
            if out and a < out[-1][1]:
                pa, pb, prepl = out[-1]
                if b > pb:
                    out[-1] = (pa, b, prepl + repl[pb - a:])
            else:
                out.append((a, b, repl))
        if not loose:
            return out
        # Differently spaced matches come second: they only fill what the others left.
        merged, i = [], 0
        for a, b, repl in loose:
            while i < len(out) and out[i][1] <= a:
                merged.append(out[i])
                i += 1
            if merged and merged[-1][1] > a:
                lo, hi, new = merged.pop()
                if b > hi:
                    new = new + repl[hi - a:]
                    hi = b
            else:
                lo, hi, new = a, b, list(repl)
            while i < len(out) and out[i][0] < hi:
                ha, hb, hrepl = out[i]
                i += 1
                if ha < lo:
                    new[0:0] = [None] * (lo - ha)
                    lo = ha
                if hb > hi:
                    new.extend([None] * (hb - hi))
                    hi = hb
                new[ha - lo:hb - lo] = hrepl
            merged.append((lo, hi, new))
        merged.extend(out[i:])
        return merged

    def unmatched(self):
        """[(key, pattern)] for the identifiers that were found nowhere, for loose()."""
        keys = [k for k in self.keys if k not in self.hit and len(k) >= 5]
        if not keys or len(keys) > LOOSE_KEYS:
            return []
        return [(k, re.compile("".join(["(?:%s|%s)" % (re.escape(c), WILD) for c in k]))) for k in keys]

    def loose(self, text, wanted):
        """Hits of identifiers that occur nowhere as they are, with every accented letter of
        the text standing for any character. Some files (Inventor drawings) make text
        extraction misread accented letters, so an identifier taken from extracted text
        reads 'Mkller' where the page says 'Müller'. At most a third of an identifier
        may be matched this way, and its words must be split like the text's."""
        low = _lower(text)
        if not _ACCENTED.search(low):
            return []
        spans = [m.span() for m in _NONSPACE.finditer(low)]
        squeezed = "".join([low[a:b] for a, b in spans])
        starts, at = [], 0
        for a, b in spans:
            starts.append(at)
            at += b - a
        folded = _ACCENTED.sub(WILD, squeezed)
        out = []
        for key, rx in wanted:
            for m in rx.finditer(folded):
                s, e = m.span()
                wild = folded.count(WILD, s, e)
                if not wild or 3 * wild > len(key):
                    continue
                k = bisect.bisect_right(starts, s) - 1
                a = spans[k][0] + s - starts[k]
                k = bisect.bisect_right(starts, e - 1) - 1
                b = spans[k][0] + e - starts[k]
                if (a > 0 and _clash(text[a], text[a - 1])) or (b < len(text) and _clash(text[b - 1], text[b])):
                    continue
                seen = text[a:b]
                shape = [len(piece) for piece in seen.split()]
                entry = next((ent for ent in self.keys[key]
                              if len(shape) == 1 or shape == [len(w) for w in ent[2].split()]), None)
                if entry is None:
                    continue
                flat = "".join(seen.split())
                value = _recase(entry[1], flat)
                repl, i = [], 0
                for ch in seen:
                    if ch.isspace():
                        repl.append(None)
                        continue
                    # a letter the pseudonym keeps stays as the page has it
                    same = i < len(value) and i < len(entry[0]) and value[i].lower() == entry[0][i].lower()
                    repl.append(None if same else value[i] if i < len(value) else "")
                    i += 1
                if any(r is not None and r != ch for r, ch in zip(repl, seen)):
                    out.append((a, b, repl))
        out.sort(key=lambda h: (h[0], h[0] - h[1]))
        return [h for i, h in enumerate(out) if not i or h[0] >= out[i - 1][1]]

    def sub(self, text):
        hits = self.find(text)
        if not hits:
            return text, 0
        out, pos = [], 0
        for a, b, repl in hits:
            out.append(text[pos:a])
            out.append("".join([text[a + i] if r is None else r for i, r in enumerate(repl)]))
            pos = b
        out.append(text[pos:])
        return "".join(out), len(hits)


def _inherited(obj, key):
    for _ in range(100000):  # some page trees are one long chain
        value = obj.get(key)
        if value is not None:
            return value
        obj = obj.get("/Parent")
        if not isinstance(obj, Dictionary):
            return None
    return None


def _flatten(pdf):
    """Give the document a one-level page tree. qpdf refuses to read or write very deep page
    trees (a chain of /Pages nodes); the page objects themselves, and with them every link,
    annotation and outline target, stay the same objects."""
    pages, seen, todo = [], set(), [pdf.Root.get("/Pages")]
    while todo:
        node = todo.pop()
        if not isinstance(node, Dictionary) or not node.is_indirect or node.objgen in seen:
            continue
        seen.add(node.objgen)
        kids = node.get("/Kids")
        if isinstance(kids, Array) and node.get("/Type") != Name.Page:
            todo.extend(reversed(list(kids)))
        else:
            pages.append(node)
    for page in pages:
        for key in ("/Resources", "/MediaBox", "/CropBox", "/Rotate"):
            if key not in page:
                value = _inherited(page, key)
                if value is not None:
                    page[key] = value
    root = pdf.make_indirect(Dictionary(Type=Name.Pages, Kids=Array(pages), Count=len(pages)))
    for page in pages:
        page["/Parent"] = root
    pdf.Root["/Pages"] = root


class _Doc:
    def __init__(self, pdf, matcher, notes):
        self.pdf, self.matcher, self.notes = pdf, matcher, notes
        self.fonts = {}
        self.parents = None
        self.written = {}
        self.standard = {}  # base font name -> the font object added for fallback words
        self.fallbacks = self.failed = 0

    def note(self, text):
        if self.notes is not None and text not in self.notes:
            self.notes.append(text)

    # -- fonts ---------------------------------------------------------------------
    def font_of(self, fobj):
        ident = fobj.objgen if fobj.is_indirect else None
        font = self.fonts.get(ident) if ident else None
        if font is None:
            font = _Font(fobj)
            if ident:
                self.fonts[ident] = font
        return font

    def getter(self, res):
        """Font lookup for one resource dictionary: by /Font name, and (.gs) by the name of
        an extended graphics state that sets a font."""
        fonts = res.get("/Font") if isinstance(res, Dictionary) else None
        states = res.get("/ExtGState") if isinstance(res, Dictionary) else None
        cache, gcache = {}, {}

        def key_of(name):
            return "/" + _NAME_ESC.sub(lambda m: bytes((int(m.group(1), 16),)), name).decode("latin-1")

        def get(name):
            if name in cache:
                return cache[name]
            font = None
            if isinstance(fonts, Dictionary):
                try:
                    fobj = fonts.get(key_of(name))
                    if isinstance(fobj, Dictionary):
                        font = self.font_of(fobj)
                except Exception as exc:  # a broken font dictionary: its text cannot be read
                    self.note("font %s could not be read (%s)" % (name.decode("latin-1"), type(exc).__name__))
                    font = None
            cache[name] = font
            return font

        def gs(name):
            if name in gcache:
                return gcache[name]
            out = None
            try:
                state = states.get(key_of(name)) if isinstance(states, Dictionary) else None
                entry = state.get("/Font") if isinstance(state, Dictionary) else None
                if isinstance(entry, Array) and len(entry) == 2 and isinstance(entry[0], Dictionary):
                    fobj, rname = entry[0], b""
                    if isinstance(fonts, Dictionary) and fobj.is_indirect:
                        for key in fonts.keys():  # a /Font name for it, to select it again later
                            value = fonts.get(key)
                            if isinstance(value, Dictionary) and value.is_indirect and value.objgen == fobj.objgen:
                                rname = key[1:].encode("latin-1")
                                break
                        else:
                            i = 0
                            while "/DeidGs%d" % i in fonts:
                                i += 1
                            fonts["/DeidGs%d" % i] = fobj
                            rname = b"DeidGs%d" % i
                    out = (self.font_of(fobj), float(entry[1]), rname)
            except Exception as exc:  # a broken graphics state: its font is not known
                self.note("graphics state %s could not be read (%s)" % (name.decode("latin-1"), type(exc).__name__))
            gcache[name] = out
            return out

        get.gs = gs
        return get

    def marked(self, page):
        """For a page: MCID -> _Span of the structure element that gives the marked-content
        sequence an /ActualText (Word writes one per text piece), or None."""
        if self.parents is None:
            self.parents = {}
            root = self.pdf.Root.get("/StructTreeRoot")
            todo = [root.get("/ParentTree")] if isinstance(root, Dictionary) else []
            seen = 0
            while todo and seen < 100000:
                node = todo.pop()
                seen += 1
                if not isinstance(node, Dictionary):
                    continue
                nums = node.get("/Nums")
                if isinstance(nums, Array):
                    for i in range(0, len(nums) - 1, 2):
                        self.parents[int(nums[i])] = nums[i + 1]
                kids = node.get("/Kids")
                if isinstance(kids, Array):
                    todo.extend(kids)
        key = page.get("/StructParents")
        elems = self.parents.get(int(key)) if key is not None else None
        if not isinstance(elems, Array):
            return None
        cache = {}

        def get(mcid):
            if mcid not in cache:
                sp = None
                try:
                    elem = elems[mcid] if 0 <= mcid < len(elems) else None
                    text = elem.get("/ActualText") if isinstance(elem, Dictionary) else None
                    if isinstance(text, String):
                        sp = _Span(_text_of(bytes(text)), "struct", -1 - mcid, 0, elem)
                except Exception as exc:  # a damaged structure tree: no ActualText from it
                    self.note("structure tree could not be read (%s)" % type(exc).__name__)
                cache[mcid] = sp
            return cache[mcid]
        return get

    def fallback_name(self, res, base):
        """Resource name of the standard font `base` in res (added on first use), or None."""
        if not isinstance(res, Dictionary):
            return None
        fonts = res.get("/Font")
        if not isinstance(fonts, Dictionary):
            res["/Font"] = Dictionary()
            fonts = res["/Font"]
        font = self.standard.get(base)
        if font is None:
            font = self.standard[base] = self.pdf.make_indirect(Dictionary(
                Type=Name.Font, Subtype=Name.Type1, BaseFont=Name("/" + base), Encoding=Name.WinAnsiEncoding))
        for key in fonts.keys():
            value = fonts.get(key)
            if isinstance(value, Dictionary) and value.is_indirect and value.objgen == font.objgen:
                return key[1:].encode("latin-1")
        stem = "/" + _FALLBACK[base][0]
        name, i = stem, 0
        while name in fonts:
            i += 1
            name = "%s%d" % (stem, i)
        fonts[name] = font
        return name[1:].encode("latin-1")

    # -- which streams -------------------------------------------------------------
    def units(self):
        """(streams, resources, page) for the page contents and every form XObject."""
        units, seen = [], set()
        for page in self.pdf.pages:
            obj = page.obj
            res = _inherited(obj, "/Resources")
            contents = obj.get("/Contents")
            if isinstance(contents, Stream):
                units.append(([contents], res, obj))
            elif isinstance(contents, Array):
                streams = [s for s in contents if isinstance(s, Stream)]
                if streams:
                    units.append((streams, res, obj))
            self.nested(res, seen, units, 0)
            annots = obj.get("/Annots")
            if not isinstance(annots, Array):
                continue
            for annot in annots:
                ap = annot.get("/AP") if isinstance(annot, Dictionary) else None
                if not isinstance(ap, Dictionary):
                    continue
                for key in ("/N", "/R", "/D"):
                    value = ap.get(key)
                    if isinstance(value, Stream):
                        self.form(value, res, seen, units, 0)
                    elif isinstance(value, Dictionary):
                        for state in value.keys():
                            if isinstance(value.get(state), Stream):
                                self.form(value.get(state), res, seen, units, 0)
        for obj in self.pdf.objects:
            if isinstance(obj, Stream) and obj.get("/Subtype") == Name.Form:
                self.form(obj, None, seen, units, 0)
        return units

    def nested(self, res, seen, units, depth):
        if not isinstance(res, Dictionary) or depth > 16:
            return
        for group in ("/XObject", "/Pattern"):
            items = res.get(group)
            if not isinstance(items, Dictionary):
                continue
            for key in items.keys():
                item = items.get(key)
                if isinstance(item, Stream) and (item.get("/Subtype") == Name.Form or item.get("/PatternType") == 1):
                    self.form(item, res, seen, units, depth + 1)

    def form(self, stream, parent_res, seen, units, depth):
        ident = stream.objgen
        if ident in seen:
            return
        seen.add(ident)
        own = stream.get("/Resources")
        units.append(([stream], own if isinstance(own, Dictionary) else parent_res, None))
        if isinstance(own, Dictionary):
            self.nested(own, seen, units, depth)

    # -- content streams -----------------------------------------------------------
    def load(self, streams, res, page):
        """Read and walk one unit: (datas, data, text, runs, ops, dstr, alts, dups), or None."""
        try:
            datas = [s.read_bytes() for s in streams]
        except Exception as exc:  # a filter qpdf cannot undo: the stream stays as it is
            self.note("a content stream could not be decoded (%s)" % type(exc).__name__)
            return None
        data = datas[0] if len(datas) == 1 else b"\n".join(datas)
        if not _TEXTY.search(data):
            return None
        getfont = self.getter(res)
        if page is not None:
            try:
                getfont.mc = self.marked(page)
            except Exception as exc:  # a damaged structure tree: no ActualText from it
                self.note("structure tree could not be read (%s)" % type(exc).__name__)
        return (datas, data) + _interpret(data, getfont)

    def run(self):
        kept, texts = {}, []
        for number, (streams, res, page) in enumerate(self.units()):
            loaded = self.load(streams, res, page)
            if loaded is None:
                continue
            datas, data, text, runs, ops, dstr, alts, dups = loaded
            hits = self.matcher.find(text) if text else []
            if alts:  # the same text as MuPDF reads it
                chars = list(text)
                for at, other in alts:
                    chars[at:at + len(other)] = other
                extra = self.matcher.find("".join(chars))
                hits.extend(h for h in extra if not any(h[0] < b and a < h[1] for a, b, _r in hits))
                hits.sort(key=lambda h: h[0])
            if text:
                texts.append((number, streams, res, page, text))
            if hits or dstr:
                kept[number] = [streams, res, page, datas, data, runs, ops, dstr, hits, dups]
        wanted = self.matcher.unmatched()
        for number, streams, res, page, text in texts if wanted else ():
            extra = self.matcher.loose(text, wanted)
            if not extra:
                continue
            if number not in kept:
                datas, data, text, runs, ops, dstr, _alts, dups = self.load(streams, res, page)
                kept[number] = [streams, res, page, datas, data, runs, ops, dstr, [], dups]
            hits = kept[number][8]
            hits.extend(h for h in extra if not any(h[0] < b and a < h[1] for a, b, _r in hits))
            hits.sort(key=lambda h: h[0])
        kept = [kept[number] for number in sorted(kept)]
        total = 0
        for streams, res, page, datas, data, runs, ops, dstr, hits, dups in kept:
            edits, count, linked = self.edits(res, data, runs, ops, hits, dups)
            for kind, a, b in dstr:
                if a in linked:
                    continue
                raw = _unescape(data[a + 1:b - 1]) if kind == "lit" else _unhex(data[a + 1:b - 1])
                new = self.text_string(raw)
                if new is not None:
                    edits.append((a, b, _string(new, kind == "hex")))
                    count += 1
            if edits:
                self.write(streams, page, datas, data, edits)
                total += count
        total += self.strings()
        if self.fallbacks:
            self.note("%d word(s) written with a standard font (Helvetica, Times or Courier): their own font "
                      "lacks a glyph" % self.fallbacks)
        if self.failed:
            self.note("%d occurrence(s) could not be rewritten" % self.failed)
        return total

    def edits(self, res, data, runs, ops, hits, dups):
        """Byte edits (start, end, new bytes) of one stream for its hits, and how many took."""
        starts = [r[0] for r in runs]
        # operator -> item -> glyph -> (new text, word number, kind). Kinds: 0 the glyph is
        # replaced where it is; 1 it takes a whole word that was spread over several
        # operators; 2 and 3 its text moved to that first glyph (3: in a later operator);
        # 4 a glyph of a replaced word that keeps its text (it follows the word's font).
        changed, owner, target, forced = {}, [], {}, set()

        def glyphs_at(opi, itemi):
            op = ops[opi]
            glyphs = op.font.glyphs(op.items[itemi][0])
            sp = op.over.get(itemi) if op.over else None
            if sp is not None and not sp.plain:  # ActualText: the first glyph carries all of it
                text = sp.text if sp.head == (opi, itemi) else ""
                glyphs = [(code, text if i == 0 else "") for i, (code, _shown) in enumerate(glyphs)]
            return glyphs

        def put(opi, itemi, gi, new, kind):
            changed.setdefault(opi, {}).setdefault(itemi, {})[gi] = (new, len(owner) - 1, kind)

        def advance(op, code):
            """Advance of a glyph in 1/1000 of the font size, Tc and Tw included."""
            if not op.fsize:
                return op.font.width(code)
            extra = op.tc + (op.tw if code == 32 and not op.font.two else 0.0)
            return op.font.width(code) + extra * 1000.0 / op.fsize

        for hno, (ms, me, repl) in enumerate(hits):
            words, expect = [[]], None
            i = bisect.bisect_right(starts, ms) - 1
            while 0 <= i < len(runs) and runs[i][0] < me:
                pos, opi, itemi, g0, gn, joined = runs[i]
                if expect is not None and (pos > expect or not joined) and words[-1]:
                    # a gap on the page (the next word of a phrase, or its next line, even
                    # when a space glyph already separates them in the text)
                    words.append([])
                glyphs = glyphs_at(opi, itemi)
                for gi in range(g0, min(g0 + gn, len(glyphs))):
                    code, shown = glyphs[gi]
                    nxt = pos + len(shown)
                    if nxt > ms and pos < me and shown:
                        new = "".join([shown[p - pos] if not ms <= p < me or repl[p - ms] is None else repl[p - ms]
                                       for p in range(pos, nxt)])
                        words[-1].append((opi, itemi, gi, new, shown, code))
                    pos = nxt
                    if pos >= me:
                        break
                expect = pos
                i += 1
            for word in words:
                if all(g[3] == g[4] for g in word):
                    continue
                owner.append(hno)
                head = ops[word[0][0]]
                whole = "".join(g[3] for g in word)
                one = word[0][0] == word[-1][0]
                # Pieces with their own clipping path or size (Word writes text this way) are
                # rewritten where they are; so is a word that neither its first font nor a
                # standard font can show in full. Otherwise the word is written as a whole
                # where it starts (Inventor places every glyph on its own, accented ones in
                # a second font).
                apart = not one and any(ops[g[0]].clip != head.clip or ops[g[0]].fsize != head.fsize for g in word)
                if not one and not apart and any(head.font.encode(ch, head.tr == 3) is None for ch in whole):
                    try:
                        whole.encode("cp1252")
                    except UnicodeEncodeError:
                        apart = True
                if one or apart:
                    for opi, itemi, gi, new, shown, _code in word:
                        put(opi, itemi, gi, new, 0 if new != shown else 4)
                        if apart and new != shown and any(
                                ops[opi].font.encode(ch, ops[opi].tr == 3) is None for ch in new):
                            forced.add(len(owner) - 1)  # one font for the whole word
                else:
                    put(word[0][0], word[0][1], word[0][2], whole, 1)
                    target[len(owner) - 1] = sum(advance(ops[g[0]], g[5]) for g in word)
                    for opi, itemi, gi, _new, _shown, _code in word[1:]:
                        put(opi, itemi, gi, "", 2 if opi == word[0][0] else 3)
        for (opi, itemi), (first_op, first_item) in dups.items():
            again = changed.get(first_op, {}).get(first_item)
            if again:  # the bold repeat of a replaced string gets the same new text
                changed.setdefault(opi, {})[itemi] = dict(again)
        edits, good, bad, linked = [], set(), set(), set()
        dead = set()  # words that could not be written: their glyphs all stay
        spans = {}
        for opi in changed:
            if ops[opi].over:
                for itemi in changed[opi]:
                    sp = ops[opi].over.get(itemi)
                    if sp is not None:
                        spans[id(sp)] = sp
        for sp in spans.values():
            if sp.plain or sp.head is None:
                continue
            opi, itemi = sp.head
            entry = changed.get(opi, {}).get(itemi, {}).get(0)
            if entry is None or entry[2] == 4:
                continue
            # the ActualText follows its glyphs; the other glyphs of the sequence go, the
            # first one now shows all of the new text
            self.retext(sp, entry[0], edits, linked)
            for mopi, mitemi in sp.members:
                for gi in range(len(ops[mopi].font.glyphs(ops[mopi].items[mitemi][0]))):
                    if (mopi, mitemi, gi) != (opi, itemi, 0):
                        changed.setdefault(mopi, {}).setdefault(mitemi, {}).setdefault(
                            gi, ("", entry[1], 2 if mopi == opi else 3))
        for sp in spans.values():
            if not sp.plain:
                continue
            parts = []
            for mopi, mitemi in sp.members:
                per_glyph = changed.get(mopi, {}).get(mitemi, {})
                for gi, (_code, shown) in enumerate(glyphs_at(mopi, mitemi)):
                    entry = per_glyph.get(gi)
                    parts.append(shown if entry is None or entry[2] == 4 else entry[0])
            new = "".join(parts) + sp.text[sp.pos:]
            if new != sp.text:
                self.retext(sp, new, edits, linked)
        for opi in sorted(changed):
            op, per_item = ops[opi], changed[opi]
            font, invisible = op.font, op.tr == 3
            glyphs_of = {itemi: glyphs_at(opi, itemi) for itemi in per_item}
            fallback, first, last = set(), {}, {}
            for itemi in sorted(per_item):
                for gi in sorted(per_item[itemi]):
                    new, wid, kind = per_item[itemi][gi]
                    if kind == 2 or kind == 3:
                        continue
                    first.setdefault(wid, (itemi, gi))
                    last[wid] = (itemi, gi)
                    if wid in forced or (kind != 4 and any(font.encode(ch, invisible) is None for ch in new)):
                        fallback.add(wid)
            fbname = self.fallback_name(res, font.fallback) if fallback and op.fname else None
            # Horizontal scaling that makes every new word exactly as wide as the old one,
            # so that whatever follows it keeps its place and its distance.
            per_size = 1000.0 / op.fsize if op.fsize else 0.0
            old_adv, new_adv = {}, {}
            for itemi, per_glyph in per_item.items():
                for gi, (new, wid, kind) in per_glyph.items():
                    if kind == 2 or kind == 3:
                        continue
                    was = target[wid] if kind == 1 else advance(op, glyphs_of[itemi][gi][0])
                    if wid in fallback:
                        now = was
                        if fbname is not None:
                            now = _fb_width(new, font.fallback) + (len(new) * op.tc + new.count(" ") * op.tw) * per_size
                    elif kind == 4:
                        now = was
                    else:
                        now = sum(advance(op, font.encode(ch, invisible)) for ch in new)
                    old_adv[wid] = old_adv.get(wid, 0.0) + was
                    new_adv[wid] = new_adv.get(wid, 0.0) + now
            ratio = {}
            for wid, was in old_adv.items():
                now = new_adv[wid]
                if font.metric and not font.vertical and was > 0 and now > 0 and not (font.type3 and wid in fallback):
                    r = min(2.0, max(0.5, was / now))
                    if abs(r - 1.0) >= 0.02:
                        ratio[wid] = r
            segs, plain, gone = [], True, 0.0
            for itemi, item in enumerate(op.items):
                per_glyph = per_item.get(itemi) if isinstance(item, tuple) else None
                if not per_glyph:
                    if gone and isinstance(item, tuple) and item[0]:
                        segs.append(_fmt(-gone))  # the glyphs that follow keep their place
                        gone, plain = 0.0, False
                    segs.append(item)
                    continue
                as_hex = item[1]
                acc, fb_text, fb_old, pieces = bytearray(), [], 0.0, []

                def flush():
                    nonlocal acc, fb_text, fb_old
                    if acc:
                        pieces.append((bytes(acc), as_hex))
                        acc = bytearray()
                    if fb_text:
                        pieces.append(["".join(fb_text), fb_old])
                        fb_text, fb_old = [], 0.0

                for gi, (code, shown) in enumerate(glyphs_of[itemi]):
                    entry = per_glyph.get(gi)
                    if entry is None:
                        if fb_text or gone:
                            flush()
                        if gone:
                            pieces.append(_fmt(-gone))
                            gone = 0.0
                        acc += font.pack(code)
                        continue
                    new, wid, kind = entry
                    if (kind == 2 or kind == 3) and wid in dead:
                        entry = None  # its word was not moved after all
                    elif kind == 2 or kind == 3:
                        if kind == 3 and op.moved and font.metric:
                            gone += advance(op, code)
                        continue
                    if entry is None:
                        if fb_text or gone:
                            flush()
                        if gone:
                            pieces.append(_fmt(-gone))
                            gone = 0.0
                        acc += font.pack(code)
                        continue
                    scale = ratio.get(wid)
                    if scale and first[wid] == (itemi, gi):
                        flush()
                        pieces.append(_Tz(op.th * 100.0 * scale))
                    helv = False
                    if wid in fallback:
                        try:
                            new.encode("cp1252")
                            helv = fbname is not None
                        except UnicodeEncodeError:  # not a character of the fallback font
                            helv = False
                        if not helv and kind != 4:
                            bad.add(owner[wid])
                            dead.add(wid)
                    if helv:
                        if acc:
                            flush()
                        fb_text.append(new)
                        fb_old += target[wid] if kind == 1 else advance(op, code)
                        good.add(owner[wid])
                    else:
                        if fb_text:
                            flush()
                        if wid in fallback or kind == 4:
                            acc += font.pack(code)  # stays as it was
                        else:
                            for ch in new:
                                acc += font.pack(font.encode(ch, invisible))
                            good.add(owner[wid])
                    if scale and last[wid] == (itemi, gi):
                        flush()
                        pieces.append(_Tz(op.th * 100.0))
                flush()
                if not pieces:
                    pieces.append((b"", as_hex))
                if len(pieces) != 1 or not isinstance(pieces[0], tuple):
                    plain = False
                    segs.extend(pieces)
                else:
                    segs.append((pieces[0][0], as_hex, item[2], item[3], True))
            if gone and opi + 1 < len(ops) and not ops[opi + 1].moved:
                segs.append(_fmt(-gone))  # the next operator continues from this pen position
                plain = False
            if plain:  # same operator, same operands: only the strings change
                for seg in segs:
                    if isinstance(seg, tuple) and len(seg) == 5:
                        edits.append((seg[2], seg[3], _string(seg[0], seg[1])))
                continue
            out, arr = [], []
            if op.name == b"'" or (op.name == b'"' and not op.pre):
                out.append(b"T*")
            elif op.name == b'"':
                out.append(op.pre[0] + b" Tw " + op.pre[1] + b" Tc T*")
            for seg in segs:
                if isinstance(seg, tuple):
                    arr.append(_string(seg[0], seg[1]))
                    continue
                if isinstance(seg, bytes):
                    arr.append(seg)
                    continue
                if arr:
                    out.append(b"[" + b" ".join(arr) + b"]TJ")
                    arr = []
                if isinstance(seg, _Tz):
                    out.append(_fmt(seg) + b" Tz")
                    continue
                text, old_w = seg
                size = op.fsize
                if font.type3 and old_w > 0:  # a Type 3 font has no em size: fit the old width
                    size = op.fsize * old_w / _fb_width(text, font.fallback)
                out.append(b"/" + fbname + b" " + _fmt(size) + b" Tf")
                out.append(b"[" + _string(text.encode("cp1252"), False) + b"]TJ")
                out.append(b"/" + op.fname + b" " + _fmt(op.fsize) + b" Tf")
                self.fallbacks += 1
            if arr:
                out.append(b"[" + b" ".join(arr) + b"]TJ")
            # spaces on both sides: the operator before may end right at the old operand ("T*[")
            edits.append((op.start, op.end, b" " + b" ".join(out) + b" "))
        self.failed += len(bad - good)
        return edits, len(good), linked

    @staticmethod
    def retext(sp, text, edits, linked):
        """Give an ActualText its new text: in the stream, or in its structure element."""
        if sp.kind == "struct":
            sp.elem["/ActualText"] = String(_text_bytes(text))
        else:
            edits.append((sp.start, sp.end, _string(_text_bytes(text), sp.kind == "hex")))
            linked.add(sp.start)

    def write(self, streams, page, datas, data, edits):
        edits.sort()
        bounds, at = [], 0
        for d in datas:
            bounds.append((at, at + len(d)))
            at += len(d) + 1
        new_datas = []
        for a, b in bounds:
            mine = [ed for ed in edits if a <= ed[0] and ed[1] <= b]
            out, pos = [], a
            for s, e, new in mine:
                if s < pos:
                    continue
                out.append(data[pos:s])
                out.append(new)
                pos = e
            out.append(data[pos:b])
            new_datas.append(b"".join(out))
        if sum(1 for ed in edits if any(a <= ed[0] and ed[1] <= b for a, b in bounds)) != len(edits):
            # an operator that straddles two streams: the page gets one stream instead
            out, pos = [], 0
            for s, e, new in edits:
                if s < pos:
                    continue
                out.append(data[pos:s])
                out.append(new)
                pos = e
            out.append(data[pos:])
            new_datas = [b"".join(out)] + [b""] * (len(datas) - 1)
        replaced = list(streams)
        for i, (stream, old, new) in enumerate(zip(streams, datas, new_datas)):
            if new == old:
                continue
            ident = stream.objgen
            if ident not in self.written:
                stream.write(new)
                self.written[ident] = new
            elif self.written[ident] != new and page is not None:
                replaced[i] = self.pdf.make_stream(new)  # shared by pages that need different text
        if page is not None and any(a is not b for a, b in zip(replaced, streams)):
            page["/Contents"] = Array(replaced)

    # -- text strings outside content streams --------------------------------------
    def text_string(self, raw):
        """The bytes of a PDF text string with its identifiers replaced, or None."""
        if raw[:2] == b"\xfe\xff":
            codec, bom = "utf-16-be", raw[:2]
        elif raw[:2] == b"\xff\xfe":
            codec, bom = "utf-16-le", raw[:2]
        elif raw[:3] == b"\xef\xbb\xbf":
            codec, bom = "utf-8", raw[:3]
        else:
            codec, bom = "latin-1", b""
        try:
            new, count = self.matcher.sub(raw[len(bom):].decode(codec))
            return bom + new.encode(codec) if count else None
        except UnicodeError:  # not text in the encoding it claims: left alone
            return None

    def strings(self):
        info = self.pdf.trailer.get("/Info")
        skip = info.objgen if isinstance(info, Dictionary) and info.is_indirect else None
        total = 0
        for obj in self.pdf.objects:
            try:
                if isinstance(obj, Stream) or (isinstance(obj, Dictionary) and obj.objgen != skip):
                    total += self.walk(obj, 0)
            except Exception as exc:  # one odd object must not stop the others
                self.note("an object could not be searched (%s)" % type(exc).__name__)
        return total

    def walk(self, d, depth):
        total = 0
        for key in list(d.keys()):
            value = d.get(key)
            if isinstance(value, String):
                if key in _TEXT_KEYS:
                    new = self.text_string(bytes(value))
                    if new is not None:
                        d[key] = String(new)
                        total += 1
            elif depth < 6 and isinstance(value, Dictionary) and not value.is_indirect:
                total += self.walk(value, depth + 1)
            elif depth < 6 and isinstance(value, Array) and not value.is_indirect:
                for i, item in enumerate(value):
                    if isinstance(item, Dictionary) and not item.is_indirect:
                        total += self.walk(item, depth + 1)
                    elif isinstance(item, String) and key in _TEXT_ARRAYS:
                        new = self.text_string(bytes(item))
                        if new is not None:
                            value[i] = String(new)
                            total += 1
        return total


def _open(src, scratch, notes):
    """Open src with qpdf. A page tree qpdf refuses (too deep) is flattened in place, with
    the same page objects; only if that fails too is the tree rebuilt by MuPDF."""
    try:
        return pikepdf.open(src)
    except pikepdf.PasswordError:
        raise
    except pikepdf.PdfError:
        try:
            pdf = pikepdf.open(src, inherit_page_attributes=False)
            _flatten(pdf)
            len(pdf.pages)  # qpdf can walk the tree now, or raises again
            if notes is not None:
                notes.append("page tree flattened: it was too deep for qpdf")
            return pdf
        except pikepdf.PasswordError:
            raise
        except pikepdf.PdfError:
            if pymupdf is None:
                raise
    doc = pymupdf.open(src)
    try:
        doc.select(list(range(doc.page_count)))
        doc.save(scratch, no_new_id=True)  # no fresh random /ID: the output stays repeatable
    finally:
        doc.close()
    if notes is not None:
        notes.append("page tree rebuilt before rewriting")
    return pikepdf.open(scratch)


def _copy(src, dst):
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copyfile(src, dst)


def leftover(path, mapping, max_pages=0):
    """[(page number, identifier as found)] still in the page text of a PDF, as MuPDF's text
    extraction reads it: what a rewrite did not reach (text in fonts that cannot be read,
    or drawn in a way this module does not follow). [] when MuPDF is missing or cannot
    open the file."""
    if pymupdf is None:
        return []
    out = []
    try:
        matcher = _Matcher(mapping)
        doc = pymupdf.open(path)
        try:
            for pno, page in enumerate(doc):
                if max_pages and pno >= max_pages:
                    break
                text = " ".join(w[4] for w in page.get_text("words"))
                out.extend((pno, text[a:b]) for a, b, _repl in matcher.find(text))
        finally:
            doc.close()
    except Exception:  # unreadable for MuPDF: nothing can be said about its text
        return out
    return out


def replace(src, dst, mapping, notes=None):
    """Write src to dst with every identifier of mapping replaced by its pseudonym in the
    page text. Returns the number of occurrences replaced; never raises."""
    part = dst + ".part"
    try:
        matcher = _Matcher(mapping)
        total = 0
        if matcher.keys:
            with _open(src, part + ".tree", notes) as pdf:
                total = _Doc(pdf, matcher, notes).run()
                if total:
                    # deterministic_id: the same input and mapping give the same bytes
                    pdf.save(part, compress_streams=True, fix_metadata_version=False, deterministic_id=True,
                             object_stream_mode=pikepdf.ObjectStreamMode.preserve)
        if os.path.lexists(part + ".tree"):
            os.remove(part + ".tree")
        if total:
            os.replace(part, dst)
        else:
            _copy(src, dst)
        return total
    except Exception as exc:  # unreadable, encrypted or damaged: keep the file as it is
        if notes is not None:
            notes.append("page text not rewritten (%s: %s)" % (type(exc).__name__, str(exc)[:120]))
        try:
            for leftover in (part, part + ".tree"):
                if os.path.lexists(leftover):
                    os.remove(leftover)
            _copy(src, dst)
        except OSError as exc2:
            if notes is not None:
                notes.append("copy failed (%s)" % exc2)
        return 0
