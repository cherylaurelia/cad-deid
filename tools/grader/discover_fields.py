"""Identity discovery from INPUT files (from the corpus auditor's discover.py): identity-like
fields in any view of a file (properties, XML, STEP/IGES/DXF headers, Parasolid, user folders,
e-mail, URLs, git accounts). Used only to build the grader's answer key; private."""
import re

EMAIL = re.compile(rb"([A-Za-z0-9._%+-]{1,64})@([A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,12})(?![A-Za-z0-9])")
WINUSER = re.compile(rb"(?i)(?:[a-z]:|\\\\[^\\/]{1,40})?[\\/]+(?:users|documents and settings|dokumente und einstellungen)[\\/]+([^\\/:*?\"<>|\r\n\x00]{2,40})[\\/]")
NIX = re.compile(rb"(?<![A-Za-z0-9._-])/(?:home|Users)/([A-Za-z0-9_.-]{2,32})/")
GIT = re.compile(rb"(?i)(github\.com|gitlab\.com|bitbucket\.org|thingiverse\.com/|grabcad\.com/library|printables\.com|onshape\.com|instructables\.com/member|hackaday\.io/)[/:]+([A-Za-z0-9_.-]{2,60})(?:/([A-Za-z0-9_.-]{1,100}))?")
GHIO = re.compile(rb"(?i)([a-z0-9-]{2,39})\.(?:github|gitlab)\.io")
URLHOST = re.compile(rb"(?i)(?:https?://|www\.)([a-z0-9-]+(?:\.[a-z0-9-]+)+)")
PHONE = re.compile(rb"(?i)\b(?:tel|phone|fax|mobile|t\xe9l)\b\.?\s*[:.]?\s*(\+?[0-9][0-9 ()./-]{6,20}[0-9])")
STEP_FN = re.compile(rb"FILE_NAME\s*\(")
STEP_ENT = re.compile(rb"(?i)[=\s](PERSON|ORGANIZATION|APPROVAL_PERSON_ORGANIZATION|PERSON_AND_ORGANIZATION)\s*\(")
STR = re.compile(rb"'((?:[^']|'')*)'")
DXF_VAR = re.compile(rb"\$(LASTSAVEDBY|AUTHOR|PROJECTNAME)\s*\r?\n\s*\d+\s*\r?\n([^\r\n]{1,200})")
XT = re.compile(rb"(USER|SITE|MC_ID|MC_MODEL|MACHINE_ID|NODE)=([^;\r\n]{1,80})[;\r\n]")
XMLTAG = re.compile(rb"(?is)<(dc:creator|dc:publisher|dc:rights|dc:contributor|cp:lastModifiedBy|dc:lastModifiedBy|meta:initial-creator|dc:title|pdf:Author|Author|LastAuthor|Company|Manager|xmpRights:Owner|photoshop:AuthorsPosition|Iptc4xmpCore:CreatorContactInfo)(?:\s[^>]*)?>(.{1,400}?)</\1")
RDFLI = re.compile(rb"<rdf:li[^>]*>([^<]{1,200})</rdf:li>")
FCPROP = re.compile(rb"(?is)<Property name=\"(CreatedBy|LastModifiedBy|Company|FileName|Comment|License|LicenseURL|Id|Uid)\"[^>]*>\s*<(?:String|Uuid|Path)[^>]*value=\"([^\"]{1,400})\"")
MF3 = re.compile(rb"(?is)<metadata\s+name=\"([^\"]{1,40})\"[^>]*>([^<]{1,400})</metadata>")
SVGATTR = re.compile(rb"(?i)(inkscape:export-filename|sodipodi:docname|sodipodi:docbase|xlink:href|sodipodi:absref)=\"([^\"]{1,400})\"")
LABEL = re.compile(rb"(?im)^[^\r\n]{0,40}?\b(author|drawn by|designed by|designer|engineer|checked by|approved by|created by|copyright|\(c\)|\xa9|maintainer|contact|owner)\b\s*[:=]?\s*([^\r\n]{2,80})")
CUSTOMXML = re.compile(rb"(?is)<property\s[^>]*name=\"([^\"]{1,60})\"[^>]*>\s*<vt:lpwstr>([^<]{1,200})</vt:lpwstr>|<property\s[^>]*name=\"([^\"]{1,60})\"[^>]*>\s*<vt:lpstr>([^<]{1,200})</vt:lpstr>")
CUSTOM_NAME = re.compile(rb"(?i)author|drawn|designer|designed|engineer|checked|approved|company|customer|client|vendor|supplier|manager|owner|creator|zeichner|konstrukt|bearbeit|firma|dessin|auteur|saved by|ersteller|verantwort")
SOLID = re.compile(rb"(?m)^\s*solid\s+([^\r\n]{1,120})")
GENERIC = set("""none unknown unspecified author user default n/a na anonymous admin administrator owner student
pc guest root public operator test name company organization designer engineer me nobody null undefined
title subject""".split())


def text_of(b):
    return b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else str(b)


def iges_global(b):
    lines = b.split(b"\n")
    g = b"".join(l[:72] for l in lines if len(l) >= 73 and l[72:73] == b"G")
    if not g:
        return []
    pd = b"," if not g.startswith(b"1H") else g[2:3]
    out, i, fields = [], 0, []
    # tokenise Hollerith-aware
    cur = b""
    rd = b";"
    if g.startswith(b"1H"):
        i = 4
        if g[i:i + 2] == b"1H":
            rd = g[i + 2:i + 3]
            i += 4
        fields = [b"", b""]
    while i < len(g):
        m = re.match(rb"\s*(\d+)H", g[i:])
        if m and (i == 0 or g[i - 1:i] in (pd, b" ")):
            n = int(m.group(1))
            s = i + m.end()
            fields.append(g[s:s + n])
            i = s + n
            while i < len(g) and g[i:i + 1] == b" ":
                i += 1
            i += 1
            continue
        j = i
        while j < len(g) and g[j:j + 1] not in (pd, rd):
            j += 1
        fields.append(g[i:j].strip())
        if j < len(g) and g[j:j + 1] == rd:
            break
        i = j + 1
    names = {3: "iges-sender", 4: "iges-filename", 12: "iges-receiver", 21: "iges-author", 22: "iges-org"}
    for k, kind in names.items():
        if k - 1 < len(fields) and fields[k - 1]:
            out.append((kind, fields[k - 1]))
    return out


def step_header(b):
    m = STEP_FN.search(b, 0, 40000)
    if not m:
        return []
    out, depth, arg, i = [], 1, 0, m.end()
    end = min(len(b), i + 8000)
    while i < end and depth > 0:
        c = b[i:i + 1]
        if c == b"'":
            mm = STR.match(b, i)
            if not mm:
                break
            kind = {0: "step-filename", 2: "step-author", 3: "step-org", 6: "step-authorisation"}.get(arg)
            if kind:
                out.append((kind, mm.group(1)))
            i = mm.end()
            continue
        if c == b"(":
            depth += 1
        elif c == b")":
            depth -= 1
        elif c == b"," and depth == 1:
            arg += 1
        i += 1
    return out


def step_entities(b):
    out = []
    for m in STEP_ENT.finditer(b):
        ent = m.group(1).upper().decode()
        if ent not in ("PERSON", "ORGANIZATION"):
            continue
        seg = b[m.end():m.end() + 1500]
        close = seg.find(b";")
        seg = seg[:close if close > 0 else len(seg)]
        strs = [s.group(1) for s in STR.finditer(seg)]
        if ent == "PERSON":
            for k, s in enumerate(strs[:4]):
                out.append(("step-person", s))
        else:
            for s in strs[:2]:
                out.append(("step-org", s))
    return out


def extract(view_kind, b, ext, binary):
    out = []
    if view_kind == "prop":
        return [("prop", b)]
    if view_kind in ("name",):
        return [("member-name", b)]
    if view_kind == "error":
        return []
    texts = [b]
    if binary and len(b) <= (96 << 20):
        for off in (0, 1):
            try:
                t = b[off:].decode("utf-16le", "ignore")
                # keep only runs of printable chars so regexes can see them
                t = re.sub(r"[^\x20-\x7e -ɏ一-鿿]+", "\n", t)
                texts.append(t.encode("utf-8"))
            except Exception:
                pass
    for t in texts:
        for m in EMAIL.finditer(t):
            out.append(("email", m.group(0)))
        for m in WINUSER.finditer(t):
            out.append(("win-user", m.group(1)))
        for m in NIX.finditer(t):
            out.append(("nix-user", m.group(1)))
        for m in GIT.finditer(t):
            out.append(("git-account", m.group(1).lower() + b"/" + m.group(2)))
            if m.group(3):
                out.append(("git-repo", m.group(2) + b"/" + m.group(3)))
        for m in GHIO.finditer(t):
            out.append(("github-io", m.group(1)))
        for m in URLHOST.finditer(t):
            out.append(("url-host", m.group(1)))
        for m in PHONE.finditer(t):
            out.append(("phone", m.group(1)))
        for m in XT.finditer(t[:20000]):
            out.append(("xt-" + m.group(1).decode().lower(), m.group(2)))
        for m in XMLTAG.finditer(t):
            val = m.group(2)
            lis = RDFLI.findall(val)
            for v in (lis or [val]):
                out.append(("xml-" + m.group(1).decode().lower(), v))
        for m in FCPROP.finditer(t):
            out.append(("fc-" + m.group(1).decode().lower(), m.group(2)))
        for m in MF3.finditer(t):
            out.append(("3mf-" + m.group(1).decode().lower(), m.group(2)))
        for m in CUSTOMXML.finditer(t):
            name, val = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            if CUSTOM_NAME.search(name):
                out.append(("xmlprop:" + name.decode("latin-1"), val))
        for m in SVGATTR.finditer(t):
            out.append(("svg-" + m.group(1).decode().lower(), m.group(2)))
    if not binary:
        if ext in (".step", ".stp", ".p21") or b[:13] == b"ISO-10303-21;":
            out += step_header(b) + step_entities(b)
        if ext in (".igs", ".iges"):
            out += iges_global(b[:200000])
        if ext == ".dxf":
            for m in DXF_VAR.finditer(b[:2000000]):
                out.append(("dxf-" + m.group(1).decode().lower(), m.group(2)))
        if ext == ".stl":
            for m in SOLID.finditer(b[:4096]):
                out.append(("stl-solid", m.group(1)))
        for m in LABEL.finditer(b[:4000000]):
            out.append(("label-" + m.group(1).decode("latin-1").lower(), m.group(2)))
    elif ext == ".stl":
        out.append(("stl-header", b[:80]))
    return out


def looks_binary(b):
    head = b[:8192]
    return b"\x00" in head


