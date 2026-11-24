"""Original-identity rules of the local grader: path identities per project, filters for the
identity fields found in input files, strength classes. Independent of code/deid (only the
input and the manifest are read). Private: the key built from these rules holds originals."""
import re
import unicodedata

HEX12 = re.compile(r"[0-9a-f]{12}$")
HEXLONG = re.compile(r"[0-9a-f]{6,}$")
WORD = re.compile(r"[A-Za-z0-9]+")
SEPS = re.compile(r"[\s\-_.­​]+")


def _english():
    for p in ("/usr/share/dict/words", "/usr/share/dict/american-english"):
        try:
            return {w.strip().lower() for w in open(p, encoding="utf-8", errors="replace") if w.strip()}
        except OSError:
            continue
    return set()


ENGLISH = _english()
GENERIC = set("""none unknown unspecified author user users default n/a na anonymous admin administrator administrators
owner student pc guest root public operator test name company organization organisation designer engineer drafter
checker approver me nobody null undefined title subject utente usuario utilisateur benutzer local system appdata
desktop documents downloads temp tmp program files windows wheel staff runner ubuntu docker build builder jenkins
vagrant www data share lib bin opt var etc solidworks autodesk inventor creo catia siemens nx freecad fusion onshape
office microsoft adobe kicad ptc din psadmin cm10 all-users default-user public-user dell hp lenovo acer asus
customer client vendor supplier manager project part assembly drawing model rev revision sheet scale date
vorgabe standard standardnutzer all users noname intel""".split()) | {"all users", "default user", "program files"}
VENDOR = re.compile(r"(?i)mcmaster|traceparts|misumi|digi-?key|samtec|te connectivity|automationdirect|mill-max|"
                    r"mouser|farnell|igus|bosch rexroth|festo|smc corp|thorlabs inc|3d ?content ?central|"
                    r"partcommunity|cadenas|snapeda|ultralibrarian|keystone|wurth|würth|molex|hirose|jst|"
                    r"amphenol|phoenix contact|harting|lapp|schaeffler|skf|misumi|accu|rs components")
TLD = set("""com org net edu gov mil int io co uk de fr it es nl be ch at se no dk fi pl cz sk hu ro bg gr pt ie ru ua
by lt lv ee si hr rs ba mk al tr il ae sa in cn jp kr tw hk sg my th vn id ph au nz ca us mx br ar cl pe co ve za eg ng
ke info biz me tv cc ws xyz app dev ai eu asia name pro tech online site store space cloud design studio""".split())
SOFTWARE = re.compile(r"(?i)solidworks|autodesk|inventor|autocad|creo|pro/engineer|catia|fusion|freecad|kicad|"
                      r"microsoft|acrobat|adobe|distiller|pscript|ghostscript|latex|cairo|libreoffice|openoffice|"
                      r"reportlab|onshape|siemens|zwcad|draftsight|bluebeam|nitro|foxit|quartz|mac os|windows|"
                      r"printer|ilovepdf|inkscape|matplotlib|eeschema|pcbnew|indesign|illustrator|chrome|skia|"
                      r"ezdxf|librecad|qcad|oda |open cascade|opencascade|spatial|parasolid|acis|step ap|"
                      r"pdf-xchange|pdfcreator|bullzip|cutepdf|primopdf|word|excel|powerpoint|writer|calc")
STEP_PLACEHOLDER = re.compile(r"(?i)^(unspecified|unknown|none|nauo-per\d*|nauo-org\d*|per\d*|org\d*|id\d*|person\d*|"
                              r"organization\d*|\$|''|-|\s*|anonymous|undefined|n/?a|null|default|user\d*)$")
JUNK = re.compile(r"(?i)^(program files.*|materials?|steel|aluminum|aluminium|application|standard|black|red|white|"
                  r"blue|green|satin|chrome.*|polycarbonate.*|default|generic|\d+|[a-z]?\d{1,4}|id_\d+|"
                  r".*\.(sldprt|sldasm|slddrw|ipt|iam|step|stp|dwg|pdf|stl|dxf|igs|iges|x_t|par|asm|prt))$")
PUBLIC_MAIL = set("gmail.com googlemail.com yahoo.com hotmail.com outlook.com live.com msn.com icloud.com me.com "
                  "qq.com 163.com 126.com gmx.de gmx.net web.de protonmail.com aol.com mail.com".split())
INFRA_HOST = re.compile(r"(^|\.)(w3\.org|iso\.org|adobe\.com|purl\.org|microsoft\.com|openxmlformats\.org|autodesk\.com|"
                        r"solidworks\.com|ptc\.com|siemens\.com|github\.com|gitlab\.com|google\.com|youtube\.com|youtu\.be|"
                        r"wikipedia\.org|creativecommons\.org|gnu\.org|opencascade\.com|freecad\.org|freecadweb\.org|"
                        r"openscad\.org|inkscape\.org|sourceforge\.net|xml\.org|apache\.org|mozilla\.org|python\.org|"
                        r"doi\.org|3ds\.com|spatial\.com|arxiv\.org|ieee\.org|acm\.org|springer\.com|elsevier\.com|"
                        r"sciencedirect\.com|researchgate\.net|wiley\.com|mdpi\.com|nature\.com|ncbi\.nlm\.nih\.gov|"
                        r"orcid\.org|crossref\.org|jstor\.org|amazon\.com|apple\.com|example\.com|schemas\.\w+\.\w+|"
                        r"ns\.adobe\.com|instagram\.com|facebook\.com|twitter\.com|x\.com|linkedin\.com|reprap\.org|"
                        r"thingiverse\.com|printables\.com|mcmaster\.com|kicad\.org|octoprint\.org|slic3r\.org|"
                        r"hackaday\.io|hackaday\.com|github\.io|readthedocs\.io|cadquery\.org|fsf\.org|opensource\.org|"
                        r"tinyurl\.com|bit\.ly|goo\.gl|dropbox\.com|drive\.google\.com|grabcad\.com|onshape\.com|"
                        r"instructables\.com|arduino\.cc|python\.org|numpy\.org|scipy\.org|matplotlib\.org|"
                        r"steptools\.com|hsmworks\.com|cipa\.jp|metalprices\.com|plasticsnews\.com|snapeda\.com|"
                        r"traceparts\.com|3dcontentcentral\.com|partcommunity\.com|cadenas\.de|ultralibrarian\.com|"
                        r"misumi\S*|digikey\.\w+|mouser\.\w+|ptc\.com|sw\.com|dassault\S*)$")
PERSON_KINDS = set("""saved-by user-folder ole-name xml-name xml-dc:creator xml-cp:lastmodifiedby xml-dc:lastmodifiedby
xml-meta:initial-creator xml-author xml-lastauthor step-author step-person iges-author dxf-lastsavedby dxf-author xt-user
fc-createdby fc-lastmodifiedby 3mf-designer git-account""".split())
NAME_PROPS = re.compile(r"(?i)^(e0859ff2:(4|8)|02d5cdd5:(14|15)|0f3f8532:(10|12|34|41|42|43|7)|39de383d:(4|8)|"
                        r"30fb61d8:16|[0-9a-f]{8}:.*(author|drawn|designer|designed|engineer|checked|approv|company|"
                        r"customer|client|manager|owner|creator|zeichner|konstrukt|bearbeit|firma|dessin|auteur|"
                        r"saved by|ersteller|verantwort))")
SAVED_PROPS = re.compile(r"(?i)^(e0859ff2:8|39de383d:8|30fb61d8:16|[0-9a-f]{8}:.*saved by.*)$")
FIELD_KINDS = set("""step-author step-org step-authorisation step-person iges-author iges-org dxf-lastsavedby
dxf-author xt-user xt-site xml-dc:creator xml-cp:lastmodifiedby xml-dc:lastmodifiedby xml-company xml-author
xml-meta:initial-creator xml-lastauthor xml-manager fc-createdby fc-lastmodifiedby fc-company 3mf-designer
3mf-copyright 3mf-licenseterms tar-owner""".split())


def norm(s):
    return SEPS.sub("", unicodedata.normalize("NFKC", s).lower())


def camel_words(tok):
    parts = []
    for w in re.split(r"[^A-Za-z0-9]+", tok):
        parts += re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", w)
    return [p for p in parts if p]


def generic_word(w):
    lw = w.lower()
    return lw in ENGLISH or lw in GENERIC or len(lw) < 3 or lw.isdigit()


def distinctive(w, minlen=4):
    return len(w) >= minlen and not generic_word(w) and not HEXLONG.fullmatch(w.lower()) and not w.isdigit()


def strength(v):
    """strong: names a specific person/organisation/project; medium: plausible; weak: generic words."""
    if not v.isascii():
        return "strong" if len(v.strip()) >= 2 else "drop"
    words = WORD.findall(v)
    if not words:
        return "drop"
    if any(distinctive(w, 3) for w in words):
        return "strong" if len(v) >= 4 else "medium"
    if len(words) >= 2 and len(v) >= 8:
        return "medium"
    return "weak"


def is_generic_value(v):
    v = v.strip()
    lv = v.lower()
    return (not v or lv in GENERIC or STEP_PLACEHOLDER.match(v) is not None or JUNK.match(v) is not None
            or SOFTWARE.search(v) is not None or len(norm(v)) < 3)


def project_key(rel):
    p = rel.split("/")
    if p[0] == "archives":
        return rel
    if len(p) > 2 and p[1] == "existing":
        return "/".join(p[:5])
    return "/".join(p[:3])


def path_identities(rel):
    """[(kind, value, strength)] identities named by the project folders of one path."""
    p = rel.split("/")
    out = []

    def add(kind, value, st):
        value = value.strip(" _-.")
        if len(norm(value)) >= 2:
            out.append((kind, value, st))

    def owner_repo(toks):
        for k, t in enumerate(toks):
            kind = "path-owner" if k == 0 else "path-repo"
            words = camel_words(t)
            if k == 0:
                add(kind, t, "strong")
            elif any(distinctive(w) for w in words) or (len(words) >= 2 and len(t) >= 8):
                add(kind, t, "strong")
            else:
                add(kind, t, "weak")
            for w in words:
                if distinctive(w):
                    add(kind + "-word", w, "strong" if len(w) >= 5 or k == 0 else "medium")

    if p[0] == "archives" and len(p) > 1:
        name = re.sub(r"\.zip$", "", p[1])
        owner_repo([t for t in name.split("__") if t and not HEX12.match(t)])
        return out
    if len(p) < 3 or p[0] != "sources":
        return out
    g, third = p[1], p[2]
    if g == "github":
        owner_repo([t for t in third.split("__") if t and not HEX12.match(t)])
    elif g == "gitlab":
        owner_repo([t for t in third.split("__") if t and t.lower() not in ("gitlab.com", "project") and not HEX12.match(t)])
    elif g == "web":
        labels = third.split(".")
        keep = 2 if len(labels) > 2 and labels[-2] in ("co", "ac", "edu", "org", "gov", "com", "net") and len(labels[-1]) == 2 else 1
        add("path-host", ".".join(l for l in labels if l.lower() != "www"), "strong")
        for lab in labels[:-keep]:
            if lab.lower() in ("www", "github", "gitlab", "sites", "docs", "en", "de", "web", "files", "static", "cdn", "dl", "download", "shop", "public"):
                continue
            add("path-host-label", lab, "strong" if distinctive(lab, 3) else "medium")
            for w in camel_words(lab):
                if w.lower() != lab.lower() and distinctive(w):
                    add("path-host-word", w, "strong")
    elif g == "catalog":
        words = re.split(r"[\s_]+", third)
        lead = []
        for w in words:
            if not w:
                continue
            if (w[:1].isupper() or any(c.isdigit() for c in w)) and (not generic_word(w) or w.isupper() or w in ("University", "Institute")):
                lead.append(w)
            else:
                break
        if lead and any(distinctive(x, 3) or x.isupper() and len(x) >= 3 for w in lead for x in [w] + camel_words(w)):
            add("path-catalog-org", " ".join(lead), "strong")
            for w in lead:
                for x in [w] + camel_words(w):
                    if distinctive(x, 3) or (x.isupper() and len(x) >= 3):
                        add("path-catalog-word", x, "strong")
    elif g == "existing":
        for i, c in enumerate(p[:-1]):
            if c in ("new-github", "github") and i + 1 < len(p) - 1:
                add("path-owner", p[i + 1], "strong")
        for comp in p[2:5]:
            for w in re.split(r"[^A-Za-z0-9]+", comp):
                if distinctive(w, 4) and not re.fullmatch(r"(validate|round\d+|research|search|scientific|industrial|curated)", w.lower()):
                    add("path-existing-word", w, "medium")
    return out


def field_identity(kind, value):
    """Filter one discovered (kind, value) from an input file into (idkind, value) or None."""
    value = value.strip().strip("'\"").strip()
    if not value or len(value) > 80 or is_generic_value(value):
        return None
    if VENDOR.search(value):
        return None
    if kind.startswith("prop:"):
        pk = kind.split("#")[-1] if "#" in kind else kind[5:]
        if not NAME_PROPS.match(pk):
            return None
        if "\\" in value:
            value = re.split(r"\\", value)[-1].strip()
            if is_generic_value(value):
                return None
        return ("saved-by" if SAVED_PROPS.match(pk) else "ole-name", value)
    if kind.startswith("xmlprop:"):
        low = kind.lower()
        if re.search(r"vendor|supplier|manufact|hersteller|lieferant", low):
            return None
        return ("saved-by" if "saved by" in low else "xml-name", value)
    if kind in ("win-user", "nix-user"):
        if "~" in value or value.lower() in GENERIC or value.lower() in ENGLISH:
            return None
        return ("user-folder", value)
    if kind == "email":
        m = re.match(r"([^@]+)@(.+)", value)
        if not m or re.search(r"(?i)\.(sldprt|sldasm|slddrw|prt|asm|step|stp|png|jpg)$", value) or re.match(r"(?i)sketch\d", value):
            return None
        dom = m.group(2)
        labels = dom.split(".")
        if INFRA_HOST.search(dom.lower()) or dom.lower() in ("example.com", "domain.com", "email.com"):
            return None
        if labels[-1].lower() not in TLD or not (labels[-1].islower() or labels[-1].isupper()) or min(len(x) for x in labels) < 2:
            return None
        if not re.search(r"[A-Za-z]{2}", m.group(1)) or re.match(r"^\d+\.\d", m.group(1)):
            return None
        return ("email", value)
    if kind == "git-account":
        acct = value.split("/", 1)[1] if "/" in value else value
        if acct.lower() in ("orgs", "users", "blob", "tree", "raw", "issues", "wiki", "releases", "about", "features", "explore", "login", "settings", "topics", "search", "marketplace", "sponsors", "pricing"):
            return None
        return ("git-account", acct)
    if kind == "github-io":
        return ("git-account", value)
    if kind == "url-host":
        v = value.lower()
        if v.startswith("www."):
            v = v[4:]
        if INFRA_HOST.search(v) or "." not in v:
            return None
        return ("url-host", v)
    if kind in FIELD_KINDS or kind.startswith("xml-") and kind not in ("xml-dc:title",):
        if kind == "xml-dc:title":
            return None
        if "<" in value or ">" in value:
            return None
        return (kind, value)
    return None


def field_strength(kind, value):
    st = strength(value)
    if st == "weak" and kind in PERSON_KINDS and value.strip().lower() not in GENERIC and len(value.strip()) >= 3:
        return "medium"
    return st


MORE_INFRA = re.compile(r"(?i)(^|\.)(gimp\.org|iptc\.org|useplus\.org|digicert\.com\d*|serif\.com|blender\.org|"
                        r"json-schema\.org|shields\.io|iec\.ch|cern-ohl\S*|netfabb\.com|transcendata\.com|"
                        r"octopart\.com|homedepot\.com|adafruit\.com|pololu\.com|yageo\.com|infineon\.com|"
                        r"diodes\.com|st\.com|ti\.com|shadcn\.com|kicad\S*|sparkfun\.com|cadenas\.de|web2cad\.de)$")


def adjust(kinds, value, st):
    """Grade-time strength: content-only URL hosts are medium (vendor and tool sites are common in
    CAD files and the scorer is unlikely to treat them as the authors' identities); tool hosts weak."""
    if st == "strong" and all(k.endswith("url-host") or k.endswith("url-label") for k in kinds):
        return "weak" if MORE_INFRA.search(value) or INFRA_HOST.search(value) else "medium"
    return st
