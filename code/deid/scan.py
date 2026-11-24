"""Find identifiers in bytes and replace each with a same-length pseudonym, in place."""
import re

import ahocorasick

from .identities import (GENERIC_USERS, GENERIC_VALUES, PLACEHOLDER, PUBLIC_MAIL, SOFTWARE_HINTS,
                         VENDOR_DOMAINS)

OVERLAP = 4096  # no rule looks further than this past the start of its match
_B64 = re.compile(r"[a-z0-9+/=\s]*")
_USER_WIN = re.compile(r"[\\/]*([^\\/:*?\"<>|\r\n\x00';,)]{2,40})")
_USER_NIX = re.compile(r"([a-z_][a-z0-9_.-]{1,31})/")
_LOCAL = re.compile(r"[a-z0-9._%+-]{2,40}$")
_DOMAIN = re.compile(
    r"@((?:[a-z0-9-]{1,30}\.){1,4})(com|org|net|edu|gov|io|de|fr|uk|it|ch|es|nl|se|no|dk|fi|pl|cz|at|be|"
    r"jp|cn|in|ru|br|au|ca|us|eu|info|me|co|ac|tw|kr|tech|dev|si|pt|gr|hu|ie|nz|za|mx|ar|cl|tr|il|sg|hk)"
    r"(?![a-z0-9])")
_STEP_STR = re.compile(rb"'((?:[^'\r\n]|'')*)'")
_STEP_HDR_STR = re.compile(rb"'((?:[^']|'')*)'")
_XML_VALUE = re.compile(rb"<(?:dc:title|rdf:li)(?:\s[^>]*)?>([^<]{1,200})</")
_DXF_VALUE = re.compile(rb"\r?\n[ \t]*1\r?\n([^\r\n]{1,120})")
_FILE_NAME = re.compile(rb"FILE_NAME\s*\(")
_STEP_DATA = re.compile(r"endsec\s*;\s*data\s*;")
_XT_VALUE = re.compile(rb"((?:[^;\r\n]|\r?\n){2,60});")
_TAG_VALUE = re.compile(rb"([^<>\r\n]{2,120})</")
_PROP_VALUE = re.compile(rb"[^>]{0,200}>\s*<vt:lpw?str>([^<>]{2,200})</vt:lpw?str>")
_SAT_FIELD = re.compile(r"@(\d+) ")
_VTI_VALUE = re.compile(rb"(?:[^\\\r\n]{0,40}\\+)?([^\\\r\n]{2,60})")
_FC_VALUE = re.compile(rb"[^>]{0,120}>\s*<String value=\"([^\"]{1,200})\"")
_3MF_VALUE = re.compile(rb"[^>]{0,120}>([^<]{1,200})<")
_USER_WIDE = re.compile(rb"(?:[\\/]\x00)*((?:[^\\/:*?\"<>|';,)\x00-\x1f]\x00){2,40})")
_PRINTABLE = re.compile(rb"[\x20-\x7e]")


_URL = re.compile(r"(?i)(?:https?://|www\.)([a-z0-9-]+(?:\.[a-z0-9-]+)+)((?:/[^\s/]+){0,2})")
_BARE_HOST = re.compile(r"(?i)(?<![\w@./-])([a-z0-9][a-z0-9-]{2,40})\.(com|org|net|edu|gov|de|fr|it|ch|eu|io|nl|es|at)(?![\w-])")
_PHONE = re.compile(r"(?i)\b(tel|phone|fax|mobile|ph|t\u00e9l|telefon|t\u00e9l\u00e9phone)\b[.:]?\s*(\+?\d[\d ().\-/]{6,18}\d)")
KEEP_HOSTS = set("""w3 iso wikipedia youtube google adobe autodesk solidworks microsoft
creativecommons gnu doi purl inkscape freecad freecadweb opencascade openscad apache mozilla
python schemas xmlsoft sourceforge asme din ansi ieee nist-std""".split())


# an initial glued to a surname: "J.Smith", "BY A.DOE" (drafter bylines in title blocks)
_INITIAL_NAME = re.compile(r"(?<![\w.])([A-Z]\.)([A-Z][a-z]{2,}|[A-Z]{3,})(?![\w.])")

# a name after academic titles: "Prof. Dr. -Ing. J. Smith", "Prof. Dr. sc. ETH Jane Doe"
_TITLED = re.compile(r"\b(?:Prof\.?|Dr\.|Dipl\.-Ing\.)(?:[ \t]*(?:Prof\.?|Dr\.|-[ \t]?Ing\.|Ing\.|sc\.|techn\.|rer\.|nat\.|"
                     r"habil\.|ETH|Dipl\.-Ing\.|Dipl\.))*[ \t]+(?:[A-Z]\.[ \t]*)*"
                     r"([A-ZÄÖÜ][a-zäöüéèáíóúñß]{2,}(?:-[A-Z][a-z]+)?)(?:[ \t]+([A-ZÄÖÜ][a-zäöüéèáíóúñß]{2,}))?")


def document_text_rules(text, pseudo):
    """Rules for prose a person reads (PDF page text): web hosts, the account/project
    part of code-hosting links, and telephone numbers after a label. Same length."""
    from .identities import HOSTING, TWO_LEVEL

    def host(m):
        labels = m.group(1).split(".")
        keep = 2 if len(labels) > 2 and (labels[-2].lower() in TWO_LEVEL and len(labels[-1]) == 2
                                         or ".".join(labels[-2:]).lower() in HOSTING) else 1
        site = labels[-keep - 1].lower() if len(labels) > keep else ""
        path = m.group(2)
        if site in ("github", "gitlab", "bitbucket"):
            path = pseudo.span(path)          # /<account>/<project>
        elif site not in KEEP_HOSTS:
            labels = [l if l.lower() == "www" else pseudo.span(l) for l in labels[:-keep]] + labels[-keep:]
        return m.group(0)[:m.start(1) - m.start(0)] + ".".join(labels) + path

    def bare(m):
        return m.group(0) if m.group(1).lower() in KEEP_HOSTS else pseudo.span(m.group(1)) + "." + m.group(2)

    def initial_name(m):
        from .pdfids import _english
        return m.group(0) if m.group(2).lower() in _english() else m.group(1) + pseudo.span(m.group(2))

    text = _URL.sub(host, text)
    text = _BARE_HOST.sub(bare, text)
    text = _INITIAL_NAME.sub(initial_name, text)

    def titled(m):
        from .pdfids import _english
        out, last = [], m.start(0)
        for g in (1, 2):
            if m.group(g) and m.group(g).lower() not in _english():
                out += [m.string[last:m.start(g)], pseudo.span(m.group(g))]
                last = m.end(g)
        return "".join(out) + m.string[last:m.end(0)]

    text = _TITLED.sub(titled, text)
    return _PHONE.sub(lambda m: m.group(0)[:m.start(2) - m.start(0)] + pseudo.span(m.group(2)), text)


def name_matcher(names):
    """One matcher for a file's own names, in 8-bit and UTF-16LE form; None if no names."""
    auto, any_word = ahocorasick.Automaton(), False
    for name in names:
        low = name.lower()
        plain = low.encode("latin-1", "ignore").decode("latin-1")
        if len(plain) >= 3:
            auto.add_word(plain, (len(plain), False))
            any_word = True
        if len(low) >= 3 and low.isascii():
            auto.add_word(low.encode("utf-16le").decode("latin-1"), (2 * len(low), True))
            any_word = True
    if not any_word:
        return None
    auto.make_automaton()
    return auto


def replace_names(buf, matcher, pseudo):
    """Replace every occurrence of a file's own names (8-bit and UTF-16LE, any case) in
    bytearray buf with their pseudonyms, in place. Returns the number replaced."""
    if matcher is None:
        return 0
    low = bytes(buf).decode("latin-1").lower()
    hits = sorted((end - n + 1, -n, wide_form) for end, (n, wide_form) in matcher.iter(low))
    done = last = 0
    for s, neg_n, wide_form in hits:
        if s < last:
            continue
        e, step = s - neg_n, 2 if wide_form else 1
        before, after = low[max(0, s - step):s], low[e:e + step]
        if before[:1].isalpha() and before[:1].isascii() and (not wide_form or before[1:2] == "\x00") and low[s].isalpha():
            continue
        if after[:1].isalpha() and after[:1].isascii() and (not wide_form or after[1:2] == "\x00") and low[e - step].isalpha():
            continue
        if not wide_form and e - s < 7 and not anchored(buf, s, e) and (e - s < 4 or not texty(buf, s, e)):
            continue  # a short name with no text around it: chance bytes
        if not wide_form and in_base64(low, s, e):
            continue
        old = bytes(buf[s:e]).decode("utf-16le" if wide_form else "latin-1")
        new = pseudo.span(old).encode("utf-16le" if wide_form else "latin-1")
        if len(new) == e - s and new != bytes(buf[s:e]):
            buf[s:e] = new
            done += 1
            last = e
    return done


def texty(buf, s, e):
    """A short 8-bit match in binary data counts only when it looks like text: written
    in one case style (lower, UPPER or Capitalized) and surrounded by printable
    characters (all 8 neighbours for 4 letters or fewer, 6 of 8 otherwise). Compressed
    streams spell short words by chance; this keeps them untouched."""
    word = bytes(buf[s:e])
    letters = bytes(c for c in word if 65 <= c <= 90 or 97 <= c <= 122)
    if letters and not (letters.islower() or letters.isupper() or (letters[:1].isupper() and letters[1:].islower())):
        return False
    around = len(_PRINTABLE.findall(buf, max(0, s - 4), s)) + len(_PRINTABLE.findall(buf, e, e + 4))
    return around >= (8 if e - s <= 4 else 6)


def anchored(buf, s, e):
    """A short 8-bit name counts when it is a whole stored field: NUL on both sides, or
    a 4-byte little-endian length just before it that equals its length (or length + 1)."""
    n = e - s
    if s >= 1 and buf[s - 1] == 0 and e < len(buf) and buf[e] == 0:
        return True
    if s >= 4 and buf[s - 3] == 0 and buf[s - 2] == 0 and buf[s - 1] == 0 and buf[s - 4] in (n, n + 1):
        return True
    if s >= 1 and e < len(buf) and buf[s - 1] in b">\"'" and buf[e] in b"<\"'":
        return True  # a whole XML element or attribute value
    return False


def in_base64(low, s, e):
    """True when low[s:e] sits inside encoded binary data: 40 characters on each side
    that are all base64 characters, with at most a few line or column breaks (prose
    would have a space every few letters)."""
    before, after = low[max(0, s - 40):s], low[e:e + 40]
    if len(before) < 40 or len(after) < 40:
        return False
    around = before + after
    return bool(_B64.fullmatch(around)) and sum(c.isspace() for c in around) <= 4


def wide(text):
    """The UTF-16LE bytes of an ASCII string, as a latin-1 str (for the automaton)."""
    return text.encode("utf-16le").decode("latin-1")


def is_generic(value):
    """True when a field value names nobody (blank, placeholder, or a software name)."""
    v = value.strip().lower()
    if len(v) < 2 or not any(c.isalpha() for c in v):
        return True
    if v in GENERIC_VALUES or PLACEHOLDER.match(v):
        return True
    return any(h in v for h in SOFTWARE_HINTS)


def step_strings(buf, i, end, wrapped):
    """Yield (argument index, start, end) of each string literal in a STEP argument list;
    i is just past the opening parenthesis. wrapped: strings may span lines (header)."""
    depth, arg = 1, 0
    rx = _STEP_HDR_STR if wrapped else _STEP_STR
    while i < end and depth > 0:
        c = buf[i]
        if c == 0x2F and buf[i + 1:i + 2] == b"*":  # comment
            j = buf.find(b"*/", i + 2, end)
            i = end if j < 0 else j + 2
            continue
        if c == 0x27:
            m = rx.match(buf, i)
            if not m:
                return
            yield arg, m.start(1), m.end(1)
            i = m.end()
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
        elif c == 0x2C and depth == 1:
            arg += 1
        i += 1


class Scanner:
    def __init__(self, pseudo, patterns, mode="text"):
        """patterns: (lowercase pattern, exact-case bytes or None).
        mode "path": dictionary only. "text": dictionary plus the structural rules (user
        folders in stored paths, e-mail, STEP/XML/DXF/Parasolid/FreeCAD identity fields).
        "binary": dictionary and user folders in 8-bit and UTF-16 text, XML creator
        fields; short 8-bit matches need text around them (random bytes can spell a word)."""
        self.pseudo = pseudo
        self.mode = mode
        self.found = []  # (kind, original value) from structural rules, for the private log
        self._last = 0
        self.step = False   # scanning a STEP file: dictionary words count only inside text
        self.sat = False    # scanning an ACIS SAT file: only inside "@<length> text" fields
        self._data_at = 0
        auto = ahocorasick.Automaton()
        for pat, exact in patterns:
            auto.add_word(pat, (len(pat), "tok", exact))
            if mode != "path" and pat.isascii():
                auto.add_word(wide(pat), (2 * len(pat), "tok16", None))
        if mode != "path":
            for anchor in ("\\users\\", "\\documents and settings\\"):
                auto.add_word(anchor, (len(anchor), "win", None))
                auto.add_word(wide(anchor), (2 * len(anchor), "win16", None))
            for anchor in ("/users/", "/home/"):
                auto.add_word(anchor, (len(anchor), "nix", None))
            for anchor in ("<dc:creator", "<dc:publisher", "<dc:rights", "<dc:contributor"):
                auto.add_word(anchor, (len(anchor), "xml", None))
            for anchor in ("lastmodifiedby>", "lastsavedby>"):
                auto.add_word(anchor, (len(anchor), "tagval", None))
            for hint in ("sw-last saved by", "sw-author", "drawnby", "drawn by", "engineer", "checkedby", "checked by",
                         "companyname", "company", "author", "designer", "approvedby", "approved by", "last saved by"):
                auto.add_word('name="' + hint, (len(hint) + 6, "propval", None))
            for anchor in (";user=", ";site=", "\nuser=", "\nsite=", ";mc=", "\nmc="):
                auto.add_word(anchor, (len(anchor), "xt", None))
        if mode == "text":
            for anchor in ('name="createdby"', 'name="lastmodifiedby"', 'name="company"'):
                auto.add_word(anchor, (len(anchor), "fc", None))
            auto.add_word('name="designer"', (15, "3mf", None))
            auto.add_word("@", (1, "mail", None))
            for anchor in ("person(", "person (", "organization(", "organization ("):
                auto.add_word(anchor, (len(anchor), "step", None))
            auto.add_word("$lastsavedby", (12, "dxf", None))
            for anchor in ("vti_author:sr|", "vti_modifiedby:sr|"):
                auto.add_word(anchor, (len(anchor), "vti", None))
        if len(auto):  # an empty dictionary (no identifiers, no anchors) has no automaton
            auto.make_automaton()
        self.auto = auto if len(auto) else None

    # -- helpers -----------------------------------------------------------------
    def _put(self, buf, start, end):
        old = bytes(buf[start:end]).decode("latin-1")
        new = self.pseudo.span(old)
        if new == old:
            return False
        buf[start:end] = new.encode("latin-1")
        return True

    def _value(self, buf, start, end, kind, escapes=False):
        """Pseudonymize an identity field value unless it is a placeholder. escapes: the
        format writes non-ASCII text with backslash escapes (STEP), which are left alone."""
        raw = bytes(buf[start:end]).decode("latin-1")
        if (escapes and "\\" in raw) or is_generic(raw):
            return False
        if self._put(buf, start, end):
            self.found.append((kind, raw))
            return True
        return False

    # -- rules -------------------------------------------------------------------
    def _tok(self, buf, low, s, n, exact):
        e = s + n
        if s > 0 and "a" <= low[s - 1] <= "z" and not (low[s - 1] == "h" and s > 1 and low[s - 2].isdigit()):
            return 0  # (an IGES Hollerith string "6Hname" does start a word)
        if e < len(low) and "a" <= low[e] <= "z":
            return 0
        if s > 0 and low[s - 1] in "&#" and low[e:e + 1] == ";":
            return 0  # an XML/HTML entity or character reference (&lt; &gt; &#xNN;)
        if low[s].isdigit() and s > 0 and (low[s - 1].isdigit() or low[s - 1] == "."):
            return 0  # inside a longer number
        if low[e - 1].isdigit() and e < len(low) and (low[e].isdigit() or (low[e] == "." and low[e + 1:e + 2].isdigit())):
            return 0
        if exact is not None and bytes(buf[s:e]) != exact:
            return 0
        if in_base64(low, s, e):
            return 0  # inside base64 data (an embedded image), not text
        if self.sat:
            # ACIS: entity names (pcurve, coedge, ...) and numbers are the model itself
            start = low.rfind("\n", 0, s) + 1
            inside = False
            for m in _SAT_FIELD.finditer(low, start, s):
                inside = m.end() <= s and e <= m.end() + int(m.group(1))
            if not inside:
                return 0
        if self.step and s >= self._data_at:
            # STEP data section: only quoted strings and comments are text; entity names,
            # enumerations and numbers are the model itself and are never touched
            # (a string may wrap over lines: count quotes from the end of the last statement)
            floor = max(self._data_at, s - 4000)
            stmt = max(low.rfind(";\n", floor, s), low.rfind(";\r", floor, s))
            line = low[(stmt + 1 if stmt >= 0 else low.rfind("\n", 0, s) + 1):s]
            if line.count("'") % 2 == 0 and line.rfind("/*") <= line.rfind("*/"):
                return 0
        if self.mode == "binary" and n < 7 and not texty(buf, s, e):
            return 0  # a short word with no text around it: chance bytes
        if not self._put(buf, s, e):
            return 0
        self.found.append(("tok", low[s:e]))
        return e

    def _win(self, buf, low, s, n, _):
        m = _USER_WIN.match(low, s + n)
        if not m:
            return 0
        name = m.group(1).strip()
        if name in GENERIC_USERS or len(name) < 2:
            return 0
        if self._put(buf, m.start(1), m.end(1)):
            self.found.append(("user", name))
            return m.end(1)
        return 0

    def _tok16(self, buf, low, s, n, _):
        e = s + n
        if s >= 2 and low[s - 1] == "\x00" and "a" <= low[s - 2] <= "z":
            return 0
        if e + 1 < len(low) and low[e + 1] == "\x00" and "a" <= low[e] <= "z":
            return 0
        old = bytes(buf[s:e]).decode("utf-16le")
        new = self.pseudo.span(old)
        if new == old:
            return 0
        buf[s:e] = new.encode("utf-16le")
        self.found.append(("tok", old.lower()))
        return e

    def _win16(self, buf, low, s, n, _):
        m = _USER_WIDE.match(buf, s + n)
        if not m:
            return 0
        old = bytes(m.group(1)).decode("utf-16le")
        if old.strip().lower() in GENERIC_USERS:
            return 0
        new = self.pseudo.span(old)
        if new == old:
            return 0
        buf[m.start(1):m.end(1)] = new.encode("utf-16le")
        self.found.append(("user", old.lower()))
        return m.end(1)

    def _fc(self, buf, low, s, n, _):
        """FreeCAD Document.xml: CreatedBy / LastModifiedBy / Company property values."""
        m = _FC_VALUE.match(buf, s + n)
        if m and self._value(buf, m.start(1), m.end(1), "org" if "company" in low[s:s + n] else "person"):
            return m.end(1)
        return 0

    def _3mf(self, buf, low, s, n, _):
        m = _3MF_VALUE.match(buf, s + n)
        if m and self._value(buf, m.start(1), m.end(1), "person"):
            return m.end(1)
        return 0

    def _nix(self, buf, low, s, n, _):
        if s > 0 and (low[s - 1].isalnum() or low[s - 1] in "._-"):
            return 0  # part of a URL or a relative path, not a home folder
        m = _USER_NIX.match(low, s + n)
        if not m or m.group(1) in GENERIC_USERS:
            return 0
        if self._put(buf, m.start(1), m.end(1)):
            self.found.append(("user", m.group(1)))
            return m.end(1)
        return 0

    def _mail(self, buf, low, s, n, _):
        local = _LOCAL.search(low, max(0, s - 40), s)
        dom = _DOMAIN.match(low, s)
        if not local or not dom:
            return 0
        labels = dom.group(1).rstrip(".").split(".")
        if labels[-1] in VENDOR_DOMAINS or s - local.start() < 3:
            return 0
        host = bytes(buf[s + 1:dom.end()]).decode("latin-1")
        if not (host.islower() or host.isupper()):
            return 0  # random case: encoded binary data, not an address
        self.found.append(("email", low[local.start():dom.end()]))
        if local.start() >= self._last:
            self._put(buf, local.start(), s)
        if labels[-1] not in PUBLIC_MAIL:
            self._put(buf, dom.start(1), dom.end(1))
        return dom.end()

    def _step(self, buf, low, s, n, _):
        if s > 0 and (low[s - 1].isalnum() or low[s - 1] == "_"):
            return 0
        # PERSON(id, last, first, middle, prefix titles, suffix titles): names only.
        # ORGANIZATION(id, name, description): leave the description.
        kind, names = ("person", (0, 1, 2, 3)) if low.startswith("person", s) else ("org", (0, 1))
        done, last = 0, 0
        for arg, a, b in step_strings(buf, s + n, min(len(buf), s + n + 1500), False):
            last = b
            if arg in names and self._value(buf, a, b, kind, True):
                done += 1
        return last if done else 0

    def _xml(self, buf, low, s, n, _):
        tag = low[s + 1:s + n]
        close = low.find("</" + tag, s, s + 1500)
        if close < 0:
            return 0
        done = 0
        for m in _XML_VALUE.finditer(buf, s, close):
            if self._value(buf, m.start(1), m.end(1), "person"):
                done += 1
        return close if done else 0

    def _dxf(self, buf, low, s, n, _):
        m = _DXF_VALUE.match(buf, s + n)
        if m and self._value(buf, m.start(1), m.end(1), "user"):
            return m.end(1)
        return 0

    def _xt(self, buf, low, s, n, _):
        """USER= field of a Parasolid text header."""
        m = _XT_VALUE.match(buf, s + n)
        if m and self._value(buf, m.start(1), m.end(1), "user"):
            return m.end(1)
        return 0

    def _tagval(self, buf, low, s, n, _):
        """<...lastModifiedBy>NAME</...> in document property XML."""
        m = _TAG_VALUE.match(buf, s + n)
        if m and self._value(buf, m.start(1), m.end(1), "user"):
            return m.end(1)
        return 0

    def _propval(self, buf, low, s, n, _):
        """<property ... name="Drawn By"><vt:lpwstr>NAME</vt:lpwstr> in custom property XML."""
        m = _PROP_VALUE.match(buf, s + n)
        if m and self._value(buf, m.start(1), m.end(1), "org" if "company" in low[s:s + n] else "person"):
            return m.end(1)
        return 0

    def _vti(self, buf, low, s, n, _):
        """Author / modified-by lines of a FrontPage _vti_cnf metadata file."""
        m = _VTI_VALUE.match(buf, s + n)
        if m and self._value(buf, m.start(1), m.end(1), "user"):
            return m.end(1)
        return 0

    def step_header(self, buf):
        """Author, organization and authorisation of a STEP FILE_NAME header.
        Returns the (start, end) spans it replaced."""
        m = _FILE_NAME.search(buf, 0, 20000)
        if not m:
            return []
        done = []
        for arg, a, b in step_strings(buf, m.end(), min(len(buf), m.end() + 6000), True):
            if arg in (2, 3, 6) and self._value(buf, a, b, "person" if arg != 3 else "org", True):
                done.append((a, b))
        return done

    # -- driver ------------------------------------------------------------------
    def scan(self, buf, zone_end=None, first=True):
        """Replace identifiers in bytearray buf whose match starts before zone_end.
        Returns the number of replacements. Length never changes."""
        zone_end = len(buf) if zone_end is None else zone_end
        low = bytes(buf).decode("latin-1").lower()
        if self.step:
            start = _STEP_DATA.search(low) if first else None
            self._data_at = start.end() if start else (len(low) if first else 0)
        hits = []
        for end, (n, kind, extra) in (self.auto.iter(low) if self.auto else ()):
            s = end - n + 1
            if s < zone_end:
                hits.append((s, -n, kind, extra))
        spans = self.step_header(buf) if first and self.mode == "text" else []
        count = len(spans)
        if not hits:
            return count
        hits.sort(key=lambda h: (h[0], h[1]))
        rules = {"tok": self._tok, "win": self._win, "nix": self._nix, "mail": self._mail,
                 "step": self._step, "xml": self._xml, "dxf": self._dxf, "xt": self._xt,
                 "vti": self._vti, "tagval": self._tagval, "propval": self._propval, "tok16": self._tok16, "win16": self._win16, "fc": self._fc, "3mf": self._3mf}
        self._last = 0
        for s, neg_n, kind, extra in hits:
            if s < self._last or any(a < s - neg_n and s < b for a, b in spans):
                continue  # already replaced by an earlier rule
            e = rules[kind](buf, low, s, -neg_n, extra)
            if e:
                self._last = e
                count += 1
        return count

    def text(self, value):
        """Scan a short str (a path component, a metadata value); returns the new str."""
        buf = bytearray(value.encode("utf-8", "surrogateescape"))
        if not self.scan(buf, first=False):
            return value
        return bytes(buf).decode("utf-8", "surrogateescape")
