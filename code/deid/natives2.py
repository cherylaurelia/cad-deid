"""Native CAD formats that need their own container handling.

handle(src, dst, ctx) -> number of replacements. Covers
  Creo / Pro-ENGINEER  (.prt.N .asm.N .drw.N and unversioned files with the #UGC header)
  JT                   (.jt: properties and metadata of the scene graph segments)
  Fusion 360           (.f3d / .f3z archives: design streams, nested designs)
  CATIA V5             (.CATPart .CATProduct .CATDrawing)
  7z and RAR archives  (repacked; every member goes through the normal pipeline)

Every change keeps the length of the text it replaces; where the format stores
compressed blocks, sizes, offsets or checksums over that text, they are rebuilt. A file
that cannot be parsed is scanned in place where that is safe, otherwise copied as is.
"""
import calendar
import collections
import itertools
import lzma
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import zipfile
import zlib

import ahocorasick

from .scan import is_generic

# Property / parameter names whose value names a person or an organisation. Kept tight:
# the whole value is replaced, so a free-text field must never match.
_SEP = r"[ _\-]?"
PERSON_KEY = re.compile(
    r"author|drawn|drawer|designed|designer|design" + _SEP + r"by|engineer(?!ing)|checked|checker|"
    r"check" + _SEP + r"by|approv|modell?ed|model" + _SEP + r"by|created" + _SEP + r"by|creator|"
    r"modified" + _SEP + r"by|saved" + _SEP + r"by|last" + _SEP + r"saved|owner|drafter|draftsman|"
    r"reviewed|reviewer|originator|responsible|responsable|user" + _SEP + r"name|manager|contact|"
    r"dessin|zeichner|gezeichnet|geprueft|bearbeiter|ersteller|erstell" + _SEP + r"user|konstrukteur|"
    r"konstruiert|disegnat|progettista|controllat|verificat|redatt|approvat")
ORG_KEY = re.compile(r"company|customer|client|vendor|supplier|firma|societe|societa|organi[sz]ation|"
                     r"department|azienda|unternehmen|kunde")
NOT_NAME = re.compile(r"date|datum|(^|[ _])data([ _]|$)|time|number|(^|[ _])(no|nr|num|rev|revision)([ _]|$)|"
                      r"scale|status|state|type|code|version|designation|material|weight|mass")
_PRINTABLE = re.compile(rb"[\x20-\x7e]")
STATS = collections.Counter()   # what happened, for the private run log
_PROTECTED = set()              # lower-case names and plain values stored in the Creo file in work: never replaced
_MODELS = {}                    # of those, the model and file names -> file type ("" if unknown)


def name_kind(key):
    """'person' / 'org' when a property or parameter name says its value names someone."""
    k = key.strip().lower()
    if NOT_NAME.search(k):
        return ""
    if ORG_KEY.search(k):
        return "org"
    return "person" if PERSON_KEY.search(k) else ""


def _place(src, dst):
    """Output is byte-identical to the input: hard-link it (same disk) or copy it."""
    if os.path.lexists(dst):
        os.remove(dst)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def _write(dst, data):
    part = dst + ".part"
    with open(part, "wb") as f:
        f.write(data)
    os.replace(part, dst)


def _span(buf, a, b, ctx, kind, encoding="latin-1"):
    """Pseudonymize the identity value at buf[a:b] (same length). Records it for the
    private log and for the file's own-name propagation. Returns 1 if it changed."""
    raw = bytes(buf[a:b])
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        return 0
    value = text.strip("\x00 \t\r\n")
    if is_generic(value) or len(value) > 200 or len(set(value.lower())) < 2 or value.lower() in _PROTECTED:
        return 0
    new = ctx.binary.pseudo.span(text)
    try:
        packed = new.encode(encoding)
    except UnicodeEncodeError:
        return 0
    if len(packed) != len(raw) or packed == raw:
        return 0
    buf[a:b] = packed
    ctx.binary.found.append((kind, value))
    return 1


_USERS_W = re.compile(rb"(?:/\x00|\\\x00)[Uu]\x00[Ss]\x00[Ee]\x00[Rr]\x00[Ss]\x00/\x00((?:[^\\/:*?\"<>|\x00-\x1f]\x00){2,40})(?=/\x00|\\\x00)")
_GENERIC_USERS = ("public", "default", "all users", "default user", "shared", "administrator", "admin",
                  "user", "users", "owner", "guest")
_ADSK = re.compile(rb"Autodesk Fusion(?: 360)?[\\/]+([A-Z0-9]{8,16})(?=[\\/])")
_ADSK_W = re.compile(rb"F\x00u\x00s\x00i\x00o\x00n\x00(?: \x003\x006\x000\x00)?(?:[\\/]\x00)+((?:[A-Z0-9]\x00){8,16})(?=[\\/]\x00)")
_ONEDRIVE = re.compile(rb"OneDrive - ([^\\/\x00-\x1f\"<>|:*?]{2,60})")
_ONEDRIVE_W = re.compile(rb"O\x00n\x00e\x00D\x00r\x00i\x00v\x00e\x00 \x00-\x00 \x00((?:[^\\/\x00-\x1f\"<>|:*?]\x00){2,60})")


def _folder_rules(buf, ctx):
    """Identity parts of stored paths that the scanner's user-folder rule does not see:
    the organisation of a synchronised 'OneDrive - <organisation>' folder, the account
    folder of the Fusion 360 cache, and user folders written with forward slashes in
    UTF-16."""
    done = 0
    for m in list(_ONEDRIVE.finditer(buf)):
        done += _span(buf, m.start(1), m.end(1), ctx, "org")
    for m in list(_ONEDRIVE_W.finditer(buf)):
        done += _span(buf, m.start(1), m.end(1), ctx, "org", "utf-16le")
    for m in list(_ADSK.finditer(buf)):       # the account's folder of the Fusion cache
        done += _span(buf, m.start(1), m.end(1), ctx, "user")
    for m in list(_ADSK_W.finditer(buf)):
        done += _span(buf, m.start(1), m.end(1), ctx, "user", "utf-16le")
    for m in list(_USERS_W.finditer(buf)):    # c:/users/<name>/ in UTF-16 (forward slash)
        if m.group(1).decode("utf-16le", "replace").lower() not in _GENERIC_USERS:
            done += _span(buf, m.start(1), m.end(1), ctx, "user", "utf-16le")
    return done


_ANCHOR = re.compile(rb"\\(users|documents and settings)\\[\\/ ]*$")
_FOLDER8 = re.compile(rb"\\(users|documents and settings)\\[\\/]*(?![\\/])"
                      rb"(?![^\\/:*?\"<>|\x00-\x1f\x7f-\x9f';,)]{2,40}[\\/])", re.I)
_FOLDER16 = re.compile(rb"\\\x00((?:u\x00s\x00e\x00r\x00s\x00)|(?:d\x00o\x00c\x00u\x00m\x00e\x00n\x00t\x00s\x00 \x00"
                       rb"a\x00n\x00d\x00 \x00s\x00e\x00t\x00t\x00i\x00n\x00g\x00s\x00))\\\x00(?:[\\/]\x00)*(?![\\/]\x00)"
                       rb"(?!(?:[^\\/:*?\"<>|';,)\x00-\x1f]\x00){2,40}[\\/]\x00)", re.I)


def _scan(buf, ctx):
    """ctx.binary.scan with a guard for packed or record data. The scanner takes the
    text after a Users folder as its name; in a binary record that text can be a string
    cut short and followed by the next field (a length, a count), and replacing it
    would change bytes that are not text. So a Users folder is only shown to the
    scanner when its name ends at a path separator; any other one is hidden during the
    scan and left as it is. A hit that still contains control bytes is undone."""
    found = ctx.binary.found
    hidden = []
    for rx in (_FOLDER8, _FOLDER16):
        for m in rx.finditer(buf):
            hidden.append((m.start(1), bytes(buf[m.start(1):m.end(1)])))
    for at, raw in hidden:
        buf[at:at + len(raw)] = b"\x01" * len(raw)
    if hidden:
        STATS["user folder without a clean end left as is"] += len(hidden)
    try:
        for _attempt in range(6):
            before, mark = bytes(buf), len(found)
            count = ctx.binary.scan(buf, first=False)
            bad = {v for k, v in found[mark:] if k == "user" and
                   any(ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0 for c in v)}
            if not bad:
                return count
            buf[:] = before
            del found[mark:]
            low, masked = before.lower(), 0
            for value in bad:
                raw = value.encode("latin-1", "ignore")
                at = low.find(raw)
                while raw and at >= 0:
                    m = _ANCHOR.search(low, max(0, at - 48), at)
                    if m:
                        hidden.append((m.start(1), bytes(buf[m.start(1):m.end(1)])))
                        buf[m.start(1):m.end(1)] = b"\x01" * (m.end(1) - m.start(1))
                        masked += 1
                    at = low.find(raw, at + 1)
            if not masked:
                break
        STATS["scan skipped: folder name runs into binary data"] += 1
        return 0
    finally:
        for at, raw in reversed(hidden):
            buf[at:at + len(raw)] = raw


_WIDE_RUN = re.compile(rb"(?:[\x20-\x7e]\x00){6,}")


def _rule_word(rx):
    """A word that every match of an in-tree rename rule contains (lower case), to skip
    the rule quickly on buffers without it; b"" when the rule's shape is not known (it is
    then always run). paths.py writes a rule as the project's words joined by a class of
    separators, between two look-arounds."""
    m = re.fullmatch(r"\(\?<!\[A-Za-z\]\)(.+)\(\?!\[A-Za-z\]\)", rx.pattern)
    if not m:
        return b""
    words = []
    for piece in re.split(r"\[[^\]]*\]\*", m.group(1)):
        plain = re.sub(r"\\(.)", r"\1", piece)
        if not plain or re.escape(plain) != piece and plain != piece:
            return b""
        words.append(plain)
    return max(words, key=len).lower().encode("latin-1", "ignore")


def _local_rules(buf, ctx):
    """The project's in-tree rename rules (a multi-word project name that reappears in
    file and folder names) applied to stored names too, so links follow the renamed
    files. 8-bit and UTF-16LE, same length."""
    rules = getattr(ctx, "rules", None)
    if not rules:
        return 0
    span = ctx.binary.pseudo.span
    low = bytes(buf).lower()
    done = 0
    for rx in rules:
        token = _rule_word(rx)
        if not token or token in low:
            text = bytes(buf).decode("latin-1")
            for hit in list(rx.finditer(text)):
                new = span(hit.group())
                if new != hit.group() and len(new) == len(hit.group()):
                    buf[hit.start():hit.end()] = new.encode("latin-1")
                    done += 1
        if not token or token.decode("latin-1").encode("utf-16le") in low:
            for run in list(_WIDE_RUN.finditer(buf)):
                text = run.group().decode("utf-16le")
                new = rx.sub(lambda hit: span(hit.group()), text)
                if new != text and len(new) == len(text):
                    buf[run.start():run.end()] = new.encode("utf-16le")
                    done += 1
    return done


def _own_names(ctx, mark, surnames=False):
    """Matcher for the names this file revealed about itself since mark (users, hosts,
    authors): 8-bit and UTF-16LE, any case, in free text only. With surnames, a login of
    the form initial + surname (or first.last) also gives its parts. None if no names."""
    names, derived = set(), set()
    for kind, value in ctx.binary.found[mark:]:
        if kind in ("person", "org", "user"):
            for piece in re.split(r"[\\\n]", value):
                piece = piece.strip()
                if len(piece) >= 3 and not is_generic(piece):
                    names.add(piece)
                    if surnames and kind == "user":
                        if re.fullmatch(r"[A-Za-z]{7,}", piece):
                            derived.add(piece[1:])
                        derived.update(w for w in re.split(r"[ ._\-]+", piece)
                                       if len(w) >= 6 and w.isalpha() and w.isascii() and w != piece)
    names = {n for n in names if n.lower() not in _PROTECTED}
    derived = {d for d in derived if not is_generic(d) and d.lower() not in _PROTECTED} - names
    auto, any_word = ahocorasick.Automaton(), False
    for name in names | derived:
        low = name.lower()
        try:
            auto.add_word(low.encode("latin-1").decode("latin-1"), (len(low), False))
            any_word = True
        except UnicodeEncodeError:
            pass
        if low.isascii():
            auto.add_word(low.encode("utf-16le").decode("latin-1"), (2 * len(low), True))
    if not any_word:
        return None
    auto.make_automaton()
    return auto, {d.lower() for d in derived}


def _joined(ch, ahead=False):
    """True when ch next to a name makes it part of a longer identifier (a model or file
    name such as rc_iit_018): those are left to the corpus-wide dictionary, so links agree."""
    return ch.isascii() and (ch.isalnum() or ch in ("_-" if ahead else "_-."))


def _replace_names(buf, matcher, ctx):
    """Replace the file's own names where they stand in free text (8-bit and UTF-16LE,
    any case) in bytearray buf, same length. Returns the count."""
    if matcher is None:
        return 0
    auto, derived = matcher
    low = bytes(buf).decode("latin-1").lower()
    hits = sorted((end - n + 1, -n, wide) for end, (n, wide) in auto.iter(low))
    pseudo = ctx.binary.pseudo
    done = last = 0
    size = len(low)
    for s, neg_n, wide in hits:
        if s < last:
            continue
        e = s - neg_n
        if wide:
            if s >= 2 and low[s - 1] == "\x00" and _joined(low[s - 2]):
                continue
            if e + 1 < size and low[e + 1] == "\x00" and _joined(low[e], True):
                continue
            if low[e:e + 2] == ".\x00" and e + 3 < size and low[e + 3] == "\x00" and low[e + 2].isalnum():
                continue
        else:
            if s and _joined(low[s - 1]):
                continue
            if e < size and _joined(low[e], True):
                continue
            if e + 1 < size and low[e] == "." and low[e + 1].isascii() and low[e + 1].isalnum():
                continue
            if e - s < 7 and len(_PRINTABLE.findall(buf, max(0, s - 4), s)) + \
                    len(_PRINTABLE.findall(buf, e, e + 4)) < 6:
                continue  # a short name with no text around it: chance bytes
        raw = bytes(buf[s:e])
        try:
            old = raw.decode("utf-16le" if wide else "latin-1")
        except UnicodeDecodeError:
            continue
        if old.lower() in _PROTECTED:
            continue
        new = pseudo.span(old).encode("utf-16le" if wide else "latin-1")
        if len(new) == e - s and new != raw:
            buf[s:e] = new
            done += 1
            last = e
            if old.lower() in derived:
                derived.discard(old.lower())
                ctx.binary.found.append(("person", old))
    return done


# -- Creo / Pro-ENGINEER ------------------------------------------------------------
#
# File layout (all releases): a text header ending in "#-END_OF_UGC_HEADER", then
#   Pro/E up to Wildfire: "#P_OBJECT" text block (save history with user and host names,
#     parameters), the release line, sections
#   Wildfire and Creo: "#UGC_TOC" tables (chained by NEXT_TOC_ENTRY) of 81-byte entries
#     "name offset length unpacked-length version type flag sum1 sum2", offsets relative
#     to the release line. A section is "#name\n" + body. sum1 is the byte sum of the
#     body, sum2 the sum of its running sums (both mod 65536). Bodies starting 1F 9D are
#     LZW (Unix compress without block mode); in Creo the save history and the parameters
#     live in such blocks as "PSB" token streams with a string table.

CREO_MAGIC = b"#UGC:"
# Model and file names stored in a Creo file always read what paths.py gives the file of
# that name (see creo_file and _stored_name): unchanged while paths.py keeps Creo file
# names, renamed together with the file otherwise.
# False: only identity fields are rewritten (users, hosts, authors, user folders) and
# those names where they recur as free text. True (not run on the full set): dictionary
# words and the project's in-tree rules are replaced in the remaining text as well.
CREO_DICTIONARY = False
_USER_DIR = re.compile(rb"[\\/](?:[Uu][Ss][Ee][Rr][Ss]|[Dd]ocuments and [Ss]ettings)[\\/]+"
                       rb"([^\\/:*?\"<>|\x00-\x1f\x7f-\x9f]{2,40})(?=[\\/])")


def _creo_text(buf, ctx):
    """The scan of one Creo buffer beyond the identity fields."""
    if CREO_DICTIONARY:
        return _scan(buf, ctx) + _local_rules(buf, ctx)
    done = 0
    for m in list(_USER_DIR.finditer(buf)):   # user folder of a stored path
        if m.group(1).decode("latin-1").strip().lower() not in _GENERIC_USERS:
            done += _span(buf, m.start(1), m.end(1), ctx, "user")
    return done
_HOST = re.compile(rb"Hostname: *'([^'\r\n]{1,80})'")
_POBJ_LINE = re.compile(rb"(\d+) (\d+)(?: (.*))?$")
_HDR_LINE = re.compile(rb"#- (HOST|LINK|DBID|CMNM|COSM) ?([^\\\n]*)")
_PSB = b"\xe0\x00PSB_Tokens\x00"
_PSB_TYPES = b"\xe0\x04type_har\x00\xf8"
_PSB_INTS = b"\xe0\x01int_har\x00\xf8"
_PSB_STRS = b"\xe0\x03string_xar\x00\xf8"


def lzw_decode(body):
    """Unpack a Unix-compress stream without block mode (the only kind Creo writes)."""
    if len(body) < 3 or body[0] != 0x1F or body[1] != 0x9D:
        raise ValueError("not LZW")
    maxbits = body[2] & 0x1F
    if body[2] & 0xE0 or not 9 <= maxbits <= 16:
        raise ValueError("unsupported LZW flags")
    data = bytes(body[3:]) + b"\x00\x00\x00"
    total = (len(body) - 3) * 8
    limit = 1 << maxbits
    table = [bytes((i,)) for i in range(256)]
    out = []
    n_bits, mask, bitpos, group = 9, 511, 0, 0
    prev = None
    frombytes = int.from_bytes
    while bitpos + n_bits <= total:
        byte = bitpos >> 3
        code = (frombytes(data[byte:byte + 3], "little") >> (bitpos & 7)) & mask
        bitpos += n_bits
        size = len(table)
        if code < size:
            entry = table[code]
        elif code == size and prev is not None and size < limit:
            entry = prev + prev[:1]
        else:
            raise ValueError("corrupt LZW stream")
        out.append(entry)
        if prev is not None and size < limit:
            table.append(prev + entry[:1])
            size += 1
        prev = entry
        if size > mask and n_bits < maxbits:  # wider codes start on an 8-code boundary
            used = (bitpos - group) % (n_bits * 8)
            if used:
                bitpos += n_bits * 8 - used
            n_bits += 1
            mask = (1 << n_bits) - 1
            group = bitpos
    return b"".join(out)


def lzw_encode(raw, maxbits=16):
    """Pack bytes the way Creo does (compress without block mode): lzw_decode's inverse."""
    out = bytearray(b"\x1f\x9d" + bytes((maxbits,)))
    if not raw:
        return bytes(out)
    limit = 1 << maxbits
    table = {}
    free = 256            # next code the packer assigns
    seen = 256            # table size on the unpacking side after each code
    n_bits, acc, accbits, in_group = 9, 0, 0, 0
    codes = []
    ent = raw[0]
    get = table.get
    for c in memoryview(raw)[1:]:
        key = (ent << 8) | c
        code = get(key)
        if code is not None:
            ent = code
            continue
        codes.append(ent)
        if free < limit:
            table[key] = free
            free += 1
        ent = c
    codes.append(ent)
    first = True
    for code in codes:
        acc |= code << accbits
        accbits += n_bits
        while accbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            accbits -= 8
        in_group += 1
        if first:
            first = False
        elif seen < limit:
            seen += 1
        if seen > (1 << n_bits) - 1 and n_bits < maxbits:
            rest = in_group % 8
            if rest:
                acc |= 0 << accbits
                accbits += (8 - rest) * n_bits
                while accbits >= 8:
                    out.append(acc & 0xFF)
                    acc >>= 8
                    accbits -= 8
            n_bits += 1
            in_group = 0
    if accbits:
        out.append(acc & 0xFF)
    return bytes(out)


def _unpack(body):
    """LZW body -> bytes; large blocks go through gzip (same format, much faster)."""
    if len(body) > 200000:
        try:
            done = subprocess.run(["gzip", "-dc"], input=bytes(body), stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=600)
            if done.returncode == 0 and done.stdout:
                return done.stdout
        except (OSError, subprocess.SubprocessError):
            pass
    return lzw_decode(body)


def creo_sums(body):
    """The two checksums a table-of-contents entry keeps for a section body."""
    s1 = s2 = 0
    for i in range(0, len(body), 1 << 16):
        block = body[i:i + (1 << 16)]
        s2 += s1 * len(block) + sum(itertools.accumulate(block))
        s1 += sum(block)
    return s1 & 0xFFFF, s2 & 0xFFFF


def _varint(d, p):
    b = d[p]
    if b < 0x80:
        return b, p + 1
    if b < 0xC0:
        return ((b & 0x3F) << 8) | d[p + 1], p + 2
    if b < 0xE0:
        return ((b & 0x1F) << 16) | (d[p + 1] << 8) | d[p + 2], p + 3
    if b == 0xEA:
        return int.from_bytes(d[p + 1:p + 4], "big"), p + 4
    if b == 0xEB:
        return int.from_bytes(d[p + 1:p + 5], "big"), p + 5
    raise ValueError("PSB number")


def psb_parse(raw):
    """Token types, integers and string-table spans of a Creo PSB stream."""
    p = raw.find(_PSB_TYPES, 0, 80)
    if p < 0:
        raise ValueError("PSB types")
    n, p = _varint(raw, p + len(_PSB_TYPES))
    types = bytearray()
    while len(types) < n:
        b = raw[p]
        p += 1
        if 0xE4 <= b <= 0xE6:               # repeat the previous element 1..3 times
            types += types[-1:] * (b - 0xE3)
        elif b == 0xE7:                     # ... or as often as the next number says
            times, p = _varint(raw, p)
            types += types[-1:] * times
        elif b >= 0x80:
            raise ValueError("PSB types")
        else:
            types.append(b)
    if raw[p:p + len(_PSB_INTS)] != _PSB_INTS:
        raise ValueError("PSB ints")
    n, p = _varint(raw, p + len(_PSB_INTS))
    ints, neg = [], False
    while len(ints) < n:
        b = raw[p]
        if 0xE4 <= b <= 0xE6:
            ints += ints[-1:] * (b - 0xE3)
            p += 1
            continue
        if b == 0xE7:
            times, p = _varint(raw, p + 1)
            ints += ints[-1:] * times
            continue
        if b == 0xF5:                       # sign of the next number
            neg = True
            p += 1
            continue
        if b == 0xF6:
            v, p = -1, p + 1
        else:
            v, p = _varint(raw, p)
        ints.append(-v if neg else v)
        neg = False
    p = raw.find(_PSB_STRS, p)
    if p < 0:
        raise ValueError("PSB strings")
    n, p = _varint(raw, p + len(_PSB_STRS))
    spans = []
    for _ in range(n):
        e = raw.index(b"\x00", p)
        spans.append((p, e))
        p = e + 1
    return bytes(types), ints, spans


def psb_values(types, ints, strings):
    """Walk the token stream. Returns (field values, parameters): field values are
    (field name, string index); parameters are (name string index, [value indexes])."""
    fields, params = [], []
    stack = []                              # [class name, current field, [(top field, leaf field, index)]]
    k, nstr = 0, len(strings)

    def name(i):
        return strings[i] if 0 <= i < nstr else ""

    for t in types:
        if t == 0x4F:                       # O: field name
            if stack:
                stack[-1][1] = name(ints[k])
            k += 1
        elif t == 0x4D:                     # M: object of a class
            stack.append([name(ints[k]), "", []])
            k += 1
        elif t == 0x49:                     # I: string value
            i = ints[k]
            k += 1
            if stack and 0 <= i < nstr:
                fields.append((stack[-1][1], i))
                stack[-1][2].append((stack[-1][1], stack[-1][1], i))
        elif t in (0x46, 0x50, 0x53, 0x54):  # F P S T: one integer each
            k += 1
        elif t == 0x43 and stack:           # C: end of object
            cls, _field, found = stack.pop()
            if "::Parameter_" in cls:
                names = [i for top, leaf, i in found if top == "Definition" and leaf == "ProEName"]
                values = [i for top, leaf, i in found if top == "Value" and leaf not in ("discr", "unit")]
                if names and values:
                    params.append((names[0], values))
            elif stack:
                top = stack[-1][1]
                stack[-1][2].extend((top, leaf, i) for _t, leaf, i in found)
    return fields, params


def creo_psb(buf, ctx):
    """Identity strings of one unpacked PSB block: user and host names of the save
    history, values of parameters that name a person or company."""
    types, ints, spans = psb_parse(buf)
    strings = [bytes(buf[a:b]).decode("latin-1") for a, b in spans]
    fields, params = psb_values(types, ints, strings)
    done, seen = 0, set()
    for field, i in fields:
        if i in seen:
            continue
        a, b = spans[i]
        if field == "UserName":
            seen.add(i)
            done += _span(buf, a, b, ctx, "user")
        elif field == "Comment":
            seen.add(i)
            m = _HOST.search(buf, a, b)
            if m:
                done += _span(buf, m.start(1), m.end(1), ctx, "user")
    for name_i, values in params:
        kind = name_kind(strings[name_i])
        for i in values:
            if kind and i not in seen:
                seen.add(i)
                done += _span(buf, spans[i][0], spans[i][1], ctx, kind)
    return done


def creo_pobject(data, a, b, ctx):
    """The "#P_OBJECT" text block of Pro/ENGINEER files: user and host names of the save
    history (usr_rev), values of parameters that name a person or company."""
    names, section, param, done = {}, "", "", 0
    pos = a
    while pos < b:
        nl = data.find(b"\n", pos, b)
        if nl < 0:
            nl = b
        line = bytes(data[pos:nl])
        if line[:1] == b"@":
            parts = line[1:].split(b" ")
            if len(parts) >= 2 and parts[1].isdigit():
                names[int(parts[1])] = parts[0].decode("latin-1")
        else:
            m = _POBJ_LINE.match(line.rstrip(b"\r"))
            if m:
                field = names.get(int(m.group(2)), "")
                if m.group(1) == b"1":
                    section = field
                if m.group(3):
                    va, vb = pos + m.start(3), pos + m.end(3)
                    if section == "usr_rev" and field == "name":
                        done += _span(data, va, vb, ctx, "user")
                    elif section == "usr_rev" and field == "comment":
                        h = _HOST.search(data, va, vb)
                        if h:
                            done += _span(data, h.start(1), h.end(1), ctx, "user")
                    elif section == "user_symbols" and field == "name":
                        param = m.group(3).decode("latin-1")
                    elif section == "user_symbols" and field.startswith("value(s_val)"):
                        kind = name_kind(param)
                        if kind:
                            done += _span(data, va, vb, ctx, kind)
        pos = nl + 1
    return done


_TIME = rb"\xe2[\x00-\x3b][\x00-\x3b][\x00-\x17][\x01-\x1f][\x00-\x0b][\x46-\x8c]"
_USER_DECL = re.compile(rb"\xe0[\x00-\x20]user_name\x00([\x20-\x7e]{2,60})\x00"
                        rb"(?=\xe0[\x00-\x20]modif_time\x00|" + _TIME + rb")")
_USER_STAMP = re.compile(rb"\x00" + _TIME)
_USER_VALUE = re.compile(rb"(?<![\x20-\x7e])[A-Za-z][A-Za-z0-9 ._\-]{1,39}$")
_PARAM = re.compile(rb"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]{2,31})\x00.{0,12}?[\xe2\xe3]\x33([\x20-\x7e]{0,80})\x00"
                    rb"(?:\xf1?\xf7.[\xe2\xe3]\x33([\x20-\x7e]{0,80})\x00)?", re.S)


def creo_plain(buf, ctx):
    """Identity strings of a section in Creo's field/value encoding: the user of each
    modification stamp (user_name + time), the host of each save comment, and the values
    of parameters that name a person or company."""
    done = 0
    for m in list(_HOST.finditer(buf)):
        done += _span(buf, m.start(1), m.end(1), ctx, "user")
    first = _USER_DECL.search(buf)
    if first:
        done += _span(buf, first.start(1), first.end(1), ctx, "user")
        stamps = []
        for m in _USER_STAMP.finditer(buf, first.end()):
            v = _USER_VALUE.search(buf, max(first.end(), m.start() - 41), m.start())
            if v:
                stamps.append(v.span())
        seen = collections.Counter(bytes(buf[a:b]) for a, b in stamps)
        for a, b in stamps:
            if seen[bytes(buf[a:b])] >= 2:  # a stamp user repeats; a one-off is not trusted
                done += _span(buf, a, b, ctx, "user")
    for m in list(_PARAM.finditer(buf)):
        kind = name_kind(m.group(1).decode("latin-1"))
        if kind:
            for g in (2, 3):
                if m.group(g):
                    done += _span(buf, m.start(g), m.end(g), ctx, kind)
    return done + _folder_rules(buf, ctx)


class _Entry:
    __slots__ = ("line", "width", "f", "start", "body", "end", "changed")


def creo_toc(data, base):
    """Every table-of-contents entry of a Creo file; None if the file has no tables."""
    pos = data.find(b"#UGC_TOC ", base, base + 400)
    if pos < 0:
        return None
    entries, seen = [], set()
    while pos >= 0 and pos not in seen:
        seen.add(pos)
        line_end = data.find(b"\n", pos)
        head = bytes(data[pos:line_end]).rstrip(b"#").split()
        count, width = int(head[2]), int(head[3])
        if not 40 <= width <= 400 or count > 100000:
            raise ValueError("Creo table header")
        nxt = -1
        for i in range(count):
            a = line_end + 1 + i * width
            line = bytes(data[a:a + width])
            f = line.rstrip(b"\n").rstrip(b"#").split(b" ")
            if len(f) != 9:
                if line.strip(b"#\n"):
                    raise ValueError("Creo table entry")
                continue
            e = _Entry()
            e.line, e.width, e.f, e.changed = a, width, f, False
            off, size = int(f[1], 16), int(f[2], 16)
            e.start = base + off
            marker = b"#" + f[0] + b"\n"
            e.end = e.start + size
            e.body = e.start + len(marker)
            if f[0] == b"NEXT_TOC_ENTRY":
                nxt = e.start
                e.body = -1
            elif data[e.start:e.body] != marker or e.end > len(data):
                e.body = -1                  # free space or an entry we do not understand
            if _entry_line(e) != line:
                raise ValueError("Creo table entry format")
            entries.append(e)
        pos = nxt if nxt > 0 and data[nxt:nxt + 9] == b"#UGC_TOC " else -1
    return entries


def _entry_line(e):
    text = b" ".join(e.f)
    if len(text) > e.width - 1:
        raise ValueError("Creo table entry too long")
    return text + b"#" * (e.width - 1 - len(text)) + b"\n"


_NAME_EXT = re.compile(rb"\.(?:prt|asm|drw|frm|sec|lay|mfg|sym|gph|dgm|mrk)(?![A-Za-z0-9_])", re.I)
_NAME_DECL = re.compile(rb"\xe0[\x00-\x20]([A-Za-z_]*name[A-Za-z_]*)\x00([\x20-\x7e]{3,80})\x00")
_NAME_FIELD = re.compile(rb"^@(model_name|old_name|generic_name|alias_name) (\d+) ", re.M)
_NAME_CHARS = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
_PSB_NAMES = ("Name", "ToModelNameAsStored", "LibraryName", "ToLibraryNameAsStored", "AsStoredName")


def creo_names(buf, names, texts):
    """Collect from one (original) Creo buffer, in lower case. names: the model and file
    names stored in it (name -> file type): whatever stands before a Creo file extension,
    the values of *name* fields, the model descriptors of a token stream. texts: values
    that are not names of people: every string of a token stream that is not an identity
    value, the values of the other parameters, descriptive *name* fields."""
    for m in _NAME_EXT.finditer(buf):
        j = m.start()
        while j > 0 and buf[j - 1] in _NAME_CHARS and m.start() - j < 80:
            j -= 1
        if m.start() - j >= 3:
            name = bytes(buf[j:m.start()]).decode("latin-1").lower()
            if not names.get(name):
                names[name] = m.group()[1:].decode("latin-1").lower()
    for m in _NAME_DECL.finditer(buf):
        if not re.search(rb"user|host|author|owner|login|creat|modif|compan", m.group(1)):
            value = m.group(2)
            if all(c in _NAME_CHARS or c in b".<>" for c in value):
                for piece in re.split(rb"[<>]+", value):
                    if len(piece) >= 3:
                        names.setdefault(piece.decode("latin-1").lower(), "")
            else:
                texts.add(value.decode("latin-1").lower())
    for m in _PARAM.finditer(buf):
        if not name_kind(m.group(1).decode("latin-1")):
            texts.update(v.decode("latin-1").lower() for v in (m.group(2), m.group(3)) if v and len(v) >= 3)
    if buf[:len(_PSB)] == _PSB:
        try:
            types, ints, spans = psb_parse(buf)
            strings = [bytes(buf[a:b]).decode("latin-1") for a, b in spans]
            fields, params = psb_values(types, ints, strings)
        except (ValueError, IndexError):
            return
        last = ""
        for field, i in fields:
            if field in _PSB_NAMES and len(strings[i]) >= 3:
                last = strings[i].lower() if field == "Name" else ""
                names.setdefault(strings[i].lower(), "")
            elif field == "Type" and last:
                if strings[i].lower() in ("prt", "asm", "drw", "frm", "sec", "lay", "mfg"):
                    names[last] = strings[i].lower()
                last = ""
        ident = {v for n, values in params if name_kind(strings[n]) for v in values}
        plain = {v for n, values in params if not name_kind(strings[n]) for v in values}
        for field, i in fields:
            if field in ("UserName", "Comment") or (field == "String" and i in ident and i not in plain):
                continue                      # an identity value (and nothing else uses the string)
            if len(strings[i]) >= 3:
                texts.add(strings[i].lower())


def creo_text_names(buf, names, texts):
    """The same for the text layout ("#P_OBJECT" and Pro/ENGINEER text sections): the
    values of model_name / old_name / generic_name / alias_name fields are names, the
    values of parameters that do not name a person are plain values."""
    wanted = {m.group(2) for m in _NAME_FIELD.finditer(buf)}
    fields, section, param = {}, "", ""
    for line in bytes(buf).split(b"\n"):
        if line[:1] == b"@":
            parts = line[1:].split(b" ")
            if len(parts) >= 2 and parts[1].isdigit():
                fields[parts[1]] = parts[0]
            continue
        m = _POBJ_LINE.match(line.rstrip(b"\r"))
        if not m:
            continue
        field = fields.get(m.group(2), b"")
        if m.group(1) == b"1":
            section = field
        value = (m.group(3) or b"").strip()
        if len(value) < 3:
            continue
        if m.group(2) in wanted and b" " not in value:
            names.setdefault(value.decode("latin-1").lower(), "")
        elif section == b"user_symbols" and field == b"name":
            param = value.decode("latin-1")
        elif section == b"user_symbols" and field.startswith(b"value(s_val)") and not name_kind(param):
            texts.add(value.decode("latin-1").lower())


_CREO_EXT = (".prt", ".asm", ".drw", ".frm", ".sec", ".lay", ".mfg", ".sym", ".gph", ".dgm", ".mrk")
_NAME_CACHE = {}


def _stored_name(text, ctx, cache):
    """What a stored model or file name must read after the run: the name paths.py gives
    the file with that name (so a reference always equals the file it points to, whether
    or not Creo files are renamed), in the letter case it is stored in. Anything that
    is not a model or file name reads as it was."""
    low = text.lower()
    new = cache.get(low)
    if new is None:
        stem, ext = low, ""
        dot = low.rfind(".")
        if dot > 0 and low[dot:] in _CREO_EXT:
            stem, ext = low[:dot], low[dot:]
        kind = ext or "." + (_MODELS.get(stem) or "prt")
        new = low
        if low in _MODELS or stem in _MODELS:
            key = (stem, kind, tuple(r.pattern for r in getattr(ctx, "rules", ()) or ()))
            mapped = _NAME_CACHE.get(key)
            if mapped is None:
                mapped = stem
                try:
                    full = ctx.mapper.file_name(stem + kind + ".1", getattr(ctx, "rules", ()))
                    if len(full) == len(stem) + len(kind) + 2 and full[len(stem):] == kind + ".1":
                        mapped = full[:len(stem)]
                except Exception:
                    pass
                if len(_NAME_CACHE) < 300000:
                    _NAME_CACHE[key] = mapped
            new = mapped + ext
        cache[low] = new
    return "".join(n.upper() if o.isupper() else n for o, n in zip(text, new))


def _protect(before, buf, matcher, ctx=None, cache=None, renames=True):
    """Set every occurrence of a stored model or file name (a name standing alone, not
    part of a longer identifier) to the name its file gets; with Creo file names kept,
    that is the name as it was. Returns how many occurrences a rule had changed or the
    mapper renames."""
    if matcher is None or (not renames and before == buf):
        return 0
    low = before.decode("latin-1").lower()
    size, done = len(low), 0
    for end, n in matcher.iter(low):
        s, e = end - n + 1, end + 1
        if s and (low[s - 1].isascii() and (low[s - 1].isalnum() or low[s - 1] in "_-")):
            continue
        if e < size and (low[e].isascii() and (low[e].isalnum() or low[e] in "_-")):
            continue
        want = before[s:e]
        if ctx is not None:
            want = _stored_name(want.decode("latin-1"), ctx, cache).encode("latin-1")
        if buf[s:e] != want:
            buf[s:e] = want
            done += 1
    return done


def _name_matcher(names):
    auto, any_word = ahocorasick.Automaton(), False
    for name in names:
        if len(name) >= 3:
            try:
                auto.add_word(name.encode("latin-1").decode("latin-1"), len(name))
                any_word = True
            except UnicodeEncodeError:
                pass
    if not any_word:
        return None
    auto.make_automaton()
    return auto


def creo_file(src, dst, ctx):
    with open(src, "rb") as f:
        data = bytearray(f.read())
    head_end = data.find(b"#-END_OF_UGC_HEADER", 0, 8192)
    if not data.startswith(CREO_MAGIC) or head_end < 0:
        raise ValueError("not a Creo file")
    first = bytes(data[:data.find(b"\n")]).split()
    base = data.find(b"\n", head_end) + 1
    pobj = pobj_end = -1
    if data[base:base + 9] == b"#P_OBJECT":
        pobj, pobj_end = base, data.find(b"#END_OF_P_OBJECT", base)
        if pobj_end < 0:
            raise ValueError("Creo object block")
    if len(first) >= 2 and re.fullmatch(rb"[0-9a-f]{8}", first[-2]):
        base = int(first[-2], 16)
    try:
        entries = creo_toc(data, base)
    except (ValueError, IndexError):
        entries = False                       # tables we cannot read: sections stay as they are
    # the names stored in the file (its own and those it refers to) are collected first:
    # no rule may change them, so links to and from this file stay as they are
    names, texts = {}, set()
    m = re.search(rb"#- CMNM [0-9a-f]{3}([^\\\n]*)", bytes(data[:head_end]))
    if m:
        full = m.group(1).strip().decode("latin-1").lower()
        if "." in full:
            names[full.rsplit(".", 1)[0]] = full.rsplit(".", 1)[1]
            names[full] = ""
    creo_text_names(data[head_end:base], names, texts)
    work = []                                 # [entry, buffer, packed, stored body, original bytes]
    if entries:
        for e in entries:
            if e.body < 0 or e.end <= e.body:
                continue
            body = bytes(data[e.body:e.end])
            if body[:2] == b"\x1f\x9d":
                try:
                    raw = _unpack(body)
                except ValueError:
                    continue                  # another packing (display data): left as is
                packed = True
            elif body[:3] == b"\xff\xd8\xff" or body[:4] == b"\x89PNG":
                continue                      # preview image
            else:
                raw, packed = body, False
            creo_names(raw, names, texts)
            work.append([e, bytearray(raw), packed, body, raw])
    elif entries is None:
        rest = bytes(data[head_end:])
        creo_names(rest, names, texts)
        creo_text_names(rest, names, texts)
    mark = len(ctx.binary.found)
    _PROTECTED.clear()
    _MODELS.clear()
    _MODELS.update((n, t) for n, t in names.items() if len(n) >= 3)
    _PROTECTED.update(_MODELS)
    _PROTECTED.update(t for t in texts if 3 <= len(t) <= 200)
    try:
        return _creo_rules(src, dst, ctx, data, head_end, base, pobj, pobj_end, entries, work, mark)
    finally:
        _PROTECTED.clear()
        _MODELS.clear()


def _creo_rules(src, dst, ctx, data, head_end, base, pobj, pobj_end, entries, work, mark):
    keep, cache = _name_matcher(_PROTECTED), {}
    # does paths.py rename any file this one names? (not while Creo file names are kept)
    renames = any(_stored_name(n, ctx, cache) != n for n in _MODELS)
    orig = bytes(data)
    before = orig[:base] if entries else orig
    total = 0
    # header: host / link / database lines and the common name
    for m in _HDR_LINE.finditer(bytes(data[:head_end])):
        a, b = m.span(2)
        if m.group(1) == b"HOST":
            total += _span(data, a, a + len(m.group(2).rstrip()), ctx, "user")
        elif CREO_DICTIONARY:
            part = bytearray(m.group(2))
            if _scan(part, ctx):
                data[a:b] = part
                total += 1
    if pobj >= 0:
        total += creo_pobject(data, pobj, pobj_end, ctx)
    if not entries:
        STATS["creo old layout" if entries is None else "creo tables unreadable"] += 1
        if entries is None:
            # Pro/ENGINEER before Wildfire: text sections without checksums
            rest = bytearray(data[head_end:])
            n = creo_plain(rest, ctx) + _creo_text(rest, ctx)
            n += _replace_names(rest, _own_names(ctx, mark, True), ctx)
        else:
            rest = bytearray(data[head_end:base])
            n = _replace_names(rest, _own_names(ctx, mark, True), ctx)
        if n:
            data[head_end:head_end + len(rest)] = rest
            total += n
        head = bytearray(data[:len(before)])
        if _protect(before, head, keep, ctx, cache, renames):
            data[:len(before)] = head
            STATS["creo header with a stored name set"] += 1
        _set_own_name(orig, data, head_end, ctx, cache)
        return _creo_out(src, dst, data, max(total, 1) if data != before else 0)
    if pobj >= 0:
        buf = bytearray(data[pobj:pobj_end])
        total += _creo_text(buf, ctx)
        data[pobj:pobj_end] = buf
    safe, safe_mark = bytes(data), len(ctx.binary.found)
    try:
        out, n = creo_sections(data, base, entries, pobj, pobj_end, work, (keep, cache, renames), ctx, mark)
    except (ValueError, IndexError, KeyError, OverflowError, MemoryError):
        del ctx.binary.found[safe_mark:]      # keep the header and save-history changes only
        STATS["creo sections kept as is"] += 1
        out, n = bytearray(safe), 0
    head = bytearray(out[:base])
    if _protect(before, head, keep, ctx, cache, renames):
        out[:base] = head
        STATS["creo header with a stored name set"] += 1
    _set_own_name(orig, out, head_end, ctx, cache)
    return _creo_out(src, dst, out, max(total + n, 1) if out != orig else 0)


def _set_own_name(orig, data, head_end, ctx, cache):
    """The common name of the header ("#- CMNM" + 3 hex digits of length + file name)."""
    m = re.search(rb"#- CMNM [0-9a-f]{3}([^\\\n]*)", orig[:head_end])
    if m:
        name = m.group(1).rstrip()
        want = _stored_name(name.decode("latin-1"), ctx, cache).encode("latin-1")
        if len(want) == len(name):
            data[m.start(1):m.start(1) + len(name)] = want


def _creo_out(src, dst, data, total):
    if total:
        _write(dst, data)
    else:
        _place(src, dst)
    return total


def creo_sections(data, base, entries, pobj, pobj_end, work, keep, ctx, mark):
    """Apply the rules to every unpacked section of a Creo file with tables; returns
    (new file bytes, count)."""
    total = 0
    for item in work:
        e, buf = item[0], item[1]
        if buf.startswith(_PSB):
            found = len(ctx.binary.found)
            try:
                total += creo_psb(buf, ctx)
            except (ValueError, IndexError):
                buf[:] = item[4]
                del ctx.binary.found[found:]
                STATS["creo token stream unreadable"] += 1
            total += _folder_rules(buf, ctx)
        else:
            total += creo_plain(buf, ctx)
        total += _creo_text(buf, ctx)
    # names the file revealed are replaced in all of its sections and in the object block
    names = _own_names(ctx, mark, True)
    if names is not None:
        for item in work:
            total += _replace_names(item[1], names, ctx)
        if pobj >= 0:
            buf = bytearray(data[pobj:pobj_end])
            n = _replace_names(buf, names, ctx)
            if n:
                data[pobj:pobj_end] = buf
                total += n
    # stored model and file names stay exactly as they were
    for item in work:
        if _protect(item[4], item[1], keep[0], ctx, keep[1], keep[2]):
            STATS["creo section with a stored name set"] += 1
        item[0].changed = item[1] != item[4]
    # write back; a repacked section may change size, so everything after it moves
    splices, expect, touched = [], {}, []
    for e, buf, packed, body, _raw in work:
        if not e.changed:
            continue
        why = ""
        was = creo_sums(body)
        both = int(e.f[8], 16) != 0 or was[1] == 0   # some Creo 3 builds store 0 as the second sum
        if int(e.f[7], 16) != was[0] or (both and int(e.f[8], 16) != was[1]):
            why = "checksum rule"
        elif packed and lzw_encode(lzw_decode(body)) != body:
            why = "packing"
        if why:
            e.changed = False                 # not the layout we know: section left as in the source
            STATS["creo section skipped (%s) %s" % (why, e.f[0].decode("latin-1").rstrip("0123456789")[:14])] += 1
            continue
        if packed:
            new = lzw_encode(bytes(buf))
            splices.append((e.body, e.end, new))
            expect[e.start] = bytes(buf)
        else:
            new = bytes(buf)
            data[e.body:e.end] = new
        STATS["creo packed section rewritten" if packed else "creo plain section rewritten"] += 1
        s1, s2 = creo_sums(new)
        e.f[2] = b"%x" % (e.body - e.start + len(new))
        e.f[7], e.f[8] = b"%04x" % s1, b"%04x" % (s2 if both else 0)
        touched.append(e.start)
    if not touched:
        return data, total
    splices.sort()
    for i in range(1, len(splices)):
        if splices[i][0] < splices[i - 1][1]:
            raise ValueError("overlapping Creo sections")

    def moved(pos):
        return pos + sum(len(new) - (b - a) for a, b, new in splices if b <= pos)

    for e in entries:
        new_start = moved(e.start)
        if new_start != e.start:
            e.f[1] = b"%x" % (new_start - base)
            e.changed = True
    lines = [(moved(e.line), _entry_line(e)) for e in entries if e.changed]
    out = bytearray()
    pos = 0
    for a, b, new in splices:
        out += data[pos:a]
        out += new
        pos = b
    out += data[pos:]
    for a, line in lines:
        out[a:a + len(line)] = line
    creo_check(out, base, len(entries), {moved(k): v for k, v in expect.items()},
               {moved(k) for k in touched})
    return out, total


def creo_check(data, base, count, expect, touched):
    """Re-read a rewritten Creo file: the tables list the same entries, every rewritten
    section is where its entry says with valid checksums, and the repacked ones unpack
    to the intended bytes."""
    entries = creo_toc(data, base)
    if entries is None or len(entries) != count:
        raise ValueError("Creo tables changed")
    for e in entries:
        if e.body < 0:
            if e.start in touched:
                raise ValueError("Creo section lost")
            continue
        if e.start in touched:
            body = bytes(data[e.body:e.end])
            s1, s2 = creo_sums(body)
            if int(e.f[7], 16) != s1 or int(e.f[8], 16) not in (s2, 0):
                raise ValueError("Creo checksum")
            touched.discard(e.start)
            if e.start in expect and lzw_decode(body) != expect.pop(e.start):
                raise ValueError("Creo section content")
    if expect or touched:
        raise ValueError("Creo rewritten section not found")


# -- JT ----------------------------------------------------------------------------------
#
# 80-byte version line, byte order, table of contents (segment id, offset, length), then
# segments: 24-byte header (id, type, length) + data. Scene graph, metadata and property
# segments are stored packed (zlib in JT 8/9, an XZ stream in JT 10): flag, packed length,
# algorithm byte, stream. Strings are length-prefixed UTF-16. Metadata is a list of
# "Key::" strings each followed by a typed value.

JT_MAGIC = b"Version "
_JT_KEY = re.compile(rb"((?:[\x20-\x7e]\x00){3,60}):\x00:\x00")


def _jt_layout(data):
    """(major version, table offset, entry size, [(entry position, offset, length)])."""
    m = re.match(rb"Version +(\d+)\.(\d+)", bytes(data[:80]))
    if not m or len(data) < 120 or data[80] != 0:
        raise ValueError("JT header")       # byte order 1 (big endian) is not handled
    major = int(m.group(1))
    if major >= 10:
        toc, size, fmt = struct.unpack_from("<Q", data, 85)[0], 32, "<QI"
    else:
        toc, size, fmt = struct.unpack_from("<i", data, 85)[0], 28, "<iI"
    count = struct.unpack_from("<i", data, toc)[0]
    if not 0 < count < 1000000 or toc + 4 + count * size > len(data):
        raise ValueError("JT table")
    entries = []
    for i in range(count):
        pos = toc + 4 + i * size
        off, length = struct.unpack_from(fmt, data, pos + 16)
        if off < 0 or length < 24 or off + length > len(data) or \
                struct.unpack_from("<i", data, off + 20)[0] != length:
            raise ValueError("JT segment")
        entries.append((pos, off, length))
    return major, toc, size, entries


def _jt_unpack(seg):
    """(algorithm, unpacked bytes) of a segment, or (0, None) when it is stored plain."""
    if len(seg) < 33 + 12:
        return 0, None
    flag, packed = struct.unpack_from("<ii", seg, 24)
    alg = seg[32]
    if flag in (2, 3) and alg in (2, 3) and packed == len(seg) - 32:
        try:
            if alg == 2:
                return 2, zlib.decompress(bytes(seg[33:]))
            return 3, lzma.decompress(bytes(seg[33:]), format=lzma.FORMAT_XZ)
        except (zlib.error, lzma.LZMAError):
            pass
    return 0, None


def jt_strings(buf, ctx):
    """Values of the metadata keys that name a person or company ("Author::" ...)."""
    done = 0
    for m in list(_JT_KEY.finditer(buf)):
        kind = name_kind(m.group(1).decode("utf-16le"))
        p = m.end()
        if not kind or p + 5 > len(buf) or buf[p] != 1:   # value type 1: a string
            continue
        count = struct.unpack_from("<i", buf, p + 1)[0]
        if 0 < count <= 400 and p + 5 + 2 * count <= len(buf):
            done += _span(buf, p + 5, p + 5 + 2 * count, ctx, kind, "utf-16le")
    return done


def jt_file(src, dst, ctx):
    with open(src, "rb") as f:
        data = bytearray(f.read())
    major, toc, size, entries = _jt_layout(data)
    mark = len(ctx.binary.found)
    total = 0
    work = {}                                # offset -> [length, algorithm, buffer, original]
    for pos, off, length in entries:
        if off in work:
            continue
        seg = data[off:off + length]
        alg, raw = _jt_unpack(seg)
        if raw is None:
            continue                         # shape data: no text
        buf = bytearray(raw)
        total += jt_strings(buf, ctx) + _folder_rules(buf, ctx) + _scan(buf, ctx)
        total += _local_rules(buf, ctx)
        work[off] = [length, alg, buf, raw]
    names = _own_names(ctx, mark)
    for item in work.values():
        total += _replace_names(item[2], names, ctx)
    if not total:
        _place(src, dst)
        return 0
    splices, sizes = [], {}
    for off, (length, alg, buf, raw) in sorted(work.items()):
        if bytes(buf) == raw:
            continue
        if alg == 2:
            packed = zlib.compress(bytes(buf), 9)
        else:
            check = data[off + 33 + 7] & 0x0F
            packed = lzma.compress(bytes(buf), format=lzma.FORMAT_XZ,
                                   check=check if check in (0, 1, 4, 10) else lzma.CHECK_CRC64)
        seg = bytearray(data[off:off + 33]) + packed
        struct.pack_into("<i", seg, 20, len(seg))
        struct.pack_into("<i", seg, 28, len(packed) + 1)
        splices.append((off, off + length, bytes(seg)))
        sizes[off] = len(seg)
    for i in range(1, len(splices)):
        if splices[i][0] < splices[i - 1][1]:
            raise ValueError("overlapping JT segments")

    def moved(pos):
        return pos + sum(len(new) - (b - a) for a, b, new in splices if b <= pos)

    out = bytearray()
    pos = 0
    for a, b, new in splices:
        out += data[pos:a]
        out += new
        pos = b
    out += data[pos:]
    new_toc = moved(toc)
    struct.pack_into("<Q" if major >= 10 else "<i", out, 85, new_toc)
    for pos, off, length in entries:
        at = moved(pos) + 16
        if major >= 10:
            struct.pack_into("<QI", out, at, moved(off), sizes.get(off, length))
        else:
            struct.pack_into("<iI", out, at, moved(off), sizes.get(off, length))
    # re-read: same segments, and every repacked one unpacks to the intended bytes
    major2, _toc, _size, entries2 = _jt_layout(out)
    if major2 != major or len(entries2) != len(entries):
        raise ValueError("JT table changed")
    for (pos, off, length), (pos2, off2, length2) in zip(entries, entries2):
        if out[off2:off2 + 20] != data[off:off + 20]:
            raise ValueError("JT segment moved wrongly")
        if off in sizes:
            _alg, raw = _jt_unpack(out[off2:off2 + length2])
            if raw != bytes(work[off][2]):
                raise ValueError("JT segment content")
        elif out[off2:off2 + length2] != data[off:off + length]:
            raise ValueError("JT segment changed")
    STATS["jt segments repacked"] += len(splices)
    _write(dst, out)
    return total


# -- CATIA V5 ----------------------------------------------------------------------------

CATIA_MAGIC = b"V5_CFV2\x00"


def raw_file(src, dst, ctx):
    """A binary format without packed blocks or checksums over its text (CATIA V5):
    names are replaced in place, 8-bit and UTF-16, nothing moves."""
    with open(src, "rb") as f:
        data = bytearray(f.read())
    mark = len(ctx.binary.found)
    total = _folder_rules(data, ctx) + _scan(data, ctx) + _local_rules(data, ctx)
    total += _replace_names(data, _own_names(ctx, mark), ctx)
    if total:
        _write(dst, data)
    else:
        _place(src, dst)
    return total


# -- Fusion 360 --------------------------------------------------------------------------
#
# .f3d is a ZIP of the design: "Manifest.dat" / "Properties.dat" / "*Stream.dat" records
# with length-prefixed UTF-16 and 8-bit strings, ASM (ACIS) solids (.smb / .smbh /
# .smt), material assets (.protein, itself a ZIP), images and JSON. .f3z is a ZIP of
# several .f3d plus a JSON manifest. Member names are generated ids and stay as they are.

FUSION_EXT = (".f3d", ".f3z")
FUSION_DELEGATE = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".pdf", ".zip", ".svg")


def fusion_member(src, dst, ctx):
    """One binary member of a Fusion design (records with length-prefixed UTF-16 and
    8-bit strings, ASM solids): strings are replaced in place."""
    with open(src, "rb") as f:
        data = bytearray(f.read())
    total = _folder_rules(data, ctx) + _scan(data, ctx) + _local_rules(data, ctx)
    if total:
        _write(dst, data)
    else:
        _place(src, dst)
    return total


def _text_rules(path, ctx):
    """Path rules of this module on a text member (a temporary copy), before the normal
    text pipeline scans it."""
    if os.path.getsize(path) > (64 << 20):
        return 0
    with open(path, "rb") as f:
        data = bytearray(f.read())
    total = _folder_rules(data, ctx) + _local_rules(data, ctx)
    if total:
        with open(path, "wb") as f:
            f.write(data)
    return total


def _zstd():
    """The Zstandard codec (ZIP method 93, used by newer Fusion files), or None."""
    try:
        from compression import zstd            # Python 3.14+
        return zstd.decompress, lambda data: zstd.compress(data, 9)
    except ImportError:
        pass
    try:
        import zstandard
    except ImportError:
        return None

    def unpack(raw):
        return zstandard.ZstdDecompressor().decompressobj().decompress(raw)

    return unpack, lambda data: zstandard.ZstdCompressor(level=9).compress(data)


def _zip_unpack(method, raw):
    if method == 0:
        return raw
    if method == 8:
        return zlib.decompressobj(-15).decompress(raw)
    if method == 93:
        codec = _zstd()
        if codec:
            return codec[0](raw)
    raise NotImplementedError("ZIP method %d" % method)


def _zip_pack(method, data):
    if method == 0:
        return data
    if method == 8:
        packer = zlib.compressobj(6, zlib.DEFLATED, -15)
        return packer.compress(data) + packer.flush()
    return _zstd()[1](data)


def zip_members(src, dst, ctx, depth, member):
    """Rewrite a ZIP container member by member: member(name, a, b, depth) writes the
    de-identified copy of file a at b and returns its count. Names, order, methods,
    dates and attributes are kept; a member that does not change keeps its packed bytes
    exactly. Members in a method this Python cannot unpack are carried over as they are."""
    total = 0
    part = dst + ".part"
    tmpdir = tempfile.mkdtemp(dir=ctx.tmp)
    try:
        with zipfile.ZipFile(src) as zin, open(src, "rb") as fin, open(part, "wb") as out:
            infos = zin.infolist()
            comment = bytearray(zin.comment)
            if comment and ctx.text.scan(comment, first=False):
                total += 1
            central = []
            for i, info in enumerate(infos):
                fin.seek(info.header_offset)
                head = fin.read(30)
                if head[:4] != b"PK\x03\x04":
                    raise zipfile.BadZipFile("local header")
                name_len, extra_len = struct.unpack("<HH", head[26:30])
                name = fin.read(name_len)
                fin.seek(extra_len, 1)
                raw = fin.read(info.compress_size)
                if len(raw) != info.compress_size or info.compress_size >= 0xFFFFFFFF or info.file_size >= 0xFFFFFFFF:
                    raise zipfile.BadZipFile("member size")
                crc, size, method = info.CRC, info.file_size, info.compress_type
                if not info.is_dir() and not info.flag_bits & 1 and size:
                    try:
                        data = _zip_unpack(method, raw)
                    except NotImplementedError:
                        data = None
                        STATS["zip member in an unsupported method carried over"] += 1
                    if data is not None:
                        if len(data) != size or zlib.crc32(data) & 0xFFFFFFFF != crc:
                            raise zipfile.BadZipFile("member check")
                        ext = os.path.splitext(info.filename)[1].lower()
                        a, b = os.path.join(tmpdir, "in%d%s" % (i, ext)), os.path.join(tmpdir, "out%d%s" % (i, ext))
                        with open(a, "wb") as g:
                            g.write(data)
                        del data
                        n = member(info.filename, a, b, depth)
                        if n:
                            with open(b, "rb") as g:
                                data = g.read()
                            raw = _zip_pack(method, data)
                            crc, size = zlib.crc32(data) & 0xFFFFFFFF, len(data)
                            total += n
                        for leftover in (a, b):
                            if os.path.lexists(leftover):
                                os.remove(leftover)
                offset = out.tell()
                if offset >= 0xFFFFFFFF or len(raw) >= 0xFFFFFFFF or size >= 0xFFFFFFFF:
                    raise ValueError("ZIP64 needed")
                flags = info.flag_bits & ~0x08   # sizes are in the header: no data descriptor
                dostime = (info.date_time[3] << 11) | (info.date_time[4] << 5) | (info.date_time[5] // 2)
                dosdate = ((info.date_time[0] - 1980) << 9) | (info.date_time[1] << 5) | info.date_time[2]
                fixed = struct.pack("<HHHHHIIIHH", info.extract_version, flags, method, dostime, dosdate,
                                    crc, len(raw), size, len(name), 0)
                out.write(b"PK\x03\x04" + fixed + name)
                out.write(raw)
                central.append(b"PK\x01\x02" + struct.pack("<BB", info.create_version, info.create_system) + fixed[:-2] +
                               struct.pack("<HHHHII", 0, len(info.comment), 0, info.internal_attr,
                                           info.external_attr, offset) + name + info.comment)
            start = out.tell()
            for record in central:
                out.write(record)
            if len(central) > 0xFFFF or start >= 0xFFFFFFFF:
                raise ValueError("ZIP64 needed")
            out.write(b"PK\x05\x06" + struct.pack("<HHHHIIH", 0, 0, len(central), len(central),
                                                  out.tell() - start, start, len(comment)) + bytes(comment))
    except Exception:
        if os.path.lexists(part):
            os.remove(part)
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if total == 0:
        os.remove(part)
        _place(src, dst)
        return 0
    try:                                      # the rewritten container reads back clean
        with zipfile.ZipFile(part) as z, zipfile.ZipFile(src) as old:
            if z.namelist() != old.namelist():
                raise ValueError("ZIP member list changed")
            for info in z.infolist():
                if info.compress_type in (0, 8) and not info.flag_bits & 1:
                    with z.open(info) as f:
                        while f.read(1 << 20):
                            pass
    except Exception:
        os.remove(part)
        raise
    os.replace(part, dst)
    return total


def fusion_file(src, dst, ctx, depth=0):
    def one(name, a, b, level):
        ext = os.path.splitext(name)[1].lower()
        with open(a, "rb") as f:
            head = f.read(8192)
        if level < MAX_DEPTH and head[:4] == b"PK\x03\x04" and ext in FUSION_EXT + (".protein",):
            try:
                return zip_members(a, b, ctx, level + 1, one)
            except (zipfile.BadZipFile, ValueError, OSError, KeyError, NotImplementedError):
                STATS["fusion nested container kept as is"] += 1
                _place(a, b)
                return 0
        if ext in FUSION_DELEGATE:
            return _process(a, b, ext, ctx, level + 1)
        if head and b"\x00" not in head:       # text: logs, XML, JSON, NC code
            return _text_rules(a, ctx) + _process(a, b, ext, ctx, level + 1)
        return fusion_member(a, b, ctx)

    return zip_members(src, dst, ctx, depth, one)


# -- 7z and RAR archives -----------------------------------------------------------------

MAX_DEPTH = 4
SEVENZ_MAGIC = b"7z\xbc\xaf\x27\x1c"
RAR_MAGIC = b"Rar!\x1a\x07"
NOTES = []        # why a file of the last handle() call was kept as is (for the defect list)


def _process(a, b, ext, ctx, depth):
    """A member of a container goes through the normal per-format pipeline."""
    from . import formats
    return formats.process(a, b, ext, ctx, depth)[1]


def _run(cmd, cwd=None):
    try:
        done = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=3600)
        return done.returncode
    except (OSError, subprocess.SubprocessError):
        return -1


def _extract(kind, src, where):
    """Unpack an archive into the empty folder where; True when a tool managed it."""
    if kind == "7z":
        tools = [["7z", "x", "-y", "-p", "-bd", "-o" + where, src],
                 ["7za", "x", "-y", "-p", "-bd", "-o" + where, src],
                 ["bsdtar", "-xf", src, "-C", where]]
    else:
        tools = [["unrar", "x", "-y", "-p-", "-idq", src, where + os.sep],
                 ["7z", "x", "-y", "-p", "-bd", "-o" + where, src],
                 ["bsdtar", "-xf", src, "-C", where],
                 ["unrar-free", "-x", "-f", src, where + os.sep]]
    for cmd in tools:
        if not shutil.which(cmd[0]):
            continue
        if _run(cmd) == 0 and any(os.scandir(where)):
            return True
        for entry in list(os.scandir(where)):  # partial output of a tool that failed
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                os.remove(entry.path)
    if kind == "7z":
        try:
            import py7zr
            with py7zr.SevenZipFile(src) as z:
                z.extractall(where)
            return any(os.scandir(where))
        except Exception:
            pass
    return False


def _restore_times(src, where):
    """Set the unpacked members' times from the archive's own listing: unrar-free does not
    restore them, so the new archive would record the time of this run."""
    if not shutil.which("7z"):
        return
    try:
        done = subprocess.run(["7z", "l", "-slt", "-p", "-ba", src], capture_output=True, timeout=600,
                              env=dict(os.environ, TZ="UTC", LC_ALL="C.UTF-8"))
    except (OSError, subprocess.SubprocessError):
        return
    root = os.path.realpath(where)
    for block in done.stdout.decode("utf-8", "surrogateescape").split("\n\n"):
        fields = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
        path, when = fields.get("Path", ""), fields.get("Modified", "")
        try:
            stamp = calendar.timegm(time.strptime(when[:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        member = os.path.join(where, *path.replace("\\", "/").split("/"))
        if path and os.path.lexists(member) and os.path.realpath(member).startswith(root + os.sep):
            os.utime(member, (stamp, stamp), follow_symlinks=False)


def _vint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _rar_block(kind, flags, fields, data_size=None):
    body = _vint(kind) + _vint(flags | (2 if data_size is not None else 0))
    if data_size is not None:
        body += _vint(data_size)
    body += fields
    body = _vint(len(body)) + body
    return struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF) + body


def rar_store(tree, part):
    """Write the folder tree as a RAR 5 archive with stored (not packed) members: the
    free tools can only read RAR, so this is the writer used when 'rar' is not installed."""
    with open(part, "wb") as out:
        out.write(b"Rar!\x1a\x07\x01\x00")
        out.write(_rar_block(1, 0, _vint(0)))
        for root, dirs, files in os.walk(tree):
            dirs.sort()
            rel = os.path.relpath(root, tree)
            for name in sorted(dirs) + sorted(files):
                path = os.path.join(root, name)
                arc = (name if rel == "." else rel.replace(os.sep, "/") + "/" + name).encode("utf-8", "surrogateescape")
                st = os.lstat(path)
                is_dir = os.path.isdir(path) and not os.path.islink(path)
                if not is_dir and not os.path.isfile(path):
                    continue
                size = 0 if is_dir else st.st_size
                crc = 0
                if not is_dir:
                    with open(path, "rb") as f:
                        for chunk in iter(lambda: f.read(1 << 20), b""):
                            crc = zlib.crc32(chunk, crc)
                fields = _vint((1 if is_dir else 4) | 2) + _vint(size) + _vint(st.st_mode & 0xFFFF)
                fields += struct.pack("<I", int(st.st_mtime) & 0xFFFFFFFF)
                if not is_dir:
                    fields += struct.pack("<I", crc & 0xFFFFFFFF)
                fields += _vint(0) + _vint(1) + _vint(len(arc)) + arc
                out.write(_rar_block(2, 0, fields, size))   # folders too carry a (zero) data size
                if not is_dir:
                    with open(path, "rb") as f:
                        shutil.copyfileobj(f, out, 1 << 20)
        out.write(_rar_block(5, 0, _vint(0)))


def _pack(kind, tree, part):
    """Pack the folder tree into a new archive of the same kind; returns how."""
    if kind == "7z":
        for tool in ("7z", "7za"):
            if shutil.which(tool) and _run([tool, "a", "-t7z", "-mx=5", "-mmt=1", "-y", "-bd", part, "."], cwd=tree) == 0:
                return tool
        try:
            import py7zr
            with py7zr.SevenZipFile(part, "w") as z:
                z.writeall(tree, arcname="")
            return "py7zr"
        except Exception:
            return ""
    if shutil.which("rar") and _run(["rar", "a", "-r", "-y", "-idq", "-m3", "-ma5", "-mt1", part, "."], cwd=tree) == 0:
        return "rar"
    if os.path.lexists(part):
        os.remove(part)
    rar_store(tree, part)
    return "stored"


def _test(kind, part):
    """An independent tool can read the new archive (when one is installed)."""
    tools = [["7z", "t", "-p", "-bd", part]] if kind == "7z" else \
        [["unrar", "t", "-p-", "-idq", part], ["7z", "t", "-p", "-bd", part], ["bsdtar", "-tf", part]]
    for cmd in tools:
        if shutil.which(cmd[0]):
            return _run(cmd) == 0
    return True


def archive_file(src, dst, ctx, depth=0):
    """7z / RAR: unpack, send every member through the pipeline (names are renamed like
    corpus paths), pack again in the same format."""
    with open(src, "rb") as f:
        head = f.read(8)
    kind = "7z" if head.startswith(SEVENZ_MAGIC) else "rar"
    tmp = tempfile.mkdtemp(dir=ctx.tmp)
    part = os.path.join(tmp, "new." + kind)
    try:
        old, new = os.path.join(tmp, "old"), os.path.join(tmp, "new")
        os.mkdir(old)
        os.mkdir(new)
        if depth >= MAX_DEPTH or not _extract(kind, src, old):
            NOTES.append("%s archive could not be unpacked (protected, damaged or an unsupported variant); kept as is" % kind.upper())
            STATS["archive kept as is (%s)" % kind] += 1
            _place(src, dst)
            return 0
        _restore_times(src, old)
        total, taken = 0, set()
        for root, dirs, files in os.walk(old):
            dirs.sort()
            rel = os.path.relpath(root, old)
            rel = "" if rel == "." else rel.replace(os.sep, "/") + "/"
            for name in sorted(dirs) + sorted(files):
                a = os.path.join(root, name)
                is_dir = os.path.isdir(a) and not os.path.islink(a)
                arc = rel + name
                renamed = ctx.member(arc + "/")[:-1] if is_dir else ctx.member(arc)
                if renamed.lower() in taken or not renamed or renamed.startswith("/") or ".." in renamed.split("/"):
                    renamed = arc
                taken.add(renamed.lower())
                total += renamed != arc
                b = os.path.join(new, *renamed.split("/"))
                os.makedirs(os.path.dirname(b), exist_ok=True)
                if is_dir:
                    os.makedirs(b, exist_ok=True)
                elif os.path.islink(a):
                    os.symlink(os.readlink(a), b)
                    continue
                elif os.path.isfile(a):
                    total += _process(a, b, os.path.splitext(name)[1].lower(), ctx, depth + 1)
                else:
                    continue
                st = os.lstat(a)
                os.utime(b, (st.st_atime, st.st_mtime))
        if not total:
            _place(src, dst)
            return 0
        # folder times: the newest entry below it (an empty folder: the newest file). Writing
        # files stamps folders with the current time, which the new archive would record
        files_new = [os.path.join(r, n) for r, _d, fs in os.walk(new) for n in fs]
        latest = max([os.lstat(p).st_mtime for p in files_new if not os.path.islink(p)] or [0])
        for root, dirs, files in os.walk(new, topdown=False):
            for n in dirs + files:  # links copied above carry the time of this run
                if os.path.islink(os.path.join(root, n)):
                    os.utime(os.path.join(root, n), (latest, latest), follow_symlinks=False)
            newest = max([os.lstat(os.path.join(root, n)).st_mtime for n in dirs + files] or [latest])
            os.utime(root, (newest, newest), follow_symlinks=False)
        how = _pack(kind, new, part)
        if not how or not os.path.isfile(part) or not _test(kind, part):
            NOTES.append("%s archive could not be packed again; kept as is" % kind.upper())
            STATS["archive kept as is (%s)" % kind] += 1
            _place(src, dst)
            return 0
        if how == "stored":
            NOTES.append("RAR archive rewritten with stored (uncompressed) members: no free tool packs RAR")
        STATS["archive repacked (%s, %s)" % (kind, how)] += 1
        if os.path.lexists(dst):
            os.remove(dst)
        shutil.move(part, dst)
        return total
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- dispatch ------------------------------------------------------------------------

def sniff(src):
    with open(src, "rb") as f:
        head = f.read(64)
    if head.startswith(CREO_MAGIC):
        return "creo"
    if head.startswith(JT_MAGIC) and b" JT" in head[:24]:
        return "jt"
    if head.startswith(CATIA_MAGIC):
        return "catia"
    if head.startswith(SEVENZ_MAGIC) or head.startswith(RAR_MAGIC):
        return "archive"
    if head.startswith(b"PK\x03\x04") and os.path.splitext(src)[1].lower() in FUSION_EXT:
        return "fusion"
    return ""


HANDLERS = {"creo": creo_file, "jt": jt_file, "catia": raw_file, "archive": archive_file,
            "fusion": fusion_file}
_depth = [0]


def handle(src, dst, ctx, depth=None):
    """De-identified copy of src at dst (same extension). Returns the number of
    replacements; never raises and never leaves dst missing. Why a file was kept as it
    is (if so) is appended to NOTES."""
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    mark_b, mark_t = len(ctx.binary.found), len(ctx.text.found)
    if _depth[0] == 0:
        del NOTES[:]
    depth = _depth[0] if depth is None else max(depth, _depth[0])
    kind = ""
    try:
        kind = sniff(src)
        if os.path.lexists(dst):
            os.remove(dst)
        if kind in ("archive", "fusion"):
            _depth[0] = depth + 1
            try:
                return HANDLERS[kind](src, dst, ctx, depth)
            finally:
                _depth[0] = depth
        if kind:
            return HANDLERS[kind](src, dst, ctx)
        return other_file(src, dst, ctx)
    except Exception as exc:
        del ctx.binary.found[mark_b:]
        del ctx.text.found[mark_t:]
        STATS["kept as is after %s in %s" % (type(exc).__name__, kind or "unknown")] += 1
        NOTES.append("%s file could not be parsed (%s); kept as is" % (kind or "native", type(exc).__name__))
    for leftover in (dst, dst + ".part"):
        if os.path.lexists(leftover):
            os.remove(leftover)
    _place(src, dst)
    return 0


def other_file(src, dst, ctx):
    """Not one of the formats above (a text file with a CAD extension, an unknown
    binary): text is scanned as text, anything else is copied."""
    with open(src, "rb") as f:
        head = f.read(8192)
    if head and b"\x00" not in head and sum(1 for b in head if b < 9 or 13 < b < 32) * 50 < len(head) \
            and os.path.getsize(src) < (64 << 20):
        with open(src, "rb") as f:
            data = bytearray(f.read())
        total = ctx.text.scan(data)
        if total:
            _write(dst, data)
            return total
    _place(src, dst)
    return 0
