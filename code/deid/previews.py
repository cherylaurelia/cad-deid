"""Identifying text and logos in raster images: image files, images on PDF pages, images
embedded in SVG drawings, and thumbnails inside FreeCAD / 3MF / Fusion zip containers.

OCR (the tesseract command) finds the words of an image. Every word the content scanner
would replace is painted over with a box of the surrounding background colour carrying its
same-length pseudonym in the text colour. An identifier inside a logo (a small image
placed on a PDF page, or an image file named like a logo) neutralizes the whole logo: it is
filled with its background colour and only the pseudonym is drawn. Width, height, pixel
format and file format are kept; metadata is handled first by formats.py. OLE and
SolidWorks preview streams are deliberately left alone (they stay byte-identical).
"""
import base64
import hashlib
import io
import os
import re
import shutil
import struct
import subprocess
import zipfile
import zlib

from . import formats

TESSERACT = shutil.which("tesseract")
IMAGE_EXT = set(".png .jpg .jpeg .tif .tiff .gif .bmp".split())
ZIP_THUMB_EXT = set(".fcstd .fcstd1 .3mf .f3d .f3z".split())
MIN_SIDE = 24              # smaller images hold no readable text
OCR_TARGET = 1600          # small images are enlarged to about this many pixels wide
OCR_MAX = 7000             # huge scans are reduced to this for OCR
MAX_PIXELS = 400_000_000
LOGO_PAGE_SHARE = 0.05     # an image covering less than this share of its page is a logo
LOGO_COLOURS = 8           # if this many flat colours cover 90% of it (not a photo or scan)
PDF_BUDGET = 150_000_000   # image pixels OCRed per PDF, about 240 s on a box (not seconds: the
                           # output must not depend on machine load); later images are skipped
SVG_LOGO_SIDE = 600        # an image embedded in an SVG drawing no bigger than this is a logo
SVG_MAX = 300 << 20
FONT_FILES = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf")
PNG_SIG = b"\x89PNG\r\n\x1a\n"
PNG_KEEP = {b"tEXt", b"zTXt", b"iTXt", b"pHYs", b"iCCP", b"sRGB", b"gAMA", b"cHRM", b"eXIf", b"tIME"}
ROTATED_MIN_SIDE = 400    # images at least this big are also read turned 90 and 270 degrees
_ALNUM = re.compile(r"[A-Za-z0-9]")
_DATA_URI = re.compile(rb"data:image/(?:png|jpe?g|gif|bmp|tiff?);base64,([A-Za-z0-9+/=\s]{64,})")
_PART = re.compile(r"[A-Za-z0-9|!$]+")
_CONFUSE = str.maketrans("l1|!jI05$2869q", "iiiiiiosszbbgg")
_SKELETONS = {}            # scanner id -> {skeleton: identifier}
_CLEAN = set()             # sha1 of image bytes already processed (zip members seen twice)
_ACTIVE = set()            # sources being handled (guards against process() routing back here)
_FONTS = {}


# -- OCR -------------------------------------------------------------------------

def _ocr(im):
    """Words of a PIL image as (text, (x0, y0, x1, y1)) in the image's own pixels."""
    from PIL import Image

    g = im.convert("L")
    w, h = g.size
    scale = 1.0
    if max(w, h) < OCR_TARGET:
        scale = min(4.0, OCR_TARGET / max(w, h))
    elif max(w, h) > OCR_MAX:
        scale = OCR_MAX / max(w, h)
    if scale != 1.0:
        g = g.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    g.save(buf, "PNG", compress_level=1)
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    done = subprocess.run([TESSERACT, "stdin", "stdout", "--psm", "11", "-l", "eng", "tsv"],
                          input=buf.getvalue(), capture_output=True, env=env, timeout=600)
    if done.returncode != 0:
        raise RuntimeError("tesseract failed: %s" % done.stderr[-200:].decode("latin-1"))
    words = []
    for line in done.stdout.decode("utf-8", "replace").splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 12 or cols[0] != "5" or not cols[11].strip():
            continue
        x, y, bw, bh = (int(c) for c in cols[6:10])
        words.append((cols[11].strip(), (x / scale, y / scale, (x + bw) / scale, (y + bh) / scale)))
    return words


def _hits(words, ctx):
    """[(box, old, new)] for the OCR words the content scanner replaces. Adjacent words
    are also tried glued together (OCR splits 'NameWorks' into 'Name Works')."""
    if not words:
        return []
    texts = [w for w, _ in words]
    new = ctx.text.text(" ".join(texts)).split(" ")
    out, taken = [], set()
    if len(new) == len(texts):
        for i, ((old, box), nw) in enumerate(zip(words, new)):
            if nw != old:
                out.append((box, old, nw))
                taken.add(i)
    for i in range(len(words) - 1):
        (a, ba), (b, bb) = words[i], words[i + 1]
        if i in taken or i + 1 in taken or not (_ALNUM.search(a) and _ALNUM.search(b)):
            continue
        if abs((ba[1] + ba[3]) - (bb[1] + bb[3])) > 0.5 * (ba[3] - ba[1]) or bb[0] < ba[2] - 2:
            continue  # not side by side on one line
        glued = a + b
        nw = ctx.text.text(glued)
        if nw != glued:
            out.append(((ba[0], min(ba[1], bb[1]), bb[2], max(ba[3], bb[3])), glued, nw))
            taken.update((i, i + 1))
    for i, (old, box) in enumerate(words):
        if i not in taken:
            nw = _misread(old, ctx)
            if nw != old:
                out.append((box, old, nw))
    return out


def _skeleton(word):
    """A lowercase word with the letters OCR confuses folded together."""
    return word.lower().replace("rn", "m").replace("vv", "w").translate(_CONFUSE)


def _misread(word, ctx):
    """word with every part that is an OCR misreading of an identifier ('Srnith' for
    'Smith') replaced by that identifier's pseudonym, in the word's letter case."""
    index = _SKELETONS.get(id(ctx.text))
    if index is None:
        index = {}
        for key, value in ctx.text.auto.items():
            if value[1] == "tok" and len(key) >= 5 and key.isascii() and key.isalnum() and not key.isdigit():
                index.setdefault(_skeleton(key), key)
        _SKELETONS[id(ctx.text)] = index
    out, last = [], 0
    for m in _PART.finditer(word):
        part = m.group()
        ident = index.get(_skeleton(part)) if len(part) >= 5 else None
        if ident is None or ident == part.lower():
            continue
        new = ctx.text.pseudo.span(ident)
        if part.isupper():
            new = new.upper()
        elif part[:1].isupper():
            new = new.capitalize()
        out.append(word[last:m.start()] + new)
        last = m.end()
        ctx.text.found.append(("tok", ident))
    return "".join(out) + word[last:] if out else word


# -- painting --------------------------------------------------------------------

def _font(size):
    from PIL import ImageFont

    size = max(4, int(size))
    f = _FONTS.get(size)
    if f is None:
        for path in FONT_FILES:
            if os.path.isfile(path):
                f = ImageFont.truetype(path, size)
                break
        else:
            f = ImageFont.load_default(size=size)
        _FONTS[size] = f
    return f


def _border_colour(im, box):
    """Most common colour on the rim just outside box: the background behind the text."""
    x0, y0, x1, y1 = box
    w, h = im.size
    pts = []
    for x in range(max(0, x0 - 2), min(w, x1 + 2), max(1, (x1 - x0) // 40 or 1)):
        for y in (y0 - 2, y1 + 1):
            if 0 <= y < h:
                pts.append(im.getpixel((x, y)))
    for y in range(max(0, y0 - 2), min(h, y1 + 2), max(1, (y1 - y0) // 20 or 1)):
        for x in (x0 - 2, x1 + 1):
            if 0 <= x < w:
                pts.append(im.getpixel((x, y)))
    if not pts:
        return im.getpixel((min(max(x0, 0), w - 1), min(max(y0, 0), h - 1)))
    return max(set(pts), key=pts.count)


def _dist(a, b):
    if isinstance(a, int):
        return abs(a - b)
    return sum(abs(p - q) for p, q in zip(a[:3], b[:3]))


def _ink_colour(im, box, bg):
    """The colour inside box farthest from the background: the text colour."""
    region = im.crop(box)
    if region.width * region.height > 40000:
        region = region.resize((max(1, region.width // 4), max(1, region.height // 4)))
    colours = region.getcolors(region.width * region.height) or []
    best, far = None, -1
    for count, c in colours:
        d = _dist(c, bg)
        if d > far:
            best, far = c, d
    if best is None or far < 40:  # no contrast: black or white, whichever stands out
        if isinstance(bg, int):
            black, white = 0, 255
        else:
            black, white = (0, 0, 0, 255)[:len(bg)], (255, 255, 255, 255)[:len(bg)]
        best = black if _dist(bg, black) >= _dist(bg, white) else white
    if isinstance(best, tuple) and len(best) == 4:
        best = best[:3] + (255,)
    return best


def _draw_word(draw, im, box, text, bg=None, fg=None):
    """Paint box (grown a little: OCR boxes clip glyph edges) with the background colour
    and write text in it, its ink as tall as the box. Returns the painted pixel box, or
    None when it is too small."""
    w, h = im.size
    bw, bh = box[2] - box[0], box[3] - box[1]
    if bw < 2 or bh < 2:
        return None
    py, px = max(1, round(0.15 * bh)), max(1, round(0.06 * bh))
    x0, y0 = max(0, int(box[0]) - px), max(0, int(box[1]) - py)
    x1, y1 = min(w, int(box[2] + 0.999) + px), min(h, int(box[3] + 0.999) + py)
    bg = _border_colour(im, (x0, y0, x1, y1)) if bg is None else bg
    fg = _ink_colour(im, (x0, y0, x1, y1), bg) if fg is None else fg
    draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=bg)
    l, t, r, b = draw.textbbox((0, 0), text, font=_font(100))
    f = _font(100 * min(bh / max(1, b - t), bw / max(1, r - l)))
    l, t, r, b = draw.textbbox((0, 0), text, font=f)
    draw.text((box[0] - l + (bw - (r - l)) / 2, box[1] - t + (bh - (b - t)) / 2), text, fill=fg, font=f)
    return x0, y0, x1, y1


def _work_mode(mode):
    if mode in ("RGBA", "LA", "PA", "P"):
        return "RGBA"
    if mode in ("1", "L"):
        return "L"
    return "RGB"


def _to_mode(region, orig):
    """A painted region of the working copy in the original image's mode."""
    from PIL import Image

    mode = orig.mode
    if region.mode == mode:
        return region
    if mode == "1":
        return region.point(lambda v: 255 if v >= 128 else 0).convert("1", dither=Image.Dither.NONE)
    if mode == "P":
        return region.convert("RGB").quantize(palette=orig, dither=Image.Dither.NONE)
    return region.convert(mode)


def _unturn(box, turn, size):
    """A box found in the image turned by turn, in the unturned image's pixels."""
    from PIL import Image

    x0, y0, x1, y1 = box
    w, h = size
    if turn == Image.Transpose.ROTATE_90:     # turned counter-clockwise
        return w - y1, x0, w - y0, x1
    return y0, h - x1, y1, h - x0             # ROTATE_270: turned clockwise


def _blank(im, text):
    """Neutralize a whole logo: fill it with its background colour and write text."""
    from PIL import ImageDraw

    bg = _border_colour(im, (2, 2, im.width - 2, im.height - 2))
    fg = _ink_colour(im, (0, 0, im.width, im.height), bg)
    draw = ImageDraw.Draw(im)
    draw.rectangle((0, 0, im.width - 1, im.height - 1), fill=bg)
    _draw_word(draw, im, (2, im.height * 0.3, im.width - 2, im.height * 0.7), text, bg, fg)
    return 0, 0, im.width, im.height


def clean_image(im, ctx, whole=False, label=""):
    """OCR a PIL image (also turned sideways when it is big enough to hold drawing text)
    and paint over its identifiers. whole: the image is a logo, so an identifier blanks
    all of it; label: a logo known to identify someone (by its file name) is blanked with
    this pseudonym even when OCR reads nothing. Pixels outside the painted boxes are kept
    exactly. Returns (new image or None, replacements)."""
    if min(im.size) < MIN_SIDE or im.mode not in ("1", "L", "LA", "P", "RGB", "RGBA", "CMYK"):
        return None, 0
    if TESSERACT is None and not label:
        return None, 0
    from PIL import Image, ImageDraw

    turns = [None]
    if not whole and min(im.size) >= ROTATED_MIN_SIDE:
        turns += [Image.Transpose.ROTATE_90, Image.Transpose.ROTATE_270]
    back = {Image.Transpose.ROTATE_90: Image.Transpose.ROTATE_270,
            Image.Transpose.ROTATE_270: Image.Transpose.ROTATE_90}
    work, boxes, count = None, [], 0
    for turn in turns if TESSERACT else ():
        base = im if work is None else work
        view = base if turn is None else base.transpose(turn)
        hits = _hits(_ocr(view), ctx)
        if not hits:
            continue
        if work is None:
            work = im.convert(_work_mode(im.mode))
            view = work if turn is None else work.transpose(turn)
        draw = ImageDraw.Draw(view)
        if whole:
            painted = [_blank(view, " ".join(dict.fromkeys(new for _, _, new in hits)))]
            count += len(hits)
        else:
            painted = [b for b in (_draw_word(draw, view, box, new) for box, _, new in hits) if b]
            count += len(painted)
        if turn is None:
            work = view
        else:
            work = view.transpose(back[turn])
            painted = [_unturn(b, turn, work.size) for b in painted]
        boxes += painted
    if not boxes and whole and label:
        work = im.convert(_work_mode(im.mode))
        boxes, count = [_blank(work, label)], 1
    if not boxes:
        return None, 0
    out = im.copy()
    for box in boxes:
        out.paste(_to_mode(work.crop(box), im), box[:2])
    return out, count


# -- re-encoding in the original format -------------------------------------------

def _png_chunks(data):
    out, pos = [], 8
    while pos + 12 <= len(data):
        n = struct.unpack_from(">I", data, pos)[0]
        kind = bytes(data[pos + 4:pos + 8])
        out.append((kind, bytes(data[pos:pos + 12 + n])))
        pos += 12 + n
        if kind == b"IEND":
            break
    return out


def _png_like(orig_bytes, im, new):
    """PNG bytes of new with the original's chunks: when the header and palette come out
    the same only the image data chunks change; otherwise the metadata chunks are copied."""
    kw = {"compress_level": 9}
    ihdr = orig_bytes[16:29]
    if new.mode == "P":
        kw["bits"] = ihdr[8] if ihdr[9] == 3 and ihdr[8] in (1, 2, 4, 8) else 8
    if "transparency" in new.info:
        kw["transparency"] = new.info["transparency"]
    if im.info.get("interlace"):
        kw["interlace"] = 1
    buf = io.BytesIO()
    new.save(buf, "PNG", **kw)
    old_chunks, new_chunks = _png_chunks(orig_bytes), _png_chunks(buf.getvalue())
    pick = lambda chunks, kinds: [c for k, c in chunks if k in kinds]
    idat = pick(new_chunks, {b"IDAT"})
    if pick(old_chunks, {b"IHDR", b"PLTE", b"tRNS"}) == pick(new_chunks, {b"IHDR", b"PLTE", b"tRNS"}):
        out, placed = [PNG_SIG], False
        for kind, chunk in old_chunks:
            if kind == b"IDAT":
                if not placed:
                    out.extend(idat)
                    placed = True
                continue
            out.append(chunk)
        return b"".join(out)
    out = [PNG_SIG] + pick(new_chunks, {b"IHDR"}) + pick(old_chunks, PNG_KEEP)
    out += [c for k, c in new_chunks if k not in (b"IHDR",) and k not in PNG_KEEP]
    return b"".join(out)


def _jpeg_head(data):
    """(header segments as (marker, bytes), offset of the scan data) of a JPEG."""
    segs, pos = [], 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        length = struct.unpack_from(">H", data, pos + 2)[0]
        if marker == 0xDA:
            return segs, pos
        segs.append((marker, bytes(data[pos:pos + 2 + length])))
        pos += 2 + length
    return segs, pos


def _jpeg_like(orig_bytes, im, new):
    """JPEG bytes of new with the original's quantization tables and sampling, and the
    original's metadata segments (EXIF, XMP, ICC, IPTC, comments) spliced back in."""
    from PIL import JpegImagePlugin

    kw = {"quality": 92}
    try:
        if getattr(im, "quantization", None):
            kw = {"qtables": im.quantization}
            sampling = JpegImagePlugin.get_sampling(im)
            if sampling != -1:
                kw["subsampling"] = sampling
    except Exception:
        kw = {"quality": 92}
    if im.info.get("progressive") or im.info.get("progression"):
        kw["progressive"] = True
    buf = io.BytesIO()
    new.save(buf, "JPEG", **kw)
    data = buf.getvalue()
    old_segs, _ = _jpeg_head(orig_bytes)
    new_segs, scan = _jpeg_head(data)
    meta = [s for m, s in old_segs if 0xE1 <= m <= 0xED or m in (0xEF, 0xFE)]
    first = [s for m, s in new_segs if m in (0xE0, 0xEE)]
    rest = [s for m, s in new_segs if not (0xE0 <= m <= 0xEF or m == 0xFE)]
    return b"\xff\xd8" + b"".join(first + meta + rest) + data[scan:]


def _save_like(orig_bytes, im, new):
    fmt = im.format
    if fmt == "PNG":
        return _png_like(orig_bytes, im, new)
    if fmt == "JPEG":
        return _jpeg_like(orig_bytes, im, new)
    buf = io.BytesIO()
    if fmt == "TIFF":
        kw = {"compression": im.info.get("compression", "raw")}
        if im.info.get("dpi"):
            kw["dpi"] = im.info["dpi"]
        try:
            new.save(buf, "TIFF", tiffinfo=im.tag_v2, **kw)
        except Exception:
            buf = io.BytesIO()
            new.save(buf, "TIFF", **kw)
    elif fmt == "GIF":
        new.save(buf, "GIF", **({"transparency": new.info["transparency"]} if "transparency" in new.info else {}))
    else:
        new.save(buf, fmt)
    return buf.getvalue()


def image_bytes(data, ctx, whole=False, logo_side=0, label=""):
    """De-identified bytes of an image file's content. Returns (bytes, replacements);
    the input bytes come back unchanged when nothing is found or the image can't be read.
    whole: treat it as a logo; logo_side: so is any image no bigger than this."""
    from PIL import Image

    key = hashlib.sha1(data).digest()
    if key in _CLEAN:
        return data, 0
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    im = Image.open(io.BytesIO(data))
    if im.format not in ("PNG", "JPEG", "TIFF", "GIF", "BMP") or getattr(im, "n_frames", 1) > 1:
        return data, 0
    im.load()
    new, n = clean_image(im, ctx, whole or (max(im.size) <= logo_side and _logo_like(im)), label)
    if not n:
        _remember(key)
        return data, 0
    out = _save_like(data, im, new)
    check = Image.open(io.BytesIO(out))
    check.load()
    if check.size != im.size or check.format != im.format:
        return data, 0
    _remember(hashlib.sha1(out).digest())
    return out, n


def _remember(key):
    if len(_CLEAN) > 200_000:
        _CLEAN.clear()
    _CLEAN.add(key)


def _logo_like(im):
    """Flat artwork rather than a photo or a scan: a few colours cover most of it."""
    small = im.convert("RGB").resize((64, 64)).point(lambda v: v // 32 * 32)
    counts = sorted((c for c, _ in small.getcolors(4096) or []), reverse=True)
    covered, needed = 0, 0
    for c in counts:
        covered, needed = covered + c, needed + 1
        if covered >= 0.9 * 64 * 64:
            break
    return needed <= LOGO_COLOURS


def _logo_label(path, ctx):
    """For an image file named like a logo: (True, pseudonyms of the identifiers in its
    name, or ''); otherwise (False, '')."""
    stem = os.path.splitext(os.path.basename(path))[0]
    if "logo" not in stem.lower():
        return False, ""
    new = ctx.text.text(stem)
    words = [b for a, b in zip(re.split(r"[^A-Za-z0-9]+", stem), re.split(r"[^A-Za-z0-9]+", new)) if a != b]
    return True, " ".join(words)


def image_file(src, dst, ctx):
    tmp = dst + ".pvtmp"
    n = formats.image_file(src, tmp, ctx)
    try:
        with open(tmp, "rb") as f:
            data = f.read()
        logo, label = _logo_label(src, ctx)
        new, k = image_bytes(data, ctx, whole=logo, label=label)
    except Exception:
        new, k = None, 0
    if k:
        with open(dst, "wb") as f:
            f.write(new)
        os.remove(tmp)
    else:
        os.replace(tmp, dst)
    return n + k


# -- PDF page images ---------------------------------------------------------------

def _pdf_image_rects(doc):
    """{image xref: smallest share of its page that one placement covers}."""
    share = {}
    for page in doc:
        area = abs(page.rect) or 1.0
        for item in page.get_images(full=True):
            xref = item[0]
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            s = min((abs(r) / area for r in rects), default=1.0)
            share[xref] = min(share.get(xref, 1.0), s)
    return share


def _pdf_put_image(doc, xref, new):
    """Write PIL image new as the stream of image xref (same width and height): JPEG when
    the original was JPEG, otherwise deflated samples; 1-bit images stay 1-bit."""
    filt = doc.xref_get_key(xref, "Filter")[1]
    mode = new.mode
    if mode == "1":
        raw, cs, bpc = new.tobytes(), "/DeviceGray", 1
    elif mode == "L":
        raw, cs, bpc = new.tobytes(), "/DeviceGray", 8
    else:
        new = new.convert("RGB")
        raw, cs, bpc = new.tobytes(), "/DeviceRGB", 8
    if "DCTDecode" in filt and bpc == 8:
        buf = io.BytesIO()
        new.save(buf, "JPEG", quality=90)
        doc.update_stream(xref, buf.getvalue(), compress=False)
        doc.xref_set_key(xref, "Filter", "/DCTDecode")
    else:
        doc.update_stream(xref, zlib.compress(raw, 9), compress=False)
        doc.xref_set_key(xref, "Filter", "/FlateDecode")
    doc.xref_set_key(xref, "DecodeParms", "null")
    doc.xref_set_key(xref, "Decode", "null")
    doc.xref_set_key(xref, "ColorSpace", cs)
    doc.xref_set_key(xref, "BitsPerComponent", str(bpc))
    if doc.xref_get_key(xref, "Mask")[0] == "array":  # colour-key mask of the old colour space
        doc.xref_set_key(xref, "Mask", "null")


def pdf_images(path, dst, ctx):
    """OCR every image of the PDF at path; write the result to dst. Returns replacements."""
    import pymupdf as fitz
    from PIL import Image

    total = 0
    doc = fitz.open(path)
    try:
        if doc.needs_pass:
            return 0
        shares = _pdf_image_rects(doc)
        budget = PDF_BUDGET
        for xref, share in shares.items():
            if budget <= 0:
                break
            try:
                if doc.xref_get_key(xref, "ImageMask")[1] == "true":
                    continue
                w = int(doc.xref_get_key(xref, "Width")[1] or 0)
                h = int(doc.xref_get_key(xref, "Height")[1] or 0)
                if min(w, h) < MIN_SIDE or w * h > MAX_PIXELS:
                    continue
                budget -= w * h
                pix = fitz.Pixmap(doc, xref)
                if pix.alpha:
                    pix = fitz.Pixmap(pix, 0)
                if pix.n not in (1, 3):
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                im = Image.frombytes("L" if pix.n == 1 else "RGB", (pix.width, pix.height), pix.samples)
                if (pix.width, pix.height) != (w, h):
                    continue
                bpc = int(doc.xref_get_key(xref, "BitsPerComponent")[1] or 8)
                if bpc == 1 and pix.n == 1:
                    im = im.convert("1", dither=Image.Dither.NONE)
                new, n = clean_image(im, ctx, whole=share < LOGO_PAGE_SHARE and _logo_like(im))
            except Exception:
                continue
            if n:
                _pdf_put_image(doc, xref, new)
                total += n
        for page in doc:  # optional page thumbnails would show the old image
            if doc.xref_get_key(page.xref, "Thumb")[0] != "null":
                doc.xref_set_key(page.xref, "Thumb", "null")
                total += 1
        if total:
            doc.save(dst + ".part", garbage=1, deflate=True, no_new_id=True)  # no random /ID: repeatable
    finally:
        doc.close()
    if total:
        os.replace(dst + ".part", dst)
    return total


RENDER_DPI = 200            # pages without a text layer are rendered at this resolution for OCR
RENDER_MAX_PIXELS = 24_000_000
RENDER_PAGES = 40           # pages read per PDF (the first ones; not time-based: repeatable)
LOW_TEXT_WORDS = 8          # a page with fewer text-layer words shows its text as outlines or images


def pdf_render(path, dst, ctx):
    """OCR the rendered pages of the PDF at path that have (almost) no text layer: their
    words are vector outlines or pixels (title blocks drawn as curves, scanned pages). Each
    identifier is removed with a redaction (the outlines it covers and the image pixels
    under it) and its same-length pseudonym is written in its place. Writes dst when
    something changed. Returns replacements."""
    import pymupdf as fitz
    from PIL import Image

    total = 0
    doc = fitz.open(path)
    try:
        if doc.needs_pass:
            return 0
        for page in list(doc)[:RENDER_PAGES]:
            if len(page.get_text("words")) >= LOW_TEXT_WORDS:
                continue
            r = page.rect
            zoom = RENDER_DPI / 72.0
            if r.width * r.height * zoom * zoom > RENDER_MAX_PIXELS:
                zoom = (RENDER_MAX_PIXELS / max(1.0, r.width * r.height)) ** 0.5
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
            im = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            words = _ocr(im)
            hits = _hits(words, ctx)
            seen = {tuple(round(v) for v in box) for box, _, _ in hits}
            for old, box in words:  # title blocks are in capitals: "ACME" alone, read as "Acme"
                if len(old) >= 4 and old.isupper() and tuple(round(v) for v in box) not in seen:
                    nw = ctx.text.text(old.title())
                    if nw != old.title():
                        hits.append((box, old, nw.upper()))
                        seen.add(tuple(round(v) for v in box))
            # a word of a name replaced elsewhere on the page ("Acme Robotics, Inc." in the
            # copyright line, ACME and ROBOTICS stacked in the logo) gets the same pseudonym
            local = {}
            for _, old, nw in hits:
                a, b = _PART.findall(old), _PART.findall(nw)
                if len(a) == len(b):
                    local.update((x.lower(), y.lower()) for x, y in zip(a, b) if x != y and len(x) >= 3)
            for old, box in words:
                k = tuple(round(v) for v in box)
                n = local.get(old.lower())
                if n and k not in seen:
                    hits.append((box, old, n.upper() if old.isupper() else n.capitalize() if old[:1].isupper() else n))
                    seen.add(k)
            if not hits:
                continue
            jobs = []
            for box, _old, new in hits:
                pad = 0.15 * (box[3] - box[1])
                shown = fitz.Rect(box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad) / zoom
                rect = shown * page.derotation_matrix
                rect.normalize()
                page.add_redact_annot(rect, fill=(1, 1, 1))
                jobs.append((rect, shown, new))
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED)
            for rect, shown, new in jobs:
                size = 0.75 * shown.height
                width = fitz.get_text_length(new, fontname="helv", fontsize=size)
                if width > shown.width > 0:
                    size *= shown.width / width
                if size < 2:
                    continue
                # baseline at the bottom of the word as shown, mapped back like the box
                start = fitz.Point(shown.x0 + max(0.0, (shown.width - fitz.get_text_length(
                    new, fontname="helv", fontsize=size)) / 2), shown.y1 - 0.25 * shown.height)
                page.insert_text(start * page.derotation_matrix, new, fontsize=size, fontname="helv",
                                 rotate=page.rotation)
            total += len(jobs)
        if total:
            doc.save(dst + ".part", garbage=1, deflate=True, no_new_id=True)
    finally:
        doc.close()
    if total:
        os.replace(dst + ".part", dst)
    return total


def pdf_file(src, dst, ctx):
    tmp = dst + ".pvtmp"
    n = formats.pdf_file(src, tmp, ctx, 0)
    try:
        k = pdf_images(tmp, dst, ctx) if TESSERACT else 0
    except Exception:
        k = 0
        _remove(dst + ".part")
    if k:
        os.remove(tmp)
    else:
        os.replace(tmp, dst)
    return n + k


# -- images embedded in SVG drawings ----------------------------------------------

def svg_file(src, dst, ctx):
    """SVG: the text is scanned by formats.scan_stream; then every embedded base64 image
    (title-block logos of drawing templates) is cleaned and re-encoded in place. The text
    scan can hit a name inside base64 wrapped with spaces and break the image, so each
    payload is taken from the source (the scan keeps every offset)."""
    tmp = dst + ".pvtmp"
    n = formats.scan_stream(src, tmp, ctx.text)
    k, parts, last = 0, [], 0
    try:
        if os.path.getsize(src) < SVG_MAX:
            with open(src, "rb") as f:
                orig = f.read()
            with open(tmp, "rb") as f:
                data = f.read()
            for m in _DATA_URI.finditer(orig) if b"data:image/" in orig and len(orig) == len(data) else ():
                a, b = m.span(1)
                payload = new_payload = orig[a:b]
                try:
                    raw = base64.b64decode(re.sub(rb"\s+", b"", payload), validate=True)
                    new, c = image_bytes(raw, ctx, logo_side=SVG_LOGO_SIDE)
                    if c:
                        new_payload, k = base64.b64encode(new), k + c
                except Exception:
                    pass
                if new_payload != data[a:b]:
                    parts += [data[last:a], new_payload]
                    last = b
            if parts:
                with open(dst + ".part", "wb") as f:
                    f.writelines(parts + [data[last:]])
    except Exception:
        parts, k = [], 0
        _remove(dst + ".part")
    if parts:
        os.replace(dst + ".part", dst)
        os.remove(tmp)
    else:
        os.replace(tmp, dst)
    return n + k


# -- thumbnails inside zip containers ----------------------------------------------

def zip_thumbnails(src, dst, ctx):
    """FCStd / 3MF / F3D: members go through formats.zip_file; then the image members
    (thumbnails, plate previews, drawing page images) are cleaned and the zip rewritten
    with the same member order, compression and dates."""
    tmp = dst + ".pvtmp"
    n = formats.zip_file(src, tmp, ctx, False, 0)
    k = 0
    try:
        changed = {}
        with zipfile.ZipFile(tmp) as z:
            for info in z.infolist():
                if os.path.splitext(info.filename)[1].lower() in IMAGE_EXT and 0 < info.file_size < 64 << 20:
                    data = z.read(info)
                    new, c = image_bytes(data, ctx)
                    if c:
                        changed[info.filename] = new
                        k += c
            if changed:
                with zipfile.ZipFile(dst + ".part", "w", allowZip64=True) as out:
                    out.comment = z.comment
                    for info in z.infolist():
                        o = zipfile.ZipInfo(info.filename, info.date_time)
                        o.compress_type = info.compress_type if info.compress_type in (0, 8) else 8
                        o.external_attr, o.create_system, o.comment = info.external_attr, info.create_system, info.comment
                        out.writestr(o, changed.get(info.filename) if info.filename in changed else z.read(info))
    except Exception:
        k = 0
        _remove(dst + ".part")
    if k:
        os.replace(dst + ".part", dst)
        os.remove(tmp)
    else:
        os.replace(tmp, dst)
    return n + k


# -- entry point ------------------------------------------------------------------

def handle(src, dst, ctx):
    """Write the de-identified copy of src at dst (same extension). Returns replacements.
    Never raises: on failure the file is byte-scanned in place or copied."""
    ext = os.path.splitext(dst)[1].lower()
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    _remove(dst, dst + ".pvtmp", dst + ".part")
    reentered = src in _ACTIVE
    _ACTIVE.add(src)
    try:
        if ext in IMAGE_EXT:
            return image_file(src, dst, ctx)
        if ext == ".pdf":
            return pdf_file(src, dst, ctx)
        if ext == ".svg":
            return svg_file(src, dst, ctx)
        if ext in ZIP_THUMB_EXT and formats.magic(src) == "zip":
            return zip_thumbnails(src, dst, ctx)
        if not reentered:
            return formats.process(src, dst, ext, ctx)[1]
    except Exception:
        _remove(dst, dst + ".pvtmp", dst + ".part")
    finally:
        if not reentered:
            _ACTIVE.discard(src)
    try:
        if ext in (".pdf",) | IMAGE_EXT | ZIP_THUMB_EXT:  # compressed or checksummed: copy
            formats.place(src, dst)
            return 0
        return formats.scan_stream(src, dst, ctx.text if ext in formats.TEXT_EXT else ctx.binary)
    except Exception:
        _remove(dst, dst + ".part")
        formats.place(src, dst)
        return 0


def _remove(*paths):
    for p in paths:
        if os.path.lexists(p):
            os.remove(p)
