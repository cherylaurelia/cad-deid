"""Last pass over a rewritten PDF: the places the page-text and metadata passes do not reach.

- XMP packets other than the document's own (/Metadata of images, pages and form XObjects,
  packets left by earlier saves): their dc:creator / pdf:Author values name people too.
- Link targets (/URI): the visible link text is rewritten, the address behind it was not.
- Info-like dictionaries that are not the trailer's /Info (left behind by some writers).

Every value is taken from the ORIGINAL file and replaced in the output only where the output
still holds exactly that value, so nothing is pseudonymized twice.
"""
import os
import re
import sys

from .scan import is_generic

NAME_KEYS = ("/Author", "/Company", "/Manager", "/LastModifiedBy")
# strings that repeat page text or name files: structure /Alt and /ActualText, file specs
TEXT_KEYS = ("/URI", "/Alt", "/ActualText", "/F", "/UF")
_USER_DIR = re.compile(r"(?i)((?:\b[a-z]:)?[\\/]+(?:users|home|media|documents and settings)[\\/]+)([^\\/\s:*?\"<>|]+)")


def _user_dirs(text, pseudo):
    """C:\\Users\\<name>, /home/<name>, /media/<name>: the account folder gets a pseudonym."""
    return _USER_DIR.sub(lambda m: m.group(1) + (m.group(2) if is_generic_name(m.group(2)) else pseudo.span_all(m.group(2))), text)


# fixed slots, plus any element whose local name says it holds a person, account or company
# (Creo ptcpdf:ERSTELL_USERNAME / BEARBEITER / FIRMA, pdfx:_AuthorEmailDisplayName); not *Tool*
_SLOT = re.compile(r"<(dc:creator|dc:contributor|dc:publisher|pdf:Author|xmp:Author|xmpRights:Owner|"
                   r"photoshop:AuthorsPosition|photoshop:CaptionWriter|"
                   r"(?!xmp:)[A-Za-z][\w.-]*:(?![\w.-]*(?i:tool))[\w.-]*(?i:username|user_name|bearbeiter|ersteller|firma|"
                   r"company|owner|drawn_?by|checked_?by|authoremaildisplayname)[\w.-]*)>(.*?)</\1>", re.S)
_ATTR = re.compile(r"((?:pdf:Author|xmp:Author|photoshop:CaptionWriter)=\")([^\"]*)(\")")
_TEXT = re.compile(r">([^<>]+)<")
_ENTITY = re.compile(r"(&[#\w]+;)")


def is_generic_name(value):
    """is_generic for a value in a name slot (author, creator): a software hint only makes it
    generic when the value is short and has no account shape ("Autodesk, Inc." is generic,
    "Koops_Windows\\Tony (KOOPS_WINDOWS)" and a six-word company name are not)."""
    if not is_generic(value):
        return False
    from .identities import GENERIC_VALUES, PLACEHOLDER
    v = value.strip().lower()
    if len(v) < 2 or not any(c.isalpha() for c in v) or v in GENERIC_VALUES or PLACEHOLDER.match(v):
        return True
    return "\\" not in v and "@" not in v and len(v.split()) <= 3


def _span(value, pseudo):
    """Pseudonymize a name inside XML text; entities (&amp;) stay as they are."""
    if is_generic_name(value):
        return value
    return "".join(p if _ENTITY.fullmatch(p) else pseudo.span_all(p) for p in _ENTITY.split(value))


def _packet(text, fix, pseudo):
    def slot(m):
        inner = m.group(2)
        if "<" in inner:
            inner = _TEXT.sub(lambda t: ">" + _span(t.group(1), pseudo) + "<", inner)
        else:
            inner = _span(inner, pseudo)
        return "<%s>%s</%s>" % (m.group(1), inner, m.group(1))

    named = _SLOT.sub(slot, text)
    named = _ATTR.sub(lambda m: m.group(1) + _span(m.group(2), pseudo) + m.group(3), named)
    fixed = fix(text)
    if len(fixed) != len(text) or len(named) != len(text):
        return named
    # the name slots win; elsewhere the document's text rules apply
    return "".join(n if n != o else f for o, n, f in zip(text, named, fixed))


def _is_xmp(obj, pikepdf):
    return isinstance(obj, pikepdf.Stream) and (obj.get("/Type") == "/Metadata" or obj.get("/Subtype") == "/XML")


def _strings(obj, pikepdf, keys, depth=0):
    """Yield (dictionary, key) for every direct string under one of the keys inside obj;
    indirect children are visited on their own."""
    if depth > 12:
        return
    if isinstance(obj, pikepdf.Stream):
        obj = obj.stream_dict
    if isinstance(obj, pikepdf.Dictionary):
        for key in list(obj.keys()):
            value = obj[key]
            if isinstance(value, pikepdf.String):
                if key in keys:
                    yield obj, key
            elif isinstance(value, (pikepdf.Dictionary, pikepdf.Array)) and not value.is_indirect:
                yield from _strings(value, pikepdf, keys, depth + 1)
    elif isinstance(obj, pikepdf.Array):
        for value in obj:
            if isinstance(value, (pikepdf.Dictionary, pikepdf.Array)) and not value.is_indirect:
                yield from _strings(value, pikepdf, keys, depth + 1)


def plan(src, fix, pseudo):
    """{original value: new value} for the URI strings, stray name fields and XMP packets of src."""
    import pikepdf

    uris, names, packets = {}, {}, {}
    with pikepdf.open(src) as pdf:
        root = pdf.Root.get("/Metadata")
        root_id = root.objgen if root is not None and root.is_indirect else None
        info = pdf.trailer.get("/Info")
        info_id = info.objgen if info is not None and info.is_indirect else None
        for obj in pdf.objects:
            if _is_xmp(obj, pikepdf):
                if obj.objgen == root_id:
                    continue  # the document's own packet: pdf_metadata rewrites it
                try:
                    raw = obj.read_bytes()
                except Exception:
                    continue  # a damaged stream: nothing readable in it
                if b"\x00" in raw[:64] or raw in packets:
                    continue
                text = raw.decode("utf-8", "surrogateescape")
                new = _packet(text, fix, pseudo).encode("utf-8", "surrogateescape")
                if new != raw:
                    packets[raw] = new
                continue
            if not isinstance(obj, (pikepdf.Dictionary, pikepdf.Array, pikepdf.Stream)):
                continue
            for holder, key in _strings(obj, pikepdf, TEXT_KEYS + NAME_KEYS):
                old = str(holder[key])
                if key in TEXT_KEYS:
                    new = fix(old) if key == "/URI" else _user_dirs(fix(old), pseudo)
                    if new != old:
                        uris[old] = new
                elif obj.objgen != info_id and not is_generic_name(old):
                    names[old] = pseudo.span_all(old)
    return uris, names, packets


def finish(src, dst, fix, pseudo):
    """Apply plan(src) to the output PDF at dst (replaced through a temporary file, never
    written in place: dst may be a hard link to the input). Returns the values replaced."""
    try:
        import pikepdf

        uris, names, packets = plan(src, fix, pseudo)
        if not (uris or names or packets):
            return 0
        count = 0
        tmp = dst + ".post"
        with pikepdf.open(dst) as pdf:
            info = pdf.trailer.get("/Info")
            info_id = info.objgen if info is not None and info.is_indirect else None
            for obj in pdf.objects:
                if _is_xmp(obj, pikepdf):
                    try:
                        raw = obj.read_bytes()
                    except Exception:
                        continue
                    if raw in packets:
                        obj.write(packets[raw])
                        count += 1
                    continue
                if not isinstance(obj, (pikepdf.Dictionary, pikepdf.Array, pikepdf.Stream)):
                    continue
                for holder, key in _strings(obj, pikepdf, TEXT_KEYS + NAME_KEYS):
                    old = str(holder[key])
                    table = uris if key in TEXT_KEYS else names if obj.objgen != info_id else {}
                    if old in table:
                        holder[key] = pikepdf.String(table[old])
                        count += 1
            if count:
                pdf.save(tmp, deterministic_id=True)
        if count:
            os.replace(tmp, dst)
        return count
    except Exception as exc:  # keep the file as the earlier passes left it
        print("pdfpost: %s: %s" % (os.path.basename(dst), type(exc).__name__), file=sys.stderr)
        if os.path.lexists(dst + ".post"):
            os.remove(dst + ".post")
        return 0
