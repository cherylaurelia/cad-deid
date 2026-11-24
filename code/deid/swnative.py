"""Account and identity residue inside OLE compound files and newer SolidWorks files.

formats._rewrite_parts calls fix_parts(parts, ctx) once per file after its own passes,
with one [name, original bytes, bytearray] per stream or inflated chunk. This module reads
the places where these formats record who worked on a file, and replaces every name found
there in all parts of the file, always at the same byte length:

- SolidWorks: the list of every account that ever saved the file (moHeader_c /
  su_CStringArray in Header2 and its copies), per-user view records (uiUserModelEnv_c),
  "Created by" reports, user folders written as DOCUME~1 short paths;
- property sets in any code page: author, last saved by, manager, company, the Inventor and
  Solid Edge designer / user fields, custom properties whose name says "drawn by",
  "checked by" ... in several languages;
- the docProps XML of newer SolidWorks files (creator, lastModifiedBy, named properties);
- Parasolid transmit headers (USER, SITE, computer name), the Solid Edge version history
  (user and computer name), the user of an embedded Excel workbook, NX attribute fields;
- containers stored inside a stream: an OLE file, a newer SolidWorks file, a ZIP, zlib
  blocks. Each is treated stream by stream and written back at the same size.

Names with letters outside ASCII (CJK, Cyrillic, accented) get a same-length pseudonym in
the encoding of the place they stand in. Every name read that can be searched for is
appended to ctx.binary.found. Nothing here raises and no length ever changes.

Also for formats.py: fix_slack() (the same names in the bytes of an OLE file that belong
to no live stream), deflate_fit() (a deflate stream that fills the space of a rewritten
block exactly, with 7-Zip's smaller encoder when zlib does not fit), deflate_small() and
lock_file() (SolidWorks "~$" owner files).
"""
import io
import os
import re
import shutil
import struct
import subprocess
import tempfile
import zlib

import ahocorasick

from .identities import GENERIC_USERS, GENERIC_VALUES, STOP
from .pseudo import KEEP
from .scan import anchored, in_base64, is_generic, replace_names, texty

MAX_DEPTH = 3            # containers inside containers
ZLIB_BLOCKS = True       # also rewrite zlib streams found in the middle of a part
GENERIC_ACCOUNTS = True  # "User", "Owner", "Admin" ... in an account slot get a pseudonym, like "Administrator"
MAX_NAME = 80
_CSTR = b"\xff\xfe\xff"  # start of an MFC Unicode string: then a length byte and UTF-16
_SW_MARK = b"\x14\x00\x06\x00\x08\x00"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"BM", b"GIF8", b"II*\x00", b"MM\x00*")
_STACK = []              # the runs in progress, outermost first (a nested container sees its parents)
_CALLS = [0]
_LAST = {}               # the names read from the file treated last, for fix_slack()

# -- pseudonyms for what Pseudo.span leaves alone ---------------------------------

# Alphabets with upper and lower case: (first, last code point, lower pool, upper pool).
# Every letter of a pool takes the same number of bytes as the letters it replaces, in
# UTF-8, UTF-16 and the Windows code page of its script.
_ALPHABETS = (
    (0x00C0, 0x00FF, "àáâãäåçèéêëìíîïñòóôõöùúûüý", "ÀÁÂÃÄÅÇÈÉÊËÌÍÎÏÑÒÓÔÕÖÙÚÛÜÝ"),
    (0x0100, 0x017F, "ąćčďęěłńňőřśšťůűźżž", "ĄĆČĎĘĚŁŃŇŐŘŚŠŤŮŰŹŻŽ"),
    (0x0370, 0x03FF, "αβγδεζηθικλμνξοπρστυφχψω", "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"),
    (0x0400, 0x04FF, "абвгдежзиклмнопрстуфхцчшэюя", "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЭЮЯ"),
)
_KANA = (
    (0x3041, 0x3096, "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわ"),
    (0x30A1, 0x30FA, "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワ"),
)
_POOLS = {}


def _common(first, last, *codecs):
    """The characters of a range that every one of the codecs writes in two bytes."""
    out = []
    for cp in range(first, last):
        ch = chr(cp)
        try:
            if all(len(ch.encode(c)) == 2 for c in codecs):
                out.append(ch)
        except UnicodeEncodeError:
            continue
    return "".join(out)


def _pool(ch):
    """(characters a letter outside ASCII may be replaced with, whether its script writes
    words). Han, kana and hangul are replaced letter by letter, alphabets word by word."""
    cp = ord(ch)
    if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
        if "han" not in _POOLS:  # shared by simplified, traditional and Japanese code pages
            _POOLS["han"] = _common(0x4E00, 0x9FA6, "gb2312", "big5", "shift_jis")
        return _POOLS["han"], False
    if 0xAC00 <= cp <= 0xD7A3:
        if "hangul" not in _POOLS:
            _POOLS["hangul"] = _common(0xAC00, 0xD7A4, "euc_kr")
        return _POOLS["hangul"], False
    for first, last, pool in _KANA:
        if first <= cp <= last:
            return pool, False
    for first, last, lower, upper in _ALPHABETS:
        if first <= cp <= last:
            return (upper if ch.isupper() else lower), True
    return "", False


def _numbers(pseudo, text, n):
    """n keyed numbers (0..17575) for a string. They are read off the pseudonym of an
    ASCII word that spells its code points, so they depend on the key like every other
    pseudonym and come out the same on every machine."""
    token = "zq" + "".join(chr(97 + (ord(c) // d) % 26) for c in text for d in (17576, 676, 26, 1))
    word = pseudo.span(token)
    out = []
    for i in range(n):
        a, b, c = (ord(x) - 97 for x in word[2 + 3 * i:5 + 3 * i])
        out.append((a * 676 + b * 26 + c) % 17576)
    return out


def pseudonym(text, pseudo):
    """Same-length pseudonym of an identity string. ASCII words come from pseudo.span, so
    they match every other place the pipeline replaces them. On top of that: a value made
    of initials only ("J.M.G") gets other initials, and letters outside ASCII that span
    left as they were are replaced within their own script."""
    new = pseudo.span(text)
    n = len(text)
    if len(new) != n:
        return text
    out = list(new)
    if new == text:
        alone = [i for i, c in enumerate(text) if c.isascii() and c.isalpha()
                 and not (i and text[i - 1].isalnum()) and not (i + 1 < n and text[i + 1].isalnum())]
        if len(alone) >= 2:
            for i, c in zip(alone, pseudo.span("".join(text[i] for i in alone))):
                out[i] = c
    i = 0
    while i < n:
        if not text[i].isalnum():
            i += 1
            continue
        j = i
        while j < n and text[j].isalnum():
            j += 1
        word = text[i:j]
        if not word.isascii():
            untouched, nums = new[i:j] == word, None
            for k in range(i, j):
                c = text[k]
                if ord(c) < 128 or not c.isalpha() or out[k] != c:
                    continue
                pool, worded = _pool(c)
                if not pool:
                    continue
                if worded:
                    if not untouched:
                        continue  # span already changed this word ("séba"): keep its result
                    if nums is None:
                        nums = _numbers(pseudo, word.lower(), j - i)
                    at = nums[k - i] % len(pool)
                else:
                    at = _numbers(pseudo, c, 1)[0] % len(pool)
                if pool[at].lower() == c.lower():
                    at = (at + 1) % len(pool)
                out[k] = pool[at]
        i = j
    return "".join(out)


# -- which values name somebody ---------------------------------------------------

_NOBODY = GENERIC_USERS | GENERIC_VALUES | set("""
администратор пользователь админ владелец 管理员 用户 您的姓名 姓名 作者 公司 管理員 使用者
localservice networkservice systemprofile xpuser computer workstation laptop notebook
templates шаблоны документы общие 桌面 文档 我的文档 公用 共享 模板 新建文件夹 vorlagen plantillas modèles
""".split()) | {"all users", "default user", "solid edge", "local service", "network service", "your name here",
                "рабочий стол", "все пользователи"}
_LICENCE = re.compile(r"(?i)creative commons|attribution|share-?alike|unported|\bcc[ -]by\b|licen[sc]e|public domain|\bgpl\b")
# Everyday words that are nevertheless the login of whoever saved the file when they stand in
# an account slot (last saved by, save history, a user's folder). formats.py gives
# "Administrator" a pseudonym there; with GENERIC_ACCOUNTS the other default logins get one too.
_DEFAULT_LOGINS = set("""
admin user owner operator guest root student staff employee pc xpuser usuario utilisateur benutzer
utente gebruiker uporabnik administrador administrateur администратор пользователь админ 管理员 用户
""".split())
_SHORT_SYSTEM = re.compile(r"(?:admini|alluse|defaul|public|locals|networ)~\d+(?:\.\w+)?$")
_SPLIT = re.compile(r"[;|\r\n\t]+")
_NOT_LOGIN = re.compile(r"(?i)[\\/:*?\"<>|]|\.(?:sld\w{3}|ipt|iam|idw|ipn|par|psm|asm|dft|prt|drw|ste?p|igs|iges|x_[tb]|dwg|dxf|pdf"
                        r"|stl|xls\w?|doc\w?|txt|xml|zip|jpg|png|bmp|\w{3}dot)$")


def nobody(value, account=False):
    """True when the value of an identity field names no one. account: the field holds a
    login, where "Administrator", the other default logins and a number of three or more
    digits name the account that saved the file."""
    v = value.strip(" \t\r\n\x00").lower()
    if len(v) < 2 or _SHORT_SYSTEM.match(v) or (len(v) > 4 and len(set(v)) == 1) or _LICENCE.search(v):
        return True
    if account and (v == "administrator" or (GENERIC_ACCOUNTS and v in _DEFAULT_LOGINS)):
        return False
    if v in _NOBODY:
        return True
    if any(c.isalpha() for c in v):
        return is_generic(v)
    return not (account and sum(c.isdigit() for c in v) >= 3)


# Property names that mean "a person or organisation follows", in the languages seen in
# the corpus. Values under these names are identity fields.
_HINT = re.compile(
    r"author|drawn|draw[ _]?by|drafter|drafted|designer|designed|design[ _]?by|engineer(?!ing)|checked|checker"
    r"|check[ _]?by|approv|creator|created[ _]?by|saved[ _]?by|modified[ _]?by|owner|manager|company|customer|client"
    r"|user[ _]?name|responsible|reviewed|reviewer|prepared|originator"
    r"|dessin|v[ée]rifi|approuv|soci[ée]t[ée]|auteur|concepteur|zeichner|gezeichnet|gepr[üu]ft|konstrukt|bearbeiter"
    r"|ersteller|firma|kunde|verfasser|autor|dibuja|dise[ñn]ad|dissenyat|desenh|projetista|revisad|aprobad|aprovad"
    r"|comprobad|elaborad|empresa|guardado por|progettista|disegnat|controllat"
    r"|разраб|провер|(?:^|[ _.])пров|(?:^|[ _.])утв|утверд|контр|(?:^|[ _.])нач|руковод|автор|чертил|конструктор"
    r"|исполнител|организац|предприят"
    r"|设计|設計|制图|製圖|绘图|描图|校对|校核|校图|审核|審核|审定|批准|作者|姓名|公司|保存者|作成者|承認|検図")
_NOT_HINT = re.compile(r"date|дата|datum|fecha|日期|时间|time|number|\bno\b|\bnr\b|num|code|status|state|type|version"
                       r"|revision|path|file|folder|url|link|note|comment|description")
_ORG_HINT = re.compile(r"company|customer|client|firma|soci|empresa|kunde|организац|предприят|公司")
_ACCOUNT_HINT = re.compile(r"saved[ _]?by|modified[ _]?by|user[ _]?name|guardado por|保存者")


def hint(name):
    """(kind, is a login field) when a property name announces a person or organisation."""
    low = name.lower()
    if not _HINT.search(low) or _NOT_HINT.search(low):
        return None
    return ("org" if _ORG_HINT.search(low) else "person"), bool(_ACCOUNT_HINT.search(low))


# Identity properties by the first four bytes of the property set id, as stored.
_PROP_PIDS = {
    "e0859ff2": {4: ("person", True), 8: ("user", True)},       # SummaryInformation: author, last saved by
    "02d5cdd5": {14: ("person", False), 15: ("org", False)},    # DocumentSummaryInformation: manager, company
    "0f3f8532": {10: ("person", False), 12: ("person", False), 34: ("person", False), 41: ("person", True),
                 42: ("person", False), 43: ("person", False)},  # Inventor design tracking
    "39de383d": {4: ("person", True), 8: ("user", True)},       # Inventor summary information
    "0080f58c": {14: ("person", False), 15: ("org", False)},    # Inventor document summary information
    "30fb61d8": {16: ("user", True)},                            # Inventor design tracking control: user
    "a24f02cc": {20: ("person", False)},                         # Solid Edge extended summary: full name
}
_XML_PIDS = {"f29f85e0": _PROP_PIDS["e0859ff2"], "d5cdd502": _PROP_PIDS["02d5cdd5"]}

_COPYRIGHT = re.compile(r"(?i)(?:copyright\s*(?:\(c\)|©)|©|copyright(?=\s+\d{4}))[\s:,]*(?:\d{4}(?:\s*[-–]\s*\d{4})?)?[\s,]*(?:by\s+)?"
                        r"([^\r\n\x00.;,(<&]{3,60})")
_CONTACT = re.compile(r"(?i)(?:QQ群?|微信|WeChat|电话|手机|电话号码|Tel|Phone|Mobile|Fax)[\s.]*[:：]?\s*(\+?\d[\d ()-]{4,18}\d)")
_ENTITY = re.compile(r"&#?\w{1,10};")
_XML_TAG = re.compile(rb"<((?:dc|cp):(?:creator|lastModifiedBy|contributor|publisher)|Manager|Company)>([^<>]{1,300})</")
_XML_PROP = re.compile(rb"<property\b([^<>]*)>\s*<vt:lpw?str>([^<>]{1,300})</vt:lpw?str>")
_XML_NAME = re.compile(rb'\bname="([^"]*)"')
_XML_PID = re.compile(rb'\bpid="(\d+)"')
_XML_SECTION = re.compile(rb'<propertySection\b[^<>]*\bfmtid="\{([0-9A-Fa-f]{8})')
_XT_FIELD = re.compile(rb"[;\n][ \t]*(USER|SITE|MC|MC_ID)=([^;\r\n]{1,80})")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,30}$")
_PLATFORM = re.compile(r"(?i)(?:unknown|win(?:dows|32|64|nt)?|nt|x64|x86(?:_64)?|i\d86|amd64|intel|linux|unix|sun\d*|sgi|hp(?:ux)?"
                       r"|osf\d*|alpha|mac(?:os)?|darwin|arm(?:64)?)$")
_HOST8 = re.compile(rb"(?:DESKTOP|LAPTOP)-[A-Z0-9]{7,8}(?![A-Za-z0-9])")
_HOST16 = re.compile(rb"(?:D\x00E\x00S\x00K\x00T\x00O\x00P\x00|L\x00A\x00P\x00T\x00O\x00P\x00)-\x00(?:[A-Z0-9]\x00){7,8}")
_FOLDER16 = re.compile(rb"\\\x00((?:[^\\/\x00-\x1f:*?\"<>|]\x00|[\x00-\xff][\x01-\xd7\xe0-\xff]){1,40})\\\x00")
_FOLDER8 = re.compile(rb"\\([^\\/\x00-\x1f:*?\"<>|]{1,60})\\")
_AFTER_BY = re.compile(rb"[ \t]*(?:\r?\n)?[ \t]*([^\r\n\x00]{2,60}?)[ \t]*(?:\r?\n|\x00| {2})")
_GVH = re.compile(rb"(?s)([\x02-\x40])\x00\x00\x00([\x02-\x40])\x00\x00\x00")
_BIFF_USER = re.compile(rb"(?s)\x5c\x00\x70\x00(.)\x00([\x00\x01])")
_NX_FIELD = re.compile(rb"(?:DSNR|ENGR|CHKR|APVR|APPR|DRWN|DFTR|AUTHOR|DESIGNER|ENGINEER|CHECKER)=([^|\x00\r\n]{2,40})")
_USER_ROOTS = ("Users", "users", "USERS", "Documents and Settings", "DOCUMENTS AND SETTINGS",
               "documents and settings", "DOCUME~1", "docume~1", "Docume~1")
_ROOT8 = re.compile(rb"\\(?:" + b"|".join(re.escape(r.encode("ascii")) for r in _USER_ROOTS) + rb")(?=\\)")
_ROOT16 = re.compile(rb"\\\x00(?:" + b"|".join(re.escape(r.encode("utf-16le")) for r in _USER_ROOTS) + rb")(?=\\\x00)")
_STRICT_EDGE = set(b"\\/\"'<>\x00")
_ZIP_TEXT = (".xml", ".rels", ".vml", ".txt", ".csv", ".json", ".htm", ".html")


def _codec(cp):
    cp &= 0xFFFF
    return {65001: "utf-8", 1200: "utf-16le", 0: "cp1252", 10000: "mac_roman", 932: "shift_jis",
            936: "gbk", 949: "cp949", 950: "big5"}.get(cp, "cp%d" % cp)


def _each(raw, literal):
    """Start of every occurrence of literal in raw."""
    p = raw.find(literal)
    while p >= 0:
        yield p
        p = raw.find(literal, p + 1)


def _cstring(raw, p):
    """(text, start, end) of the MFC Unicode string at raw[p:], or None."""
    if raw[p:p + 3] != _CSTR or p + 4 > len(raw):
        return None
    n, a = raw[p + 3], p + 4
    if n == 0xFF:
        if a + 2 > len(raw):
            return None
        n, a = struct.unpack_from("<H", raw, a)[0], a + 2
    b = a + 2 * n
    if n > 2000 or b > len(raw):
        return None
    try:
        return raw[a:b].decode("utf-16le"), a, b
    except UnicodeDecodeError:
        return None


def _is_picture(name, data):
    low = name.lower()
    return "preview" in low or "thumbnail" in low or low.endswith(("png", "bmp", "jpg", "jpeg")) \
        or data[:4].startswith(_IMAGE_MAGIC) or data[:3] == b"\xff\xd8\xff"


# -- one file ---------------------------------------------------------------------

class _Run:
    """The parts of one container and the names it revealed."""

    def __init__(self, parts, ctx, parent=None):
        self.parts = parts
        self.ctx = ctx
        self.pseudo = ctx.binary.pseudo
        self.depth = parent.depth + 1 if parent else 0
        self.names = dict(parent.names) if parent else {}   # identity value -> kind
        self.fresh = {}                                     # the ones this run read itself
        self.codec = parent.codec if parent else ""         # 8-bit code page named by a property set
        self.texts = {}                                     # part -> byte ranges that are text for sure
        self.inside = {}                                    # containers stored inside the parts
        self.masks = {}                                     # ... and the byte ranges they take
        self.count = 0

    # -- writing ------------------------------------------------------------------
    def put(self, k, a, b, new):
        """Write new over bytes a..b of part k if it has the same length and differs."""
        buf = self.parts[k][2]
        if len(new) != b - a or b > len(buf) or buf[a:b] == new:
            return False
        if any(a < hi and lo < b for lo, hi in self.masks.get(k, ())):
            return False  # inside a nested container: nested() rewrites that properly
        buf[a:b] = new
        self.count += 1
        return True

    def note(self, value, kind):
        """Remember a name the file revealed, each person of a list on their own."""
        pieces = _SPLIT.split(value)
        for piece in list(pieces):  # "name, telephone, e-mail": each on its own as well
            if "," in piece:
                pieces += [p for p in piece.split(",") if len(p.strip()) >= 4 and p.strip().lower() not in KEEP]
        pieces += [p.strip().split("@")[0] for p in pieces if "@" in p and " " not in p.strip()]
        for piece in pieces:
            piece = piece.strip(" \t\r\n\x00.,:;-_\"'()")
            if "\\" in piece or "/" in piece or _NOT_LOGIN.search(piece):
                continue  # a path or a file name is not searched for
            if 2 <= len(piece) <= MAX_NAME and piece not in self.names and not nobody(piece, True):
                self.names[piece] = self.fresh[piece] = kind

    def decode8(self, raw):
        """(text, codec) of 8-bit text: ASCII, UTF-8, the file's own code page, or Latin-1."""
        if raw.isascii():
            return raw.decode("ascii"), "ascii"
        for codec in ("utf-8", self.codec):
            if codec:
                try:
                    return raw.decode(codec), codec
                except (UnicodeDecodeError, LookupError):
                    continue
        return raw.decode("latin-1"), "latin-1"

    def value(self, k, a, b, codec, kind, account=False, field=False, remember=True, login=False):
        """Pseudonymize the identity stored in bytes a..b of part k (read from the original
        bytes, so it does not matter what earlier passes already changed there)."""
        raw = self.parts[k][1][a:b]
        try:
            text, codec = self.decode8(raw) if codec == "8bit" else (raw.decode(codec), codec)
        except (UnicodeDecodeError, LookupError):
            return False
        core = text.strip(" \t\r\n\x00")
        contact = field and not any(c.isalpha() for c in core) and sum(c.isdigit() for c in core) >= 7
        if (nobody(core, account) and not contact) or len(core) > 2 * MAX_NAME:
            return False
        if field and ("\\" in core or core[:1] == '"' or "$PRP" in core or len(core.split()) > 8):
            return False  # a path, a SolidWorks expression, a sentence: not a name
        if login and (len(core) > 40 or _NOT_LOGIN.search(core)):
            return False  # a path or a file name where an account name was expected
        new = pseudonym(text, self.pseudo)
        if codec == "utf-8" and "&" in text:  # XML: character references stay as they are
            new = list(new)
            for m in _ENTITY.finditer(text):
                new[m.start():m.end()] = text[m.start():m.end()]
            new = "".join(new)
        try:
            packed = new.encode(codec)
        except (UnicodeEncodeError, LookupError):
            return False
        if remember:
            self.note(core, kind)
        return self.put(k, a, b, packed)

    def free_text(self, k, a, b, codec):
        """Titles, comments, keywords: the holder of a copyright notice, and telephone or
        messenger numbers written after a label."""
        raw = self.parts[k][1][a:b]
        if b - a < 8:
            return
        try:
            text = raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            return
        for rx, kind in ((_COPYRIGHT, "org"), (_CONTACT, "")):
            for m in rx.finditer(text):
                old = m.group(1).rstrip()
                if kind and (nobody(old) or re.match(r"(?i)all rights", old)):
                    continue
                try:  # only the name itself is written: the rest of the value is other passes' business
                    at = a + len(text[:m.start(1)].encode(codec))
                    new = pseudonym(old, self.pseudo).encode(codec)
                    size = len(old.encode(codec))
                except (UnicodeEncodeError, LookupError):
                    continue
                self.put(k, at, at + size, new)
                if kind:
                    self.note(old, kind)
                elif old.isdigit() and len(old) >= 7:
                    self.names.setdefault(old, "user")

    # -- readers (all work on the original bytes) ----------------------------------
    def savers(self, k, raw):
        """SolidWorks save history: moHeader_c holds a string array with every account
        that ever saved the file; uiUserModelEnv_c records start with a user name."""
        for p in _each(raw, b"su_CStringArray"):
            if b"moHeader_c" not in raw[max(0, p - 32):p]:
                continue  # the same class also holds lists of file names and paths
            p += 15
            if p + 2 > len(raw):
                continue
            n = struct.unpack_from("<H", raw, p)[0]
            p += 2
            if n == 0xFFFF and p + 4 <= len(raw):
                n, p = struct.unpack_from("<I", raw, p)[0], p + 4
            for _ in range(min(n, 500)):
                got = _cstring(raw, p)
                if got is None:
                    break
                self.value(k, got[1], got[2], "utf-16le", "user", account=True, login=True)
                p = got[2]
        for p in _each(raw, b"uiUserModelEnv_c"):
            got = _cstring(raw, p + 16)
            if got:
                self.value(k, got[1], got[2], "utf-16le", "user", account=True, login=True)

    def props(self, k, raw):
        """Identity values of an OLE property set stream, in the set's own code page."""
        if len(raw) < 48 or raw[:2] != b"\xfe\xff":
            return
        nsets = struct.unpack_from("<I", raw, 24)[0]
        for s in range(min(nsets, 4)):
            fmtid = raw[28 + 20 * s:32 + 20 * s].hex()
            base = struct.unpack_from("<I", raw, 44 + 20 * s)[0]
            if base + 8 > len(raw):
                continue
            nprops = struct.unpack_from("<I", raw, base + 4)[0]
            if nprops > 4000 or base + 8 + 8 * nprops > len(raw):
                continue
            index = [struct.unpack_from("<II", raw, base + 8 + 8 * j) for j in range(nprops)]
            cp = 1252
            for pid, off in index:
                if pid == 1 and base + off + 6 <= len(raw):
                    cp = struct.unpack_from("<H", raw, base + off + 4)[0]
            codec = _codec(cp)
            if cp not in (0, 1200, 1252, 65001) and not self.codec:
                self.codec = codec
            wanted = dict(_PROP_PIDS.get(fmtid, ()))
            for pid, off in index:
                if pid == 0:
                    wanted.update(self._dictionary(raw, base + off, cp, codec))
            for pid, off in index:
                pos = base + off
                if pid < 2 or pos + 8 > len(raw):
                    continue
                vt, count = struct.unpack_from("<II", raw, pos)
                vt &= 0xFFFF
                if vt == 30:      # VT_LPSTR: byte count, bytes in the set's code page
                    a, b, enc = pos + 8, pos + 8 + count, codec
                    if cp != 1200 and count > 3 and raw[a + 1:a + 2] == b"\x00" and raw[a + 3:a + 4] == b"\x00":
                        enc = "utf-16le"  # some writers store UTF-16 here all the same
                elif vt == 31:    # VT_LPWSTR: character count, UTF-16
                    a, b, enc = pos + 8, pos + 8 + 2 * count, "utf-16le"
                else:
                    continue
                if count > 4000 or b > len(raw) or (enc == "utf-16le" and (b - a) % 2):
                    continue
                self.texts.setdefault(k, []).append((a, b))
                want = wanted.get(pid)
                if want:
                    self.value(k, a, b, enc, want[0], want[1], field=True)
                else:
                    self.free_text(k, a, b, enc)

    @staticmethod
    def _dictionary(raw, pos, cp, codec):
        """{property id: (kind, login)} for the named properties whose name is a hint."""
        out = {}
        try:
            count = struct.unpack_from("<I", raw, pos)[0]
            pos += 4
            for _ in range(min(count, 2000)):
                cid, length = struct.unpack_from("<II", raw, pos)
                pos += 8
                if cp == 1200:
                    name = raw[pos:pos + 2 * length].decode("utf-16le", "replace")
                    pos += 2 * length + (-2 * length) % 4
                else:
                    name = raw[pos:pos + length].decode(codec, "replace")
                    pos += length
                want = hint(name.rstrip("\x00"))
                if want:
                    out[cid] = want
        except (struct.error, LookupError):
            pass
        return out

    def xml(self, k, raw):
        """docProps XML of a newer SolidWorks file (UTF-8)."""
        for m in _XML_TAG.finditer(raw):
            tag = m.group(1)
            self.value(k, m.start(2), m.end(2), "utf-8", "org" if tag in (b"Company", b"dc:publisher") else "person",
                       account=b"lastModifiedBy" in tag, field=True)
        sections = [(m.start(), m.group(1).decode("ascii").lower()) for m in _XML_SECTION.finditer(raw)]
        for m in _XML_PROP.finditer(raw):
            name = _XML_NAME.search(m.group(1))
            want = hint(name.group(1).decode("utf-8", "replace")) if name and name.group(1) else None
            if want is None:
                pid = _XML_PID.search(m.group(1))
                inside = [fmtid for at, fmtid in sections if at < m.start()]
                if pid and inside:
                    want = _XML_PIDS.get(inside[-1], {}).get(int(pid.group(1)))
            if want:
                self.value(k, m.start(2), m.end(2), "utf-8", want[0], want[1], field=True)
            else:
                self.free_text(k, m.start(2), m.end(2), "utf-8")
        for m in re.finditer(rb"<(?:dc:description|dc:subject|dc:title|cp:keywords|dc:keywords)>([^<>]{8,400})</", raw):
            self.free_text(k, m.start(1), m.end(1), "utf-8")

    def folders(self, k, raw):
        """The user's folder in stored paths: \\Users\\name\\, \\Documents and Settings\\name\\
        and the short form \\DOCUME~1\\name\\, as UTF-16 or 8-bit text."""
        for root in _ROOT16.finditer(raw):
            m = _FOLDER16.match(raw, root.end())
            # ...\Users\name\DOCUME~1 is that user's Documents folder: the short form counts under a drive only
            if m and not (b"~" in root.group() and raw[root.start() - 2:root.start()] != b":\x00"):
                self._folder(k, m, "utf-16le")
        for root in _ROOT8.finditer(raw):
            m = _FOLDER8.match(raw, root.end())
            if m and not (b"~" in root.group() and raw[root.start() - 1:root.start()] != b":"):
                self._folder(k, m, "8bit")

    def _folder(self, k, m, codec):
        # a short name (BUILDU~1) is replaced where it stands but is not the account name
        self.value(k, m.start(1), m.end(1), codec, "user", account=True, remember=b"~" not in m.group(1), login=True)

    def parasolid(self, k, raw):
        """Parasolid transmit header: USER, SITE and the computer name (MC in Solid Edge,
        MC_ID in NX). A value ends at ';' or, as Solid Edge writes USER, at the line end."""
        for p in _each(raw, b"**PART1;"):
            end = raw.find(b"**PART2", p, p + 4000)
            head = raw[p + 7:end if end > 0 else p + 2000]
            for m in _XT_FIELD.finditer(head):
                a, b = p + 7 + m.start(2), p + 7 + m.end(2)
                key, text = m.group(1), m.group(2).decode("latin-1").strip()
                if key == b"USER":
                    self.value(k, a, b, "8bit", "user", account=True, login=True)
                elif key == b"SITE":
                    self.value(k, a, b, "8bit", "org")
                elif _HOST.match(text) and not _PLATFORM.match(text):  # a computer name, not a platform
                    self.value(k, a, b, "8bit", "user")

    def hosts(self, k, raw):
        """Windows default computer names (DESKTOP-XXXXXXX)."""
        for lit, rx, codec in ((b"DESKTOP-", _HOST8, "ascii"), (b"LAPTOP-", _HOST8, "ascii"),
                               ("DESKTOP-".encode("utf-16le"), _HOST16, "utf-16le"),
                               ("LAPTOP-".encode("utf-16le"), _HOST16, "utf-16le")):
            for p in _each(raw, lit):
                m = rx.match(raw, p)
                if m:
                    self.value(k, m.start(), m.end(), codec, "user")

    def reports(self, k, raw):
        """Text reports stored with the model: "Created by: name"."""
        for lit in (b"Created by:", b"aved by:", b"odified by:"):
            for p in _each(raw, lit):
                m = _AFTER_BY.match(raw, p + len(lit))
                if m:
                    self.value(k, m.start(1), m.end(1), "8bit", "user", account=True, field=True, login=True)

    def solid_edge_history(self, k, raw):
        """Solid Edge GeometricVersionHistory: per save, two lengths, then the user and the
        computer name as NUL-terminated UTF-16."""
        for m in _GVH.finditer(raw):
            s = m.end()
            e1 = s + 2 * m.group(1)[0]
            e2 = e1 + 2 * m.group(2)[0]
            if e2 > len(raw) or raw[e1 - 2:e1] != b"\x00\x00" or raw[e2 - 2:e2] != b"\x00\x00":
                continue
            try:
                user, host = raw[s:e1 - 2].decode("utf-16le"), raw[e1:e2 - 2].decode("utf-16le")
            except UnicodeDecodeError:
                continue
            if not user.isprintable() or not host.isprintable() or nobody(user, True):
                continue
            self.value(k, s, e1 - 2, "utf-16le", "user", account=True, login=True)
            if _HOST.match(host):
                self.value(k, e1, e2 - 2, "utf-16le", "user")

    def workbook(self, k, raw):
        """Excel workbook of an embedded design table: the WRITEACCESS record (0x5C, 112
        bytes) holds the name of the user who saved it, padded with spaces."""
        for m in _BIFF_USER.finditer(raw):
            n, wide = m.group(1)[0], m.group(2)[0]
            a, end = m.end(), m.start() + 116
            b = a + n * (2 if wide else 1)
            if 1 < n and b <= end <= len(raw) and not raw[b:end].strip(b" \x00"):
                self.value(k, a, b, "utf-16le" if wide else "cp1252", "user", account=True, login=True)

    def nx(self, k, raw):
        """NX part data: the attribute block at the start has designer / engineer fields,
        and leftovers of longer values behind them. (The save history keeps user names as
        counted strings like every other string of the file; spread() replaces there the
        user that the Parasolid header names.)"""
        head = raw[:2048]
        for m in _NX_FIELD.finditer(head):
            if self.value(k, m.start(1), m.end(1), "8bit", "person", field=True):
                for word in re.findall(rb"[A-Za-z]{4,}", m.group(1)):   # what an older, longer value left behind
                    for p in _each(head, word):
                        self.put(k, p, p + len(word), self.pseudo.span(word.decode("ascii")).encode("ascii"))

    def read(self):
        for k, (name, raw, buf) in enumerate(self.parts):
            if len(raw) != len(buf) or len(raw) < 8:
                continue
            try:
                if raw[:4] == b"\xfe\xff\x00\x00" or name[:1] == "\x05":
                    self.props(k, raw)
                if name.endswith(".xml") or name.startswith("docProps/") or raw[:5] == b"<?xml":
                    self.xml(k, raw)
                if name == "GeometricVersionHistory":
                    self.solid_edge_history(k, raw)
                if name == "UG_PART":
                    self.nx(k, raw)
                if name in ("Workbook", "Book"):
                    self.workbook(k, raw)
                self.savers(k, raw)
                self.folders(k, raw)
                self.parasolid(k, raw)
                if len(raw) < (1 << 20):
                    self.hosts(k, raw)
                if "OleItems" in name or len(raw) < (1 << 16):
                    self.reports(k, raw)
            except (struct.error, IndexError, ValueError):
                continue

    # -- every name, everywhere ------------------------------------------------------
    def searchable(self, name):
        """A name read in one place is looked for in the rest of the file unless it is an
        everyday word, a number of fewer than five digits, or too short to tell apart."""
        low = name.lower()
        if len(set(low) - set(" .-_")) < 2:
            return False
        if name.isascii():
            everyday = getattr(self.ctx.mapper, "everyday", None) or ()
            if low in everyday or low in STOP or (low.isdigit() and len(low) < 5):
                return False
        return True

    def forms(self, name):
        """(bytes to look for, bytes of framing in front, codec) for one name, in the ways
        it is usually written: as read, lower, UPPER, Capitalized."""
        variants = {name, name.lower(), name.upper(), name.title(), name.capitalize()}
        out = []
        for v in variants:
            wide = v.encode("utf-16le", "surrogatepass")
            if 2 <= len(v) < 255:                       # the whole of an MFC string
                out.append((_CSTR + bytes([len(v)]) + wide, 4, "utf-16le"))
            if len(v) >= 3 or (not v.isascii() and len(v) >= 2):
                out.append((wide, 0, "utf-16le"))
            if v.isascii():
                if len(v) >= 3:
                    out.append((v.encode("ascii"), 0, "ascii"))
                continue
            seen = set()
            for codec in ("utf-8", self.codec, "gbk", "big5", "shift_jis", "cp949", "cp1251", "cp1252", "cp1250"):
                try:
                    packed = v.encode(codec) if codec else b""
                except (UnicodeEncodeError, LookupError):
                    continue
                if len(packed) >= 4 and packed not in seen:
                    seen.add(packed)
                    out.append((packed, 0, codec))
        return out

    def spread(self):
        """Replace every remembered name wherever it stands in any part."""
        auto, some = ahocorasick.Automaton(), False
        for name in sorted(self.names, key=len, reverse=True):
            free = self.searchable(name)
            for packed, frame, codec in self.forms(name):
                if not frame and not free:
                    continue  # an everyday word or a short number: only as a whole stored string
                key = packed.decode("latin-1")
                if key not in auto:
                    auto.add_word(key, (len(packed) - frame, frame, codec, name.lower(),
                                        len(name) < 3 or (not free and (len(name) < 6 or name.isdigit()))))
                    some = True
        if not some:
            return
        auto.make_automaton()
        for k, (part, raw, buf) in enumerate(self.parts):
            if len(raw) != len(buf) or len(raw) < 6:
                continue
            # 1: the part is text (XML), no chance bytes; 2: NX part data, names stand before two spaces
            loose = 2 if part == "UG_PART" else int(part.endswith(".xml") or part.startswith(("docProps/", "swXmlContents/"))
                                                    or b"<?xml" in raw[:8])
            texts = self.texts.get(k, ())
            hits = []
            for end, (n, frame, codec, name_low, strict) in auto.iter(raw.decode("latin-1")):
                e = end + 1
                s = e - n
                try:
                    text = raw[s:e].decode(codec)
                except UnicodeDecodeError:
                    continue
                if text.lower() != name_low:
                    continue
                easy = loose or int(any(lo <= s and e <= hi for lo, hi in texts))  # inside a property value
                if not self._stands_alone(raw, s, e, text, frame, codec, strict, easy):
                    continue
                try:
                    hits.append((e - s, s, pseudonym(text, self.pseudo).encode(codec)))
                except UnicodeEncodeError:
                    continue
            for n, s, new in sorted(hits):  # where two names overlap, the longer one is written last
                self.put(k, s, s + n, new)

    @staticmethod
    def _stands_alone(raw, s, e, text, frame, codec, strict, loose):
        """Is the match at raw[s:e] the name itself, not part of a longer word or chance bytes?"""
        if frame:  # a whole MFC string: the length byte in front says where it ends
            if s < 5 or raw[s - 4:s - 1] != _CSTR or raw[s - 1] != len(text):
                return False
            return not strict or raw[s - 5:s - 4] in (b"\x80", b"c")   # ... right where an object starts
        wide = codec == "utf-16le"
        step = 2 if wide else 1
        before, after = raw[max(0, s - step):s], raw[e:e + step]
        if text.isdigit() and len(text) < 7 and not (loose == 2 and not wide):
            # a short number: only a whole path component or quoted value
            return all((len(x) < step or (x[0] in _STRICT_EDGE and (not wide or x[1] == 0))) for x in (before, after))
        for edge, near in ((text[0], before), (text[-1], after)):
            if len(near) < step or not edge.isalnum():
                continue
            other = near.decode("utf-16le", "replace") if wide else near.decode("latin-1")
            if not wide and not other.isascii():
                continue  # half of a character of some other encoding
            # a letter next to the name makes it part of a longer word; so does a digit next to a digit
            if other.isascii():
                if other.isalpha() or (other.isdigit() and edge.isdigit()):
                    return False
            elif not text.isascii() and _pool(edge)[1] and other.isalpha():
                return False
        if wide:
            return True
        if text.isascii():
            if loose == 2 and (raw[e:e + 2] == b"  " or (raw[e:e + 1] == b"\x00" and (
                    raw[s - 1] in (e - s + 1, e - s + 2) or anchored(raw, s, e)))):
                return True  # NX: a counted string, or a name before two spaces
            if not loose and e - s < 7 and not anchored(raw, s, e) and (e - s < 4 or not texty(raw, s, e)):
                return False  # a short name with no text around it: chance bytes
            lo = max(0, s - 40)
            return not in_base64(raw[lo:e + 40].decode("latin-1").lower(), s - lo, e - lo)
        return e - s >= 6 or anchored(raw, s, e)

    # -- containers inside a part -----------------------------------------------------
    def find_inside(self):
        """Containers stored inside the parts: {part: [(kind, start, end, extra)]}. Their
        bytes are not text of this part (an inner stream is cut into sectors, an inner chunk
        is compressed), so the readers above leave them alone; nested() treats them."""
        self.inside = {}
        if self.depth >= MAX_DEPTH:
            return
        for k, (name, raw, buf) in enumerate(self.parts):
            if len(raw) != len(buf) or len(raw) < 64:
                continue
            found = []
            try:
                mark = raw.find(_SW_MARK, 0, 1024)
                if mark >= 4 and raw[mark - 4:mark] != b"PK\x03\x04":
                    chunks = _sw_chunks(raw)
                    if len(chunks) >= 3:
                        found.append(("sw", 0, len(raw), chunks))
                if not found:
                    found = self._inner_ole(raw) + self._inner_zip(raw)
                    found.sort(key=lambda f: f[1])
                    found = [f for i, f in enumerate(found) if not any(g[1] <= f[1] < g[2] for g in found[:i])]
                    # (a property set keeps its thumbnail in such a block, a PNG its pixels: not those)
                    if ZLIB_BLOCKS and b"<?xml" not in raw[:8] and name[:1] != "\x05" and raw[:4] != b"\xfe\xff\x00\x00" \
                            and not any(m in raw for m in (b"\x89PNG\r\n", b"IDAT")):
                        blocks = [z for z in _zlib_blocks(raw) if not any(g[1] < z[1] and z[0] < g[2] for g in found)]
                        if blocks and not (len(blocks) == 1 and blocks[0][0] < 17 and blocks[0][1] == len(raw)):
                            found.append(("zlib", blocks[0][0], blocks[-1][1], blocks))
            except Exception:
                continue
            if found:
                self.inside[k] = found
                self.masks[k] = [(lo, hi) for kind, lo, hi, _x in found if kind != "zlib"] + \
                    [(lo, hi) for kind, _lo, _hi, blocks in found if kind == "zlib" for lo, hi, _data in blocks]

    @staticmethod
    def _inner_ole(raw):
        import olefile
        found, p = [], raw.find(_OLE_MAGIC)
        while p >= 0 and len(found) < 16:
            end = 0
            try:
                ole = olefile.OleFileIO(io.BytesIO(raw[p:]))
                used = max((i for i in range(len(ole.fat)) if ole.fat[i] != 0xFFFFFFFF), default=-1)
                end = min(len(raw), p + (used + 2) * ole.sectorsize)
            except Exception:
                pass
            if end > p + 512:
                found.append(("ole", p, end, None))
                p = raw.find(_OLE_MAGIC, end)
            else:
                p = raw.find(_OLE_MAGIC, p + 8)
        return found

    @staticmethod
    def _inner_zip(raw):
        found, p = [], raw.find(b"PK\x03\x04")
        while p >= 0 and len(found) < 8:
            end = raw.find(b"PK\x05\x06", p)
            if end < 0 or end + 22 > len(raw):
                break
            end += 22 + struct.unpack_from("<H", raw, end + 20)[0]
            found.append(("zip", p, min(end, len(raw)), None))
            p = raw.find(b"PK\x03\x04", end)
        return found

    def nested(self):
        for k, found in self.inside.items():
            for kind, a, b, extra in found:
                buf = self.parts[k][2]
                before = bytes(buf[a:b])
                try:
                    self.count += getattr(self, "_" + kind)(k, a, b, extra)
                except Exception:
                    buf[a:b] = before  # unreadable after all: as the earlier passes left it

    def inner(self, sub):
        """The whole treatment for the parts of a container found inside a part: the
        pipeline's passes, then this module's (once, whichever way it is wired). Returns
        how many of them changed."""
        if not sub:
            return 0
        calls = _CALLS[0]
        _STACK.append(self)
        try:
            try:
                from . import formats
                formats._rewrite_parts(sub, self.ctx)
            except Exception:
                pass
            if _CALLS[0] == calls:  # fix_parts is not called from _rewrite_parts: do it here
                _fix(sub, self.ctx)
        finally:
            _STACK.pop()
        return sum(1 for _n, old, now in sub if old != now)

    def _ole(self, k, a, b, _extra):
        """An OLE compound file stored in a part (virtual components, embedded objects,
        third-party stores). Its streams are treated one by one, from their original
        content, and written back inside the image; its directory and its pictures are put
        back exactly as they were."""
        import olefile
        _name, raw, buf = self.parts[k]
        old = olefile.OleFileIO(io.BytesIO(raw[a:b]))
        image = bytearray(buf[a:b])
        size, sect = old.sectorsize, old.first_dir_sector
        for _ in range(4096):  # an earlier pass may have changed a stream name: restore the directory
            if not 0 <= sect < len(old.fat):
                break
            lo = (sect + 1) * size
            image[lo:lo + size] = raw[a + lo:a + lo + size]
            sect = old.fat[sect]
        image = io.BytesIO(bytes(image))
        new = olefile.OleFileIO(image, write_mode=True)
        todo, sub = [], []
        for entry in old.listdir(streams=True, storages=False):
            try:
                data = old.openstream(entry).read()
            except Exception:
                continue
            packed = None if len(data) < 8 or _is_picture(entry[-1], data) else _zlib_tail(data)
            content = packed[1] if packed else data
            if len(data) >= 8 and not _is_picture(entry[-1], data):
                sub.append([entry[-1], content, bytearray(content)])
                todo.append((entry, data, packed[0] if packed else None, sub[-1]))
            else:
                todo.append((entry, data, None, None))
        self.inner(sub)
        done = 0
        for entry, data, offset, part in todo:
            want = data
            if part is not None and part[1] != part[2]:
                if offset is None:
                    want = bytes(part[2])
                else:
                    fitted = deflate_fit(bytes(part[2]), len(data) - offset, raw=False)
                    want = data[:offset] + fitted if fitted else data
                done += want != data
            if new.openstream(entry).read() != want:
                new.write_stream(entry, want)
        out = image.getvalue()[:b - a]  # olefile pads a cut-off last sector when it writes it
        if len(out) != b - a:
            return 0
        buf[a:b] = out
        return done

    def _sw(self, k, a, b, chunks):
        """A newer SolidWorks file stored in a part (virtual and imported components): each
        chunk is treated from its original content and deflated back into its space."""
        _name, raw, buf = self.parts[k]
        image = bytearray(buf)
        directory = _directory(raw)
        sub, keep = [], []
        for pos, start, csize, name, data in chunks:
            image[pos:start + csize] = raw[pos:start + csize]
            if len(data) >= 8 and not _is_picture(name, data):
                sub.append([name, data, bytearray(data)])
                keep.append((pos, start, csize))
        self.inner(sub)
        done = 0
        for (pos, start, csize), (_n, data, now) in zip(keep, sub):
            if data == now:
                continue
            now = bytes(now)
            fitted = deflate_fit(now, csize)
            if fitted is None:
                continue
            image[start:start + csize] = fitted
            _set_crc(image, pos, zlib.crc32(now), directory)
            done += 1
        buf[:] = image
        return done

    def _zip(self, k, a, b, _extra):
        """A ZIP archive stored in a part (an embedded workbook or document): its text
        members are treated from their original content and deflated back into exactly the
        space they had; the checksums in the local header, the directory and the data
        descriptor follow."""
        _name, raw, buf = self.parts[k]
        end = raw.rfind(b"PK\x05\x06", a, b)
        if end < 0:
            return 0
        count, cd_size, cd_off = struct.unpack_from("<HII", raw, end + 10)
        q = end - cd_size
        base = q - cd_off          # where the archive starts inside the part
        if q < a or base < 0 or count > 5000:
            return 0
        image = bytearray(buf)
        image[q:b] = raw[q:b]
        members, sub = [], []
        for _ in range(count):
            if raw[q:q + 4] != b"PK\x01\x02":
                break
            flags, method = struct.unpack_from("<HH", raw, q + 8)
            csize, usize, nlen, elen, clen = struct.unpack_from("<IIHHH", raw, q + 20)
            local = base + struct.unpack_from("<I", raw, q + 42)[0]
            name = raw[q + 46:q + 46 + nlen].decode("utf-8", "replace")
            entry, q = q, q + 46 + nlen + elen + clen
            if csize == 0xFFFFFFFF or raw[local:local + 4] != b"PK\x03\x04":
                continue
            start = local + 30 + sum(struct.unpack_from("<HH", raw, local + 26))
            if start + csize > b:
                continue
            image[local:start + csize] = raw[local:start + csize]
            if flags & 1 or method not in (0, 8) or not 8 <= usize <= (64 << 20) or not name.lower().endswith(_ZIP_TEXT):
                continue
            data = raw[start:start + csize]
            if method == 8:
                try:
                    data = zlib.decompressobj(-15).decompress(data)
                except zlib.error:
                    continue
            if len(data) != usize:
                continue
            members.append((entry, local, start, csize, method, flags))
            sub.append([name, data, bytearray(data)])
        self.inner(sub)
        done = 0
        for (entry, local, start, csize, method, flags), (_n, data, now) in zip(members, sub):
            if data == now:
                continue
            now = bytes(now)
            packed = now if method == 0 else deflate_fit(now, csize, pad=False)
            if packed is None:
                continue
            old, crc = bytes(image[entry + 16:entry + 20]), struct.pack("<I", zlib.crc32(now))
            image[start:start + csize] = packed
            image[entry + 16:entry + 20] = crc
            if image[local + 14:local + 18] == old:
                image[local + 14:local + 18] = crc
            if flags & 8:      # the checksum is repeated after the data
                d = start + csize + (4 if image[start + csize:start + csize + 4] == b"PK\x07\x08" else 0)
                if image[d:d + 4] == old:
                    image[d:d + 4] = crc
            done += 1
        buf[a:b] = image[a:b]
        return done

    def _zlib(self, k, a, b, blocks):
        """zlib streams inside a part (SolidWorks model data, Inventor segments, Solid Edge
        clusters store their data in such blocks): each is inflated, treated, and deflated
        back to exactly its size; a block that would grow stays as it was."""
        name, raw, buf = self.parts[k]
        sub = [[name, data, bytearray(data)] for _lo, _hi, data in blocks]
        self.inner(sub)
        done = 0
        for (lo, hi, _data), (_n, data, now) in zip(blocks, sub):
            packed = deflate_fit(bytes(now), hi - lo, raw=False, pad=False) if data != now else None
            buf[lo:hi] = packed or raw[lo:hi]
            done += packed is not None
        return done

    def report(self):
        """Hand the names this run read to the pipeline's log (per-file propagation and the
        global identifier list are built from it)."""
        found = self.ctx.binary.found
        for name, kind in self.fresh.items():
            if self.searchable(name):
                found.append((kind, name))


def _zlib_tail(data):
    """(offset, inflated bytes) when data is a short header followed by one zlib stream
    that runs to its end; else None. Same rule as formats._zlib_tail."""
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


# -- newer SolidWorks container ------------------------------------------------------

def _sw_chunks(data):
    """[(header position, data start, compressed size, name, inflated bytes)] of a newer
    SolidWorks file image. A chunk header is a ZIP local header without its signature."""
    out, pos = [], data.find(_SW_MARK)
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
                    out.append((pos, start, csize, name, raw))
        pos = data.find(_SW_MARK, nxt)
    return out


def _directory(data):
    """{chunk header position: [positions of its directory entries]}. A directory entry has
    the ZIP central-directory layout around the same mark (at q): CRC-32 at q+10, the sizes
    at q+14, and at q+36 the chunk's header position minus 12."""
    out = {}
    q = data.find(_SW_MARK)
    while q >= 0:
        if q + 40 <= len(data):
            pos = struct.unpack_from("<I", data, q + 36)[0] + 12
            if pos != q and data[pos:pos + 6] == _SW_MARK and data[q + 14:q + 22] == data[pos + 14:pos + 22]:
                out.setdefault(pos, []).append(q)
        q = data.find(_SW_MARK, q + 6)
    return out


def _set_crc(data, pos, crc, directory):
    """Store the CRC-32 of a chunk's inflated data in its header (mark at pos) and in its
    directory entries."""
    old = data[pos + 10:pos + 14]
    new = struct.pack("<I", crc)
    if old == new:
        return 0
    data[pos + 10:pos + 14] = new
    for q in directory.get(pos, ()):
        if data[q + 10:q + 14] == old:
            data[q + 10:q + 14] = new
    return 1


_ZLIB_HEAD = re.compile(rb"\x78[\x01\x5e\x9c\xda]")


def _zlib_blocks(raw, limit=4000):
    """[(start, end, inflated bytes)] of the zlib streams inside raw."""
    out, view, p = [], memoryview(raw), 0
    for m in _ZLIB_HEAD.finditer(raw):
        if m.start() < p:
            continue
        d = zlib.decompressobj()
        try:
            data = d.decompress(view[m.start():])
        except zlib.error:
            continue
        if d.eof and len(data) >= 64 and not data[:4].startswith(_IMAGE_MAGIC) and data[:4] != b"\x28\x00\x00\x00":
            p = len(raw) - len(d.unused_data)
            out.append((m.start(), p, data))
            if len(out) >= limit:
                break
    return out


# -- deflate into an exact size --------------------------------------------------------

# Empty deflate blocks, none of them final, that end on a byte boundary: put in front of a
# finished stream they make it 5, 6, 7 or 9 bytes longer without adding any output.
_LEAD_IN = {5: b"\x00\x00\x00\xff\xff", 6: b"\x02\x00\x00\x00\xff\xff", 7: b"\x02\x08\x00\x00\x00\xff\xff",
            9: b"\x02\x08\x20\x00\x00\x00\x00\xff\xff"}
_FILL = {2: b"\x03\x00", 3: b"\x02\x0c\x00", 4: b"\x02\x08\x30\x00", 5: b"\x01\x00\x00\xff\xff",
         6: b"\x02\x04\x00\x00\xff\xff"}  # empty blocks that end a stream, the last one final
_SEVEN = []   # the 7-Zip program, looked up once


def _lead_in(k):
    """k bytes of empty blocks to put in front of a finished deflate stream, or None for
    the sizes that cannot be made (1 to 4 and 8)."""
    if k == 0:
        return b""
    if k < 5 or k == 8:
        return None
    out = b""
    while k >= 14:
        out, k = out + _LEAD_IN[5], k - 5
    for part in {5: (5,), 6: (6,), 7: (7,), 9: (9,), 10: (5, 5), 11: (5, 6), 12: (5, 7), 13: (6, 7)}[k]:
        out += _LEAD_IN[part]
    return out


def _wrap(core, data, raw):
    return core if raw else b"\x78\xda" + core + struct.pack(">I", zlib.adler32(data) & 0xFFFFFFFF)


def deflate_exact(data, room, raw=True):
    """data as a deflate stream of exactly room bytes (raw, or zlib-wrapped), or None if zlib
    cannot make it that small. The spare space is taken by empty blocks inside the stream,
    so a reader that expects the stream to fill its space finds it does."""
    space = room - (0 if raw else 6)
    for level, strategy, memory in ((6, zlib.Z_DEFAULT_STRATEGY, 9), (9, zlib.Z_DEFAULT_STRATEGY, 9),
                                    (9, zlib.Z_FILTERED, 9), (9, zlib.Z_DEFAULT_STRATEGY, 8), (6, zlib.Z_DEFAULT_STRATEGY, 8)):
        c = zlib.compressobj(level, zlib.DEFLATED, -15, memory, strategy)
        whole = c.compress(data) + c.flush()
        if len(whole) > space:
            continue
        lead = _lead_in(space - len(whole))
        if lead is not None:
            return _wrap(lead + whole, data, raw)
        c = zlib.compressobj(level, zlib.DEFLATED, -15, memory, strategy)
        body = c.compress(data) + c.flush(zlib.Z_SYNC_FLUSH)   # ends on a byte, no final block yet
        spare = space - len(body)
        if spare >= 2:
            fill = (spare - 2) // 5
            return _wrap(body + b"\x00\x00\x00\xff\xff" * fill + _FILL[spare - 5 * fill], data, raw)
    return None


def _seven_zip(data, passes=15, word=258):
    """data as a raw deflate stream from 7-Zip's encoder (a few per cent smaller than zlib
    level 9), or None when the program is not installed or fails."""
    if not _SEVEN:
        _SEVEN.append(shutil.which("7z") or shutil.which("7za") or shutil.which("7zz") or "")
    if not _SEVEN[0] or not 0 < len(data) <= (256 << 20):
        return None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src, arc = os.path.join(tmp, "b"), os.path.join(tmp, "b.zip")
            with open(src, "wb") as f:
                f.write(data)
            done = subprocess.run([_SEVEN[0], "a", "-tzip", "-mm=Deflate", "-mx=9", "-mfb=%d" % word, "-mpass=%d" % passes,
                                   "-bd", "-y", arc, src], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
            if done.returncode != 0:
                return None
            with open(arc, "rb") as f:
                z = f.read()
        if z[:4] != b"PK\x03\x04" or struct.unpack_from("<H", z, 8)[0] != 8:
            return None
        start = 30 + sum(struct.unpack_from("<HH", z, 26))
        core = z[start:start + struct.unpack_from("<I", z, 18)[0]]
        d = zlib.decompressobj(-15)
        return core if d.decompress(core) == data and d.eof and not d.unused_data else None
    except (OSError, subprocess.SubprocessError, struct.error, zlib.error):
        return None


def deflate_fit(data, room, raw=True, pad=True):
    """data as a deflate stream that fills room bytes, for a block that is written back
    in the space it had. zlib first (deflate_exact). When zlib needs more than room:
    7-Zip's deflate encoder, if the program is installed, with empty blocks in front so
    that the stream is exactly room bytes long; if that size cannot be made and pad is
    set, zero bytes follow the stream instead (what formats._deflate_fit does). None if
    nothing fits."""
    out = deflate_exact(data, room, raw)
    if out is not None:
        return out
    space, shortest = room - (0 if raw else 6), None
    for passes, word in ((15, 258), (10, 128), (5, 64)):
        core = _seven_zip(data, passes, word)
        if core is None or len(core) > space:
            break
        lead = _lead_in(space - len(core))
        if lead is not None:
            return _wrap(lead + core, data, raw)
        shortest = shortest or core
    if pad and shortest is not None:
        out = _wrap(shortest, data, raw)
        return out + b"\x00" * (room - len(out))
    return None


def deflate_small(data, raw=True):
    """The smallest deflate stream for data that zlib or 7-Zip can make (no target size)."""
    best = None
    for level in (9, 6):
        c = zlib.compressobj(level, zlib.DEFLATED, -15, 9)
        whole = c.compress(data) + c.flush()
        if best is None or len(whole) < len(best):
            best = whole
    core = _seven_zip(data)
    if core is not None and len(core) < len(best):
        best = core
    return _wrap(best, data, raw)


# -- entry points ------------------------------------------------------------------------

def _fix(parts, ctx):
    run = _Run(parts, ctx, _STACK[-1] if _STACK else None)
    if not run.depth:
        _LAST.clear()
    run.find_inside()
    run.read()
    run.spread()
    run.nested()
    run.report()
    _LAST.update(run.names)
    return run.count


def fix_parts(parts, ctx):
    """Replace account and identity residue in the parts of one OLE or newer SolidWorks
    file, in place and at the same length. Returns the number of replacements."""
    _CALLS[0] += 1
    try:
        return _fix(parts, ctx)
    except Exception:
        return 0


# -- what an OLE file keeps outside its streams ---------------------------------------------

def slack_ranges(image):
    """[(start, end)] of the bytes of an OLE file image that belong to no live stream and to
    no table: free sectors, the unused end of the last sector of each stream, free mini
    sectors, chains that no directory entry points to. Old copies of streams (and of the
    names in them) stay there when an application saves in place."""
    import olefile
    ole = olefile.OleFileIO(io.BytesIO(bytes(image)))
    try:
        size, fat = ole.sectorsize, ole.fat
        count = min(len(fat), max(0, (len(image) + size - 1) // size - 1))
        used, out = bytearray(count), []

        def chain(first, table, limit):
            seen = 0
            while 0 <= first < limit and seen <= limit:
                yield first
                first, seen = table[first], seen + 1

        for i in range(count):
            if fat[i] in (0xFFFFFFFD, 0xFFFFFFFC):   # the allocation tables themselves
                used[i] = 1
        for first in (ole.first_dir_sector, ole.first_mini_fat_sector, ole.first_difat_sector):
            for i in chain(first, fat, count):
                used[i] = 1
        root = ole.root
        minis = list(chain(root.isectStart, fat, count)) if root is not None and root.size else []
        for i in minis:
            used[i] = 1
        small, cutoff = ole.minisectorsize, ole.minisectorcutoff
        mini_used = bytearray(len(minis) * size // small)
        try:
            ole.loadminifat()
            minifat = ole.minifat
        except Exception:
            minifat = []
        for entry in ole.listdir(streams=True, storages=False):
            item = ole.direntries[ole._find(entry)]
            left = item.size
            if left >= cutoff:
                for i in chain(item.isectStart, fat, count):
                    used[i] = 1
                    if left < size:
                        out.append(((i + 1) * size + left, (i + 2) * size))
                    left = max(0, left - size)
            else:
                for m in chain(item.isectStart, minifat, min(len(minifat), len(mini_used))):
                    mini_used[m] = 1
                    at = (minis[m * small // size] + 1) * size + m * small % size
                    if left < small:
                        out.append((at + left, at + small))
                    left = max(0, left - small)
        out += [((i + 1) * size, (i + 2) * size) for i in range(count) if not used[i]]
        for m in range(len(mini_used)):
            if not mini_used[m]:
                at = (minis[m * small // size] + 1) * size + m * small % size
                out.append((at, at + small))
    finally:
        ole.close()
    merged = []
    for lo, hi in sorted((lo, min(hi, len(image))) for lo, hi in out if lo < min(hi, len(image))):
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]


def fix_slack(image, ctx, matcher=None):
    """For formats.ole_file, after the streams are written: image is the whole OLE file
    as a bytearray. Replaces the file's names in the bytes that belong to no live stream
    (slack_ranges): those of matcher (the file's own names as formats collects them) and
    those fix_parts read from the same file. No live stream and no table can change.
    Returns the number of replacements; never raises."""
    try:
        ranges = slack_ranges(image)
        parts, total = [], 0
        for lo, hi in ranges:
            block = bytearray(image[lo:hi])
            if matcher is not None:
                total += replace_names(block, matcher, ctx.binary.pseudo)
            parts.append(["", bytes(image[lo:hi]), block])
        run = _Run(parts, ctx)
        run.names = dict(_LAST)
        run.spread()
        for (lo, hi), (_name, _raw, block) in zip(ranges, parts):
            image[lo:hi] = block
        return total + run.count
    except Exception:
        return 0


def lock_file(data, ctx):
    """SolidWorks owner file ("~$name.SLDPRT", written while a document is open): the whole
    file is the user's name, NUL-terminated, in 8-bit text or UTF-16. data is a bytearray;
    returns 1 if the name was replaced."""
    try:
        if not 2 <= len(data) <= 512:
            return 0
        wide = len(data) >= 4 and data[1] == 0 and data[3] == 0
        for codec in (("utf-16le",) if wide else ("utf-8", "gbk", "latin-1")):
            try:
                text = bytes(data).decode(codec)
                break
            except UnicodeDecodeError:
                continue
        else:
            return 0
        name = text.split("\x00")[0]
        if nobody(name, True) or not name.isprintable():
            return 0
        new = (pseudonym(name, ctx.binary.pseudo) + text[len(name):]).encode(codec)
        if len(new) != len(data) or new == bytes(data):
            return 0
        data[:] = new
        ctx.binary.found.append(("user", name))
        return 1
    except Exception:
        return 0
