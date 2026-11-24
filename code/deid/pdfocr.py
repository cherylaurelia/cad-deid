"""Identifiers that only show on the RENDERED page of a PDF: text drawn as outlines (Qt, cairo,
LibreOffice Draw, CGM exports), text inside page images and logos, Type3 glyphs.

redact(path, ctx, identities) is a post-pass on an already de-identified PDF (after the
page-text, metadata and pdfpost passes). Pages that can hold such text are rendered and read
with tesseract; the words are given to the same rules as a text layer (formats.PdfFix:
dictionary identifiers, project names, hosts, phones; pdfids on a scratch PDF holding the OCR
lines: title-block labels, copyright holders, companies, e-mail), plus identifiers inside
longer words and OCR misreadings. Every hit is covered on the page by a box of the background
colour with its same-length pseudonym in Helvetica; where the original is inside a page
image, the image pixels are painted too (a small flat logo that the words fill is blanked
whole). Words the text layer already holds at that place are left to the text passes, so
nothing gets a second pseudonym (with DEID_PDF_OCR_TEXT=1 a dictionary identifier that the
text layer still holds under its rendered word is removed there as well: names split over
text runs). Drawing templates recognised by the labels of their title block get its cells
(drawn by, logo, address, holder, project) read on their own at 600 dpi and covered.

Dimension callouts and number tables are never covered: a word without letters is only
replaced as part of a telephone number. The file is replaced through a temporary file; when
anything fails, or the result does not reopen with the same pages, links, annotations,
bookmarks and images shown, it is left as it was. The output depends only on the input bytes
(page counts, not seconds, bound the work).

Environment: DEID_PDF_OCR_LANGS (default eng+deu+spa+fra, the installed ones are used),
DEID_PDF_OCR_ART_PAGES (first pages read for logos beside a text layer, default 3),
DEID_PDF_OCR_TEXT=1 (see above), DEID_PDF_OCR_LOG=<private file> (one line per covered word;
it quotes the originals).
"""
import difflib
import io
import os
import re
import shutil
import subprocess
import sys

from . import formats, previews
from .pseudo import KEEP
from .scan import document_text_rules

TESSERACT = shutil.which("tesseract")
LANGS = "eng+deu+spa+fra"   # the installed ones of these are used (DEID_PDF_OCR_LANGS overrides)
DPI = 200
MAX_SIDE = 6000             # pixels: larger sheets are rendered smaller
MAX_PAGES = 150             # pages read per PDF (a count, not seconds: the output is repeatable)
MIN_CONF = 20               # tesseract word confidence below this is noise
HIT_CONF = 60               # a word only a pattern points at (label, notice, company form) must be read clearly,
SHORT_CONF = 80             # and one of up to three letters (initials) very clearly
LOW_WORDS = 12              # a page with fewer text-layer words has no real text layer
IMAGE_SHARE = 0.5           # images covering this share of the page: a scan
OUTLINE_FILLS = 200         # this many filled paths and ...
OUTLINE_RATIO = 8           # ... this many per text-layer word: text drawn as outlines
ART_PAGES = 3               # the first pages are also read when they hold pictures or filled
ART_FILLS = 20              # shapes next to their text: logos (DEID_PDF_OCR_ART_PAGES overrides)
MARGIN = 1.5                # points added around a covered word
LOGO_FILL = 0.10            # a small flat image is blanked whole when the words fill this share of it (a logo,
                            # not a screenshot that shows a name somewhere)
_FILL = re.compile(rb"(?<![A-Za-z0-9/.*'\"])(?:f\*?|F|B\*?|b\*?)(?=\s)")
_SQUEEZE = re.compile(r"[^a-z0-9]+")
_DIMENSION = re.compile(r"[^A-Za-z]*(?:[A-Za-z][^A-Za-z]*){0,2}")   # at most two letters
_PHONE_LABEL = re.compile(r"(?i)\b(?:tel|phone|fax|mobile|ph|telefon|tél)\b")
_LANGS = []


# -- which pages -------------------------------------------------------------------

def needs_ocr(page):
    """Why the rendered page can show text its text layer does not hold ('' when it cannot):
    'image' (a scan), 'type3', 'notext' (a drawing without a text layer), 'outlined', or
    'art' (one of the first pages, with pictures or filled shapes beside its text)."""
    import pymupdf as fitz

    area = abs(page.rect) or 1.0
    try:
        images = [fitz.Rect(i["bbox"]) & page.rect for i in page.get_image_info()]
    except Exception:
        images = []
    if sum(abs(r) for r in images) >= IMAGE_SHARE * area:
        return "image"
    if any(f[2] == "Type3" for f in page.get_fonts(full=True)):
        return "type3"
    words = len(page.get_text("words"))
    try:
        raw = page.read_contents()
    except Exception:
        raw = b""
    if words < LOW_WORDS:
        return "notext" if images or len(raw) > 2000 or page.get_xobjects() else ""
    fills = len(_FILL.findall(raw))
    if fills >= OUTLINE_FILLS and fills >= OUTLINE_RATIO * words:
        return "outlined"
    front = int(os.environ.get("DEID_PDF_OCR_ART_PAGES", ART_PAGES))
    if page.number < front and (fills >= ART_FILLS or any(r.width >= 16 and r.height >= 8 for r in images)):
        return "art"
    return ""


# -- reading the page ----------------------------------------------------------------

def _langs():
    if not _LANGS:
        done = subprocess.run([TESSERACT, "--list-langs"], capture_output=True, timeout=60)
        have = set((done.stdout + done.stderr).decode("utf-8", "replace").split())
        want = os.environ.get("DEID_PDF_OCR_LANGS", LANGS).split("+")
        _LANGS.append("+".join(l for l in want if l in have) or "eng")
    return _LANGS[0]


def _zoom(page):
    z = DPI / 72.0
    side = max(page.rect.width, page.rect.height) * z
    return z * MAX_SIDE / side if side > MAX_SIDE else z


def _render(page, zoom, grey=False):
    import pymupdf as fitz
    from PIL import Image

    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY if grey else fitz.csRGB, alpha=False)
    return Image.frombytes("L" if grey else "RGB", (pix.width, pix.height), pix.samples)


def ocr_lines(page):
    """Lines of the rendered page: [[(word, (x0, y0, x1, y1), confidence), ...]] in page points."""
    zoom = _zoom(page)
    buf = io.BytesIO()
    _render(page, zoom, grey=True).save(buf, "PNG", compress_level=1)
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    done = subprocess.run([TESSERACT, "stdin", "stdout", "--psm", "11", "-l", _langs(), "tsv"],
                          input=buf.getvalue(), capture_output=True, env=env, timeout=900)
    if done.returncode != 0:
        raise RuntimeError("tesseract failed: %s" % done.stderr[-200:].decode("latin-1"))
    lines = {}
    for row in done.stdout.decode("utf-8", "replace").splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5" or not cols[11].strip():
            continue
        try:
            x, y, w, h = (int(c) for c in cols[6:10])
            conf = float(cols[10])
        except ValueError:
            continue
        if conf < MIN_CONF or w < 2 or h < 2:
            continue
        lines.setdefault(tuple(cols[1:5]), []).append(
            (cols[11].strip(), (x / zoom, y / zoom, (x + w) / zoom, (y + h) / zoom), conf))
    return list(lines.values())


def _line_box(line):
    return (min(w[1][0] for w in line), min(w[1][1] for w in line),
            max(w[1][2] for w in line), max(w[1][3] for w in line))


# -- what to replace -----------------------------------------------------------------

def _inner_rx(ctx):
    """The DEID_PDF_INNER matcher of formats (distinctive identifiers of 5+ letters and known
    acronyms, found inside longer words too), whatever the flag says: OCR glues and splits
    words, so the rule is always on for rendered text."""
    flag = os.environ.get("DEID_PDF_INNER")
    os.environ["DEID_PDF_INNER"] = "1"
    try:
        return formats._inner_rx(ctx)
    finally:
        if flag is None:
            del os.environ["DEID_PDF_INNER"]
        else:
            os.environ["DEID_PDF_INNER"] = flag


def _near_word(t, _index={}):
    """True when t differs from an ordinary word by a letter or two: a misreading of it."""
    from . import pdfids

    if not _index:
        for w in pdfids._vocab()[0]:
            if len(w) >= 4:
                _index.setdefault((w[0], len(w)), []).append(w)
    near = [w for n in (len(t) - 1, len(t), len(t) + 1) for w in _index.get((t[0], n), ())]
    return bool(difflib.get_close_matches(t, near, 1, 0.85))


def _plausible(name):
    """A person or organisation the rules found in OCR text is kept when one of its words (5+
    letters) is no ordinary word, no label and no likely misreading of one: 'SPECIHED' and
    'MIC APPR' are readings of the drawing's own words, not names."""
    from . import pdfids

    for word in re.findall(r"[^\W\d_]{5,}", name):
        t = word.lower()
        if not (pdfids._is_word(t) or t in pdfids._D_NOISE or t in pdfids._LABEL_CLASS or _near_word(t)):
            return True
    return False


def _discovered(ctx, pages, path):
    """A matcher for the names that the title-block, notice and name rules of pdfids find in
    the OCR text (drawn by / checked / designer values, copyright and property-of holders,
    companies, institutes, e-mail, hosts, phones): the lines are written to a scratch PDF as
    a text layer and read like any other PDF. None when they find nothing."""
    import pymupdf as fitz
    from . import pdfids

    tmp = path + ".ocrtext"
    try:
        with fitz.open() as doc:
            for width, height, lines in pages:
                page = doc.new_page(width=width, height=height)
                shape = page.new_shape()
                for line in lines:
                    x0, y0, _x1, y1 = _line_box(line)
                    text = " ".join(w[0] for w in line).encode("latin-1", "replace").decode("latin-1")
                    shape.insert_text((x0, y1), text, fontsize=max(3.0, 0.9 * (y1 - y0)), fontname="helv")
                shape.commit()
            doc.save(tmp)
        found = pdfids.find_detailed(tmp)
    finally:
        if os.path.lexists(tmp):
            os.remove(tmp)
    # 'project' and 'part' are left out: in OCR text they are mostly the drawing's own title
    # and labels, misread or attached to the wrong cell
    names = sorted({s.strip() for kind, s in found
                    if kind in ("email", "url", "address", "phone") and 3 <= len(s.strip()) <= 80
                    or kind in ("person", "org") and 3 <= len(s.strip()) <= 80 and _plausible(s)},
                   key=len, reverse=True)
    bodies = [r"\.?\s*".join(re.escape(w.rstrip(".")) for w in s.split() if w.rstrip(".")) for s in names]
    for kind, s in found:              # a person's family name also on its own
        last = s.split()[-1].strip(".,") if kind == "person" and len(s.split()) >= 2 and _plausible(s) else ""
        if len(last) >= 4 and last.isalpha() and not pdfids._is_word(last.lower()) and last.lower() not in pdfids._D_NOISE:
            bodies.append(re.escape(last))
    bodies = ["(?i:%s)" % b for b in bodies if b]
    # initials in a title-block slot (3+ capitals), in their own case as in formats.PdfFix
    bodies += sorted({re.escape(s) for kind, s in found if kind == "initials" and len(s) >= 3 and s.isalpha()
                      and s.isupper() and s not in formats.INITIALS_NOT_PEOPLE})
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(bodies) + r")(?![A-Za-z0-9])") if bodies else None


def _extra_rx(identities):
    """A matcher for identities given as strings ({original: pseudonym} or an iterable)."""
    if identities is None or callable(identities) or hasattr(identities, "words"):
        return None, {}
    table = dict(identities) if isinstance(identities, dict) else {s: None for s in identities}
    bodies = [r"[\s_.-]*".join(re.escape(w) for w in re.findall(r"[A-Za-z0-9]+", s)) for s in table if len(s) >= 3]
    bodies = sorted((b for b in bodies if b), key=len, reverse=True)
    rx = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(bodies) + r")(?![A-Za-z0-9])", re.I) if bodies else None
    return rx, {k.lower(): v for k, v in table.items() if v}


def _plain(fix, text):
    """fix(text) without the DEID_PDF_INNER rule of formats.PdfFix: identifiers inside longer
    words are replaced by this module's own rule, which leaves ordinary words alone."""
    flag = os.environ.pop("DEID_PDF_INNER", None)
    try:
        return fix(text)
    finally:
        if flag is not None:
            os.environ["DEID_PDF_INNER"] = flag


def _rewriter(ctx, identities, pages, path):
    """text -> (same-length text with every identifier replaced by its pseudonym; the same
    with only what the dictionary scanner and the given identities replace; the same with
    only identifiers matched letter for letter: no e-mail or host pattern, so never a
    pseudonym that an earlier pass wrote)."""
    from . import pdfids

    pseudo = ctx.text.pseudo
    mark = len(ctx.text.found)
    try:
        # the rules of a PDF's text layer without a document to discover names in: hosts,
        # labelled phones, the dictionary, this project's account and project names
        fix = formats.PdfFix(ctx, path + ".none")
    except Exception as exc:
        print("pdfocr: %s: plain rules (%s)" % (os.path.basename(path), type(exc).__name__), file=sys.stderr)
        fix = lambda text: formats.combine(text, document_text_rules(text, pseudo), ctx.text.text(text))  # noqa: E731
    try:
        names = _discovered(ctx, pages, path)
    except Exception as exc:
        print("pdfocr: %s: no name discovery (%s)" % (os.path.basename(path), type(exc).__name__), file=sys.stderr)
        names = None
    del ctx.text.found[mark:]          # OCR readings are not evidence for the identity list
    inner = _inner_rx(ctx)
    extra, table = _extra_rx(identities)

    def named(m):
        new = table.get(m.group().lower())
        return new if new and len(new) == len(m.group()) else pseudo.span(m.group())

    def inside(m):
        """An identifier inside a longer token (a prefix letter glued on, a host name); an
        ordinary word that merely holds one (a dictionary word starting with an account
        name) stays."""
        a, b = m.span()
        while a > 0 and m.string[a - 1].isalpha():
            a -= 1
        while b < len(m.string) and m.string[b].isalpha():
            b += 1
        token = m.string[a:b].lower()
        if token != m.group().lower() and pdfids._is_word(token):
            return m.group()
        rule = getattr(formats, "_inner_sub", None)     # the boundary rule of newer formats
        return rule(m, pseudo) if rule else pseudo.span(m.group())

    def rewrite(text):
        literal = [inner.sub(inside, text)] if inner is not None else []
        if extra is not None:
            literal.append(extra.sub(named, text))
        known = [ctx.text.text(text)] + literal
        found = [_plain(fix, text)] + ([_plain(identities, text)] if callable(identities) else [])
        if names is not None:
            found.append(names.sub(lambda m: pseudo.span(m.group()), text))
        return (formats.combine(text, *(found + known)), formats.combine(text, *known),
                formats.combine(text, *literal))

    return rewrite


def _squeeze(s):
    return _SQUEEZE.sub("", s.lower())


def _in_text_layer(box, old, layer):
    """True when the page's text layer holds this word at this place: the text passes own it
    (it is the pseudonym they wrote, or a word they decided to keep)."""
    x0, y0, x1, y1 = box
    a = _squeeze(old)
    for wx0, wy0, wx1, wy1, word in layer:
        if wx1 <= x0 or wx0 >= x1 or wy1 <= y0 or wy0 >= y1:
            continue
        ix, iy = min(x1, wx1) - max(x0, wx0), min(y1, wy1) - max(y0, wy0)
        if ix * iy < 0.3 * min((x1 - x0) * (y1 - y0), (wx1 - wx0) * (wy1 - wy0)):
            continue
        b = _squeeze(word)
        if a and b and (a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.6):
            return True
    return False


def _phone_spans(text):
    """Character spans of telephone numbers: after a label, or in a telephone format."""
    from . import pdfids

    spans = []
    for m in pdfids._PHONE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        line = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
        if _PHONE_LABEL.search(line) or (len(digits) >= 10 and re.search(r"[()+]|\d{3}[.-]\d{3}[.-]\d{4}", m.group())):
            spans.append(m.span())
    return spans


def page_hits(lines, layer, rewrite, ctx, strip=None):
    """[(box, old, new)] in page points for one page's OCR lines. strip (a list, with
    DEID_PDF_OCR_TEXT=1): it receives the boxes of identifiers matched letter for letter that
    the text layer still holds at that place; they are hits too, and their glyphs are removed."""
    words, parts, pos = [], [], 0
    for line in lines:
        for i, (word, box, conf) in enumerate(line):
            words.append((word, box, pos, conf))
            parts.append(word + (" " if i + 1 < len(line) else "\n"))
            pos += len(word) + 1
    text = "".join(parts)
    new, known, literal = rewrite(text)
    if not len(new) == len(known) == len(literal) == len(text):
        return []
    phones = _phone_spans(text)
    hits, taken, named = [], [], set()
    for word, box, at, conf in words:
        nw = new[at:at + len(word)]
        if literal[at:at + len(word)] != word:
            named.add(box)
        if nw == word or (known[at:at + len(word)] == word and conf < (
                SHORT_CONF if sum(c.isalpha() for c in word) <= 3 else HIT_CONF)):
            continue                   # unchanged, or a doubtful reading that only a pattern points at
        if _DIMENSION.fullmatch(word) and (any(c.isdigit() for c in word) or not any(c.isalpha() for c in word)) \
                and not any(a <= at and at + len(word) <= b for a, b in phones):
            continue                   # 12.5, M6x1.0, R12, 10mm, 2x45: numbers and callouts keep their value
        hits.append((box, word, nw))
        taken.append(box)
    # glued neighbours and OCR misreadings of dictionary identifiers (previews)
    for box, old, nw in previews._hits([w[:2] for w in words], ctx):
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if not any(t[0] <= cx <= t[2] and t[1] <= cy <= t[3] for t in taken) and sum(c.isalpha() for c in old) >= 3:
            hits.append((box, old, nw))
    out = []
    for box, old, nw in hits:
        if box[2] - box[0] < 2 or box[3] - box[1] < 2 or len(nw) != len(old):
            continue
        if _in_text_layer(box, old, layer):
            if strip is None or box not in named:
                continue               # the text passes own it
            strip.append(box)          # a known identifier they left: text and picture both go
        out.append((box, old, nw))
    return out


# -- drawing templates with fixed title-block cells -----------------------------------

# A template is recognised by its page size and by the fixed labels of its title block read
# at their places (shares of the page; no name of anyone is needed, or kept in this file).
# Its cells are then read on their own at CELL_DPI (the names in them are too small for the
# page OCR) and covered: 'block' = the whole cell with the pseudonym of everything read in it
# (logo, address); 'names' = each word with letters (drawn by, designer, holder, project).
# A template may also be known by a logo image at a fixed place (no OCR needed for that).
# (producers, sizes, [(label, x0, y0)], labels needed, [(x0, y0, x1, y1, mode)], logo image box or None)
CELL_DPI = 600
ANCHOR_SLACK = 0.02
_LICENCE = re.compile(r"(?i)(?:cc|by|sa|nc|nd|inc|phone|tel|fax|usa|www|com)(?:[-.](?:by|sa|nc|nd|\d))*[.,:]?")
_A_SERIES, _LETTER, _A3 = ((842, 595), (1191, 842)), ((792, 612),), ((1191, 842),)
_OUTLINERS, _CGM = ("Qt 4.", "LibreOffice", "cairo"), ("PDFlib", "Microsoft")   # also sheets collected in Word
TEMPLATES = (
    # outlined title block, A4 / A3 landscape (Qt, LibreOffice Draw)
    (_OUTLINERS, _A_SERIES, [(r"DRAWN", 0.448, 0.762), (r"DATE", 0.521, 0.761), (r"MATERIAL", 0.452, 0.795),
                 (r"SIZE|DWG", 0.569, 0.905)], 2,
     [(0.446, 0.771, 0.517, 0.789, "names"), (0.583, 0.760, 0.723, 0.832, "block"), (0.748, 0.775, 0.883, 0.827, "block")],
     None),
    (_OUTLINERS, _A_SERIES, [(r"(?i)copyright", 0.027, 0.034)], 1, [(0.094, 0.030, 0.160, 0.050, "names")], None),
    # the same title block on US letter (cairo, LibreOffice Draw)
    (_OUTLINERS, _LETTER, [(r"DRAWN", 0.490, 0.762), (r"DATE", 0.570, 0.762), (r"MATERIAL", 0.495, 0.795),
               (r"SIZE|DWG", 0.623, 0.906)], 2,
     [(0.488, 0.771, 0.560, 0.789, "names"), (0.636, 0.760, 0.792, 0.833, "block"), (0.820, 0.775, 0.968, 0.828, "block")],
     None),
    (_OUTLINERS, _LETTER, [(r"(?i)copyright", 0.028, 0.032)], 1, [(0.102, 0.029, 0.174, 0.048, "names")], None),
    # CGM export, A3: designer, logo with the institute's name, project, licence holder
    (_CGM, _A3, [(r"(?i)signed by|zeichnet von", 0.563, 0.896), (r"(?i)projekt|project", 0.875, 0.891),
           (r"(?i)nderungsstand|change state", 0.565, 0.757), (r"(?i)werkstoff|material", 0.674, 0.757),
           (r"(?i)ckzahl|quantity", 0.674, 0.939)], 2,
     [(0.637, 0.8955, 0.6695, 0.9055, "names"), (0.566, 0.939, 0.672, 0.978, "block"),
      (0.872, 0.906, 0.945, 0.922, "names"), (0.862, 0.962, 0.992, 0.987, "names")],
     (0.568, 0.941, 0.670, 0.964)),
)


def template_cells(page, lines, producer):
    """Cells of every template whose labels the page OCR found at their places."""
    w, h = page.rect.width, page.rect.height
    heads = [(" ".join(w[0] for w in line), _line_box(line)) for line in lines]
    cells, images = [], None
    for producers, sizes, anchors, need, tcells, logo in TEMPLATES:
        if not producer.startswith(producers) or not any(abs(w - sw) < 2 and abs(h - sh) < 2 for sw, sh in sizes):
            continue
        seen = sum(1 for rx, ax, ay in anchors
                   if any(re.search(rx, text) and abs(b[0] / w - ax) < ANCHOR_SLACK and abs(b[1] / h - ay) < ANCHOR_SLACK
                          for text, b in heads))
        if seen < need and logo:
            if images is None:
                images = [i["bbox"] for i in page.get_image_info()]
            if any(all(abs(v / d - t) < 0.01 for v, d, t in zip(b, (w, h, w, h), logo)) for b in images):
                seen = need
        if seen >= need:
            cells.extend(tcells)
    return cells


def _read_clip(page, box):
    """Words inside box read at CELL_DPI: [(word, box in page points)] in reading order."""
    import pymupdf as fitz

    zoom = CELL_DPI / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=fitz.Rect(box), colorspace=fitz.csGRAY, alpha=False)
    if pix.width < 8 or pix.height < 8:
        return [], False
    low, high = min(pix.samples), max(pix.samples)
    if high - low < 80:
        return [], False                # an empty cell
    done = subprocess.run([TESSERACT, "stdin", "stdout", "--psm", "11", "-l", _langs(), "tsv"],
                          input=pix.tobytes("png"), capture_output=True, env=dict(os.environ, OMP_THREAD_LIMIT="1"),
                          timeout=300)
    if done.returncode != 0:
        raise RuntimeError("tesseract failed: %s" % done.stderr[-200:].decode("latin-1"))
    words = []
    for row in done.stdout.decode("utf-8", "replace").splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5" or not cols[11].strip():
            continue
        try:
            x, y, bw, bh = (int(c) for c in cols[6:10])
            conf = float(cols[10])
        except ValueError:
            continue
        if conf >= MIN_CONF and bw >= 2 and bh >= 2:
            words.append((cols[11].strip(), (box[0] + x / zoom, box[1] + y / zoom,
                                             box[0] + (x + bw) / zoom, box[1] + (y + bh) / zoom)))
    return sorted(words, key=lambda wb: (round(wb[1][1] / 4), wb[1][0])), True


def template_hits(page, cells, ctx):
    """([(box, old, new)], [block boxes]) for the cells of a recognised template."""
    pseudo = ctx.text.pseudo
    w, h = page.rect.width, page.rect.height
    hits, blocks = [], []
    for x0, y0, x1, y1, mode in cells:
        box = (x0 * w, y0 * h, x1 * w, y1 * h)
        words, ink = _read_clip(page, box)
        if not ink:
            continue
        text = " ".join(word for word, _ in words)
        new = pseudo.span(text)
        if mode == "block":
            named = [n for (word, _), n in zip(words, new.split(" ")) if sum(c.isalpha() for c in word) >= 3
                     and not _LICENCE.fullmatch(word)] if len(new.split(" ")) == len(words) else []
            label = " ".join(dict.fromkeys(named)) or pseudo.span(" ".join(getattr(ctx, "project", ())[:1]))
            hits.append((box, text or "(unread)", label))
            blocks.append(box)
            continue
        at, names, initials = 0, [], []
        for word, wbox in words:
            nw = new[at:at + len(word)]
            at += len(word) + 1
            if re.fullmatch(r"[A-Z]\.?", word):
                initials.append((wbox, word, pseudo.word(word[0]) + word[1:]))
            elif nw != word and any(c.isalpha() for c in word) and not _LICENCE.fullmatch(word) \
                    and not (_DIMENSION.fullmatch(word) and any(c.isdigit() for c in word)):
                names.append((wbox, word, nw))
        hits += names + (initials if names else [])     # an initial only next to a name
    return hits, blocks


# -- painting --------------------------------------------------------------------------

def _latin(text):
    return text.encode("latin-1", "replace").decode("latin-1")


def _own_resources(doc, page):
    """Give a page that takes its /Resources from the page tree an entry of its own (the same
    dictionary). PyMuPDF otherwise adds its font to a new, empty dictionary on the page, and
    the page loses its images, fonts and graphics states. True when the page was changed."""
    if doc.xref_get_key(page.xref, "Resources")[0] != "null":
        return False
    xref, seen = page.xref, set()
    while xref not in seen:
        seen.add(xref)
        kind, value = doc.xref_get_key(xref, "Parent")
        if kind != "xref":
            break
        xref = int(value.split()[0])
        kind, value = doc.xref_get_key(xref, "Resources")
        if kind != "null":
            doc.xref_set_key(page.xref, "Resources", value)
            return True
    return False


def _fit(text, width, height):
    """(font size, lines) for text in Helvetica inside width x height: one line with its
    capitals and descenders inside the box; the label of a block wraps when one line would
    be too small to read."""
    import pymupdf as fitz

    size = min(height / 0.93, 48.0)
    wide = fitz.get_text_length(text, fontname="helv", fontsize=size)
    if wide <= width:
        return size, [text]
    if " " not in text or size * width / wide >= 4.0:
        return max(2.0, size * width / wide), [text]
    while True:
        lines, line = [], ""
        for word in text.split():
            trial = (line + " " + word).strip()
            if line and fitz.get_text_length(trial, fontname="helv", fontsize=size) > width:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
        if size <= 2.0 or (1.15 * len(lines) * size <= height and all(
                fitz.get_text_length(l, fontname="helv", fontsize=size) <= width for l in lines)):
            return size, lines
        size = max(2.0, 0.9 * size)


def _paint_page(page, hits, im, zoom):
    """Cover each hit with the background colour around it and draw its pseudonym."""
    import pymupdf as fitz

    if not page.is_wrapped:
        page.wrap_contents()           # our drawing must not inherit the page's last transform
    shape = page.new_shape()
    for box, _old, new in hits:
        rect = fitz.Rect(box[0] - MARGIN, box[1] - MARGIN, box[2] + MARGIN, box[3] + MARGIN) & page.rect
        if rect.is_empty:
            continue
        px = (max(0, int(rect.x0 * zoom)), max(0, int(rect.y0 * zoom)),
              min(im.width, int(rect.x1 * zoom) + 1), min(im.height, int(rect.y1 * zoom) + 1))
        bg = previews._border_colour(im, px)
        fg = previews._ink_colour(im, px, bg)
        shape.draw_rect(rect)
        shape.finish(color=None, fill=[c / 255.0 for c in bg[:3]], width=0)
        new = _latin(new)
        if not new.strip():
            continue
        width, height = box[2] - box[0], box[3] - box[1]
        size, lines = _fit(new, width, height)
        ink = [c / 255.0 for c in fg[:3]]
        if len(lines) == 1:
            shape.insert_text((box[0], box[3] - 0.21 * size), new, fontsize=size, fontname="helv", color=ink)
        for i, line in enumerate(lines if len(lines) > 1 else ()):
            shape.insert_text((box[0], box[1] + (0.8 + 1.15 * i) * size), line, fontsize=size, fontname="helv", color=ink)
    shape.commit(overlay=True)


def _strip_text(page, boxes):
    """Remove the text-layer glyphs under the boxes: a thin band through the middle of each
    word, as formats.pdf_text does. Drawings, images, annotations and links stay (a link
    under a removed word is put back)."""
    import pymupdf as fitz

    notes = [a.rect for a in page.annots() or ()]
    count = 0
    for x0, y0, x1, y1 in boxes:
        if any(fitz.Point((x0 + x1) / 2, (y0 + y1) / 2) in r for r in notes):
            continue                   # text of an annotation: the text passes handle its content
        page.add_redact_annot(fitz.Rect(x0 + 0.5, (y0 + y1) / 2 - 0.4, x1 - 0.5, (y0 + y1) / 2 + 0.4),
                              fill=False, cross_out=False)
        count += 1
    if not count:
        return
    links = page.get_links()
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
    kept = {formats._link_key(link) for link in page.get_links()}
    for link in links:
        if formats._link_key(link) not in kept:
            page.insert_link({k: v for k, v in link.items() if k not in ("xref", "id")})


def _image_boxes(page, hits):
    """{image xref: [(pixel box, new, share of the page)]} for the hits lying on page images."""
    import pymupdf as fitz

    out = {}
    area = abs(page.rect) or 1.0
    for info in page.get_image_info(xrefs=True):
        xref, bbox = info.get("xref") or 0, fitz.Rect(info["bbox"])
        w, h = info.get("width") or 0, info.get("height") or 0
        if not xref or min(w, h) < previews.MIN_SIDE or bbox.is_empty:
            continue
        try:
            inverse = ~fitz.Matrix(info["transform"])
        except Exception:
            continue
        for box, _old, new in hits:
            rect = fitz.Rect(box)
            if not new.strip():
                continue
            if abs(rect & bbox) >= 0.8 * abs(bbox):     # the hit covers the image: a logo
                out.setdefault(xref, []).append(((0, 0, w, h), new, 0.0))
                continue
            if abs(rect & bbox) < 0.5 * abs(rect):
                continue
            pts = [p * inverse for p in (rect.tl, rect.tr, rect.bl, rect.br)]   # the image fills the unit square
            x0, x1 = min(p.x for p in pts) * w, max(p.x for p in pts) * w
            y0, y1 = min(p.y for p in pts) * h, max(p.y for p in pts) * h
            px = (max(0, int(x0)), max(0, int(y0)), min(w, int(x1 + 0.999)), min(h, int(y1 + 0.999)))
            if px[2] - px[0] >= 2 and px[3] - px[1] >= 2:
                out.setdefault(xref, []).append((px, new, abs(bbox) / area))
    return out


def _paint_image(doc, xref, boxes):
    """Paint the boxes into the pixels of image xref (same size, format and bit depth), so
    the extracted image does not show the original either. Returns the boxes painted."""
    import pymupdf as fitz
    from PIL import Image, ImageDraw

    if doc.xref_get_key(xref, "ImageMask")[1] == "true":
        return 0
    w = int(doc.xref_get_key(xref, "Width")[1] or 0)
    h = int(doc.xref_get_key(xref, "Height")[1] or 0)
    if w * h > previews.MAX_PIXELS:
        return 0
    pix = fitz.Pixmap(doc, xref)
    if pix.alpha:
        pix = fitz.Pixmap(pix, 0)
    if pix.n not in (1, 3):
        pix = fitz.Pixmap(fitz.csRGB, pix)
    if (pix.width, pix.height) != (w, h):
        return 0
    im = Image.frombytes("L" if pix.n == 1 else "RGB", (w, h), pix.samples)
    if pix.n == 1 and int(doc.xref_get_key(xref, "BitsPerComponent")[1] or 8) == 1:
        im = im.convert("1", dither=Image.Dither.NONE)
    work = im.convert(previews._work_mode(im.mode))
    painted = []
    filled = sum((px[2] - px[0]) * (px[3] - px[1]) for px, _, _ in set(boxes)) / float(w * h)
    if any(share == 0.0 for _, _, share in boxes) or (
            filled >= LOGO_FILL and all(share < previews.LOGO_PAGE_SHARE for _, _, share in boxes)
            and previews._logo_like(im)):
        painted.append(previews._blank(work, " ".join(dict.fromkeys(new for _, new, _ in boxes))))
    else:
        draw = ImageDraw.Draw(work)
        for px, new, _share in sorted(set(boxes)):
            low, high = work.crop(px).convert("L").getextrema()
            if high - low < 40:
                continue               # nothing written in the image here: the text is drawn over it
            box = previews._draw_word(draw, work, px, new)
            if box:
                painted.append(box)
    if not painted:
        return 0
    out = im.copy()
    for box in painted:
        out.paste(previews._to_mode(work.crop(box), im), box[:2])
    previews._pdf_put_image(doc, xref, out)
    return len(painted)


# -- entry point -----------------------------------------------------------------------

def _shape(doc):
    """What must survive: pages, links and annotations per page, bookmarks."""
    return [(len(page.get_links()), len(list(page.annots() or ()))) for page in doc], len(doc.get_toc(simple=True))


def _centre_in(box, others):
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return any(o[0] <= cx <= o[2] and o[1] <= cy <= o[3] for o in others)


def _same_names(read, layers, hits, ctx):
    """A word replaced somewhere in the document gets the same pseudonym wherever the pages
    show it again on its own (a name stacked in a logo, a holder repeated on every sheet)."""
    everyday = getattr(ctx.mapper, "everyday", ()) or ()
    local = {}
    for found in hits.values():
        for _box, old, new in found:
            a, b = re.findall(r"[A-Za-z]{4,}", old), re.findall(r"[A-Za-z]{4,}", new)
            if len(a) == len(b):
                local.update((x.lower(), y.lower()) for x, y in zip(a, b)
                             if x != y and x.lower() not in everyday and x.lower() not in KEEP)
    if not local:
        return
    for n, lines in read.items():
        taken = [hit[0] for hit in hits.get(n, ())]
        for line in lines:
            for word, box, _conf in line:
                core = word.strip(".,;:()[]\"'")
                new = local.get(core.lower())
                if new and not _centre_in(box, taken) and not _in_text_layer(box, word, layers[n]):
                    new = new.upper() if core.isupper() else new.capitalize() if core[:1].isupper() else new
                    hits.setdefault(n, []).append((box, word, word.replace(core, new)))


def plan(doc, ctx, identities=None, path="pdfocr", strip=None):
    """{page number: [(box, old, new)]} for an open document. Pages are un-rotated as a side
    effect (same appearance), so the boxes are in plain page coordinates. strip (a dict,
    with DEID_PDF_OCR_TEXT=1) receives {page number: boxes whose text-layer glyphs must go}."""
    read, layers, hits = {}, {}, {}
    if TESSERACT is None:
        return hits
    for page in doc:
        if len(read) >= MAX_PAGES or not needs_ocr(page):
            continue
        if page.rotation:
            page.remove_rotation()
        read[page.number] = ocr_lines(page)
        layers[page.number] = [w[:5] for w in page.get_text("words")]
    if not any(read.values()):
        return hits
    rewrite = _rewriter(ctx, identities, [(doc[n].rect.width, doc[n].rect.height, read[n]) for n in sorted(read)], path)
    for n in sorted(read):
        boxes = [] if strip is not None else None
        found = page_hits(read[n], layers[n], rewrite, ctx, boxes)
        if found:
            hits[n] = found
        if boxes:
            strip[n] = boxes
    _same_names(read, layers, hits, ctx)
    producer = (doc.metadata or {}).get("producer") or ""
    for n in sorted(read):
        cells = template_cells(doc[n], read[n], producer)
        if cells:
            fixed, blocks = template_hits(doc[n], cells, ctx)
            kept = [hit for hit in hits.get(n, ()) if not _centre_in(hit[0], blocks)]
            taken = [hit[0] for hit in kept]
            kept += [hit for hit in fixed if hit[0] in blocks or not (
                _centre_in(hit[0], taken) or any(_centre_in(t, [hit[0]]) for t in taken)
                or _in_text_layer(hit[0], hit[1], layers[n]))]
            if kept:
                hits[n] = kept
    return hits


def redact(path_in_out, ctx, identities=None):
    """Cover the identifiers that only the rendered pages of the PDF at path_in_out show.
    identities: None, the document's formats.PdfFix (any text -> text callable), or extra
    identity strings ({original: pseudonym} or an iterable). Returns the words covered;
    0 leaves the file untouched."""
    tmp = path_in_out + ".ocr"
    try:
        return _redact(path_in_out, tmp, ctx, identities)
    except Exception as exc:            # keep the file as the earlier passes left it
        print("pdfocr: %s: %s" % (os.path.basename(path_in_out), type(exc).__name__), file=sys.stderr)
        return 0
    finally:
        for leftover in (tmp, path_in_out + ".ocrtext"):
            if os.path.lexists(leftover):
                os.remove(leftover)


def _log(path, hits):
    """DEID_PDF_OCR_LOG=<file in the private folder>: one line per covered word (it quotes
    the originals, so it must never be shipped)."""
    name = os.environ.get("DEID_PDF_OCR_LOG")
    if name:
        rows = ["%s\t%d\t%.1f,%.1f,%.1f,%.1f\t%s\t%s\n" % ((path, n) + tuple(box) + (old, new))
                for n in sorted(hits) for box, old, new in hits[n]]
        with open(name, "a", encoding="utf-8", errors="surrogateescape") as f:
            f.write("".join(rows))


def _redact(path, tmp, ctx, identities):
    import pymupdf as fitz

    total = 0
    with fitz.open(path) as doc:
        if doc.needs_pass or not doc.is_pdf:
            return 0
        before = _shape(doc)
        strip = {} if os.environ.get("DEID_PDF_OCR_TEXT") == "1" else None
        hits = plan(doc, ctx, identities, path, strip)
        if not hits:
            return 0
        _log(path, hits)
        images, shows = {}, {}
        for n in sorted(hits):
            page = doc[n]
            if _own_resources(doc, page):
                page = doc.reload_page(page)
            shows[n] = len(page.get_image_info())
            zoom = _zoom(page)
            for xref, boxes in _image_boxes(page, hits[n]).items():
                images.setdefault(xref, []).extend(boxes)
            shown = _render(page, zoom)                 # the colours, before anything is removed
            if strip and strip.get(n):
                _strip_text(page, strip[n])
            _paint_page(page, hits[n], shown, zoom)
            if doc.xref_get_key(page.xref, "Thumb")[0] != "null":
                doc.xref_set_key(page.xref, "Thumb", "null")   # a thumbnail would show the old page
            total += len(hits[n])
        for xref in sorted(images):
            try:
                _paint_image(doc, xref, images[xref])
            except Exception as exc:    # the page is covered; only the extracted image keeps the text
                print("pdfocr: %s: image %d not painted (%s)" % (os.path.basename(path), xref, type(exc).__name__),
                      file=sys.stderr)
        doc.save(tmp, garbage=1, deflate=True, no_new_id=True)   # no random /ID: repeatable
    with fitz.open(tmp) as check:
        if _shape(check) != before or any(len(check[n].get_image_info()) != shown for n, shown in shows.items()):
            print("pdfocr: %s: result differs in pages, links, annotations, bookmarks or images shown; kept as it was"
                  % os.path.basename(path), file=sys.stderr)
            return 0
    os.replace(tmp, path)
    return total
