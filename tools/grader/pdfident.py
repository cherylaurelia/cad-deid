"""PDF identity inventory of the local grader (ported from the PDF auditor's score.py): identities
read from an INPUT PDF's metadata, XMP, page text (e-mails, URL hosts, phones, addresses,
copyright / property-of holders, company names, user folders) and title-block label values.
Private: the values are originals."""
import collections
import re

from idkey import ENGLISH, GENERIC, norm, WORD

SOFTWARE_RX = None  # set below


def is_generic(v):
    v = v.strip()
    return not v or v.lower() in GENERIC or len(norm(v)) < 3


def generic_word(w):
    lw = w.lower()
    return lw in ENGLISH or lw in GENERIC or len(lw) < 3 or lw.isdigit()


LABELS = [
    "drawn by", "drawn", "drwn", "drn", "dwn", "drafter", "drafted by", "drafted", "designed by", "designed",
    "designer", "design by", "dsgn", "checked by", "checked", "chk", "chkd", "approved by", "approved", "appr",
    "appd", "eng appr", "mfg appr", "engineer", "engineered by", "created by", "created", "author", "authors",
    "prepared by", "reviewed by", "made by", "company", "client", "customer", "owner", "contact",
    "gezeichnet", "gez", "gezeichn", "bearbeitet", "bearb", "geprüft", "gepr", "erstellt von", "erstellt",
    "ersteller", "konstrukteur", "konstr", "zeichner", "freigabe", "freigegeben", "firma", "autor", "verfasser",
    "dessiné par", "dessiné", "dessinateur", "dessine", "vérifié par", "vérifié", "verifie", "approuvé",
    "auteur", "société", "créé par", "conçu par", "dibujado", "dibujó", "diseñado", "revisado", "aprobado",
    "disegnato", "disegnatore", "controllato", "approvato", "progettista", "desenhado", "elaborado",
    "empresa", "设计", "制图", "审核", "批准", "校对", "作成", "設計", "製図", "承認", "検図", "設計者",
]
LABEL_SET = {norm(l) for l in LABELS}
NONVALUE = set(norm(x) for x in """name date signature sign title scale sheet size rev revision dwg no qty
material finish weight units mm inch tolerances unless otherwise specified part number description
item datum sheet of a4 a3 a2 a1 a0 projection third first angle angular dimensions are in drawing
- -- n/a na tbd none date: name: datum unterschrift maßstab massstab blatt werkstoff benennung
zeichnungsnummer nom echelle échelle matière format feuille indice visa titel durch von by for mfg
chk'd chk qa q.a. eng engr sheet weight dwg part drawn checked approved designed tol tolerance
surface treatment heat edges break all dims dimension notes note quantity rev. date: list sign. ok
genehmigt geprüft gezeichnet bearbeitet norm ersetzt ursprung zust änderung blatt
verificato fatto fecha aprovado estado blocked titel revisado dibujado aprobado cad cam gpt sem
""".split()) | LABEL_SET
INFRA_HOSTS = re.compile(r"(^|\.)(doi\.org|dx\.doi\.org|semanticscholar\.org|arxiv\.org|sciencedirect\.com|elsevier\.com|"
                         r"springer\.com|link\.springer\.com|ieee\.org|ieeexplore\.ieee\.org|acm\.org|dl\.acm\.org|"
                         r"researchgate\.net|wiley\.com|tandfonline\.com|mdpi\.com|nature\.com|scholar\.google\.com|"
                         r"wikipedia\.org|youtube\.com|youtu\.be|github\.com|gitlab\.com|creativecommons\.org|"
                         r"w3\.org|iso\.org|adobe\.com|microsoft\.com|google\.com|apple\.com|amazon\.com|"
                         r"python\.org|apache\.org|mozilla\.org|gnu\.org|sourceforge\.net|ncbi\.nlm\.nih\.gov|"
                         r"pubmed\.ncbi\.nlm\.nih\.gov|orcid\.org|crossref\.org|jstor\.org|purl\.org)$")
GENERIC_LOCAL = set("""info contact sales support admin office mail webmaster service example permissions
help hello team noreply no-reply enquiries inquiries kontakt technik vertrieb""".split())
DATE_RX = re.compile(r"^[\d./\-:]+$|^\d{1,2}[-/ ][A-Za-z]{3}[-/ ]\d{2,4}$|^(19|20)\d\d|^(yyyy|yy|dd|mm|tt|jjjj)[./\-]", re.I)
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]{2,64}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,12}")
URL_RX = re.compile(r"(?:https?://|www\.)[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", re.I)
PHONE_RX = re.compile(r"(?:(?:tel|phone|fax|mob|mobile|telefon|téléphone|ph)\.?\s*[:.]?\s*)(\+?[\d][\d ()./\-]{7,20}\d)"
                      r"|(\+\d{1,3}[ .\-]?\(?\d{1,4}\)?(?:[ .\-]?\d{2,4}){2,4})", re.I)
ADDR_RX = re.compile(r"\b\d{1,5}[A-Za-z]?\s+(?:[A-Z][A-Za-z\-']+\s){1,3}(?:Street|St\.|Road|Rd\.?|Avenue|Ave\.?|"
                     r"Boulevard|Blvd\.?|Lane|Ln\.?|Drive|Dr\.|Way|Court|Ct\.?|Place|Pl\.?|Parkway|Pkwy|Highway|Hwy)\b"
                     r"|\b[A-ZÄÖÜ][a-zäöüß\-]+(?:straße|strasse|str\.|weg|platz|allee|gasse)\s+\d{1,4}[a-z]?\b"
                     r"|\b\d{1,4},?\s+(?:rue|avenue|boulevard|chemin|calle|avenida|via)\s+(?:[A-Z][A-Za-zéèêàç'\-]+\s?){1,4}"
                     r"|\b(?:rue|avenue|boulevard|chemin|calle|avenida|viale|via)\s+(?:[A-Z][A-Za-zéèêàç'\-]+\s){1,4},?\s*\d{1,4}\b")
COPY_RX = re.compile(r"(?:©|\([cC]\)|[Cc]opyright|COPYRIGHT)\s*(?:\([cC]\)|©)?\s*(?:(?:19|20)\d\d(?:\s*[-–,]\s*(?:19|20)?\d\d)*)?\s*,?\s*"
                     r"(?:by\s+)?([A-Z][A-Za-z0-9&.,'\- ]{2,60}?)(?=\s*(?:\.|,|\n|[Aa]ll [Rr]ights|ALL RIGHTS|$|\||;|is licensed))")
PROPERTY_RX = re.compile(r"(?:sole |exclusive )?property of\s+([A-Z][A-Za-z0-9&.,'\- ]{2,60}?)(?=\s*(?:\.|,|\n|any|$|;))", re.I)
CORP_RX = re.compile(r"\b((?:[A-Z][A-Za-z0-9&\-]+\s){0,3}[A-Z][A-Za-z0-9&\-]+)\s*,?\s+(Inc\.?|Ltd\.?|LLC|GmbH|AG|S\.?A\.?S?|"
                     r"B\.?V\.?|Co\.,?\s*Ltd\.?|Corp\.?|Corporation|Limited|S\.?r\.?l\.?|S\.?L\.?|SpA|Oy|AB|ApS|K\.?K\.?|Pty)\b")
WINUSER_RX = re.compile(r"(?:[A-Za-z]:)?\\(?:users|documents and settings)\\([^\\/:*?\"<>|\r\n]{2,40})", re.I)
NIXUSER_RX = re.compile(r"/(?:home|Users)/([a-z_][a-z0-9_.\-]{1,31})/")
DOMUSER_RX = re.compile(r"^([A-Za-z0-9][A-Za-z0-9\-_.]{0,30})\\([A-Za-z0-9][A-Za-z0-9\-_. ]{0,40}?)(?:\s*\(([^)]{1,40})\))?$")
SOFTWARE_RX = re.compile(r"solidworks|autodesk|inventor|autocad|creo|pro/engineer|catia|fusion|freecad|kicad|"
                         r"microsoft|word|excel|acrobat|adobe|distiller|pscript|ghostscript|pdf|latex|tex|"
                         r"cairo|qt |libreoffice|openoffice|writer|google|reportlab|onshape|nx|siemens|zwcad|"
                         r"draftsight|bluebeam|nitro|foxit|quartz|mac os|windows|printer|cgm2pdf|plot|hdi|"
                         r"ilovepdf|online2pdf|jspdf|inkscape|matplotlib|eeschema|pcbnew|indesign|illustrator|"
                         r"wps|prince|wkhtml|chrome|skia|safari|firefox|ezdxf|librecad|qcad|any dwg|oda ", re.I)
VENDOR_DOMAINS = set("""3ds.com autodesk.com solidworks.com adobe.com microsoft.com w3.org ptc.com siemens.com
google.com apple.com reportlab.com reportlab.org pdf-tools.com ilovepdf.com purl.org ns.adobe.com
iso.org example.com github.com gitlab.com creativecommons.org youtube.com wikipedia.org gnu.org
mcmaster.com instagram.com facebook.com twitter.com x.com linkedin.com hackaday.io hackaday.com
octoprint.org slic3r.org thingiverse.com printables.com sbcglobal.net reprap.org github.io
openreview.net neurips.cc huggingface.co arxiv.org""".split())


def meta_identities(add, i, rec):
    info = rec.get("info", {})
    for key, val in info.items():
        if not isinstance(val, str) or not val.strip():
            continue
        v = val.strip()
        k = key.lstrip("/")
        for m in WINUSER_RX.finditer(v):
            add(i, m.group(1), "meta:userpath")
        for m in NIXUSER_RX.finditer(v):
            add(i, m.group(1), "meta:userpath")
        for m in EMAIL_RX.finditer(v):
            add(i, m.group(0), "meta:email")
        if k in ("Author", "Company", "Manager", "LastModifiedBy", "Owner", "Operator", "Creator.Name", "AuthorEmail", "Authors"):
            if is_generic(v) or SOFTWARE_RX.search(v):
                continue
            m = DOMUSER_RX.match(v)
            if m:
                if not is_generic(m.group(2)) and m.group(2).lower() not in ("owner", "operator", "admin", "user", "administrator"):
                    add(i, m.group(2), "meta:author-user")
                if not generic_word(m.group(1)):
                    add(i, m.group(1), "meta:author-domain", "medium")
                if m.group(3) and not generic_word(m.group(3)):
                    add(i, m.group(3), "meta:author-host", "medium")
            else:
                add(i, v, "meta:" + k.lower())
        elif k in ("Keywords", "Subject", "Title", "Creator"):
            if k == "Creator" and SOFTWARE_RX.search(v):
                continue
            # person-like: "M. Henson", "John Smith"
            for m in re.finditer(r"\b([A-Z]\.\s?[A-Z][a-z]{2,}|[A-Z][a-z]{2,}\s[A-Z][a-z]{2,})\b", v):
                name = m.group(1)
                if not all(generic_word(w) for w in WORD.findall(name)):
                    add(i, name, "meta:%s-name" % k.lower(), "medium")
    xmp = rec.get("xmp", "") or ""
    for tag in ("dc:creator", "pdf:Author", "xmpRights:Owner", "photoshop:AuthorsPosition", "dc:publisher", "dc:contributor"):
        for blk in re.findall(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), xmp, re.S):
            for v in re.findall(r"<rdf:li[^>]*>([^<]{1,200})</rdf:li>", blk) or [blk]:
                v = v.strip()
                if v and not is_generic(v) and not SOFTWARE_RX.search(v) and "<" not in v:
                    m = DOMUSER_RX.match(v)
                    add(i, m.group(2) if m else v, "xmp:" + tag)
        for v in re.findall(r'%s="([^"]{1,200})"' % tag, xmp):
            if not is_generic(v) and not SOFTWARE_RX.search(v):
                m = DOMUSER_RX.match(v)
                add(i, m.group(2) if m else v, "xmp:" + tag)


def text_identities(add, i, text, where):
    for m in EMAIL_RX.finditer(text):
        e = m.group(0)
        dom = e.split("@")[1].lower()
        if dom in VENDOR_DOMAINS:
            continue
        local = e.split("@")[0]
        if local.lower() in GENERIC_LOCAL and dom in ("email.com", "example.com", "domain.com"):
            continue
        add(i, e, where + ":email")
        if local.lower() not in GENERIC_LOCAL:
            add(i, local, where + ":email-local", "medium")
        labs = dom.split(".")
        if labs[0] not in ("gmail", "yahoo", "hotmail", "outlook", "googlemail", "qq", "163", "web", "gmx", "icloud", "aol", "live", "mail"):
            add(i, dom, where + ":email-domain")
    for m in URL_RX.finditer(text):
        u = m.group(0).rstrip(".,);:'\"")
        host = re.sub(r"^(?:https?://)?", "", u, flags=re.I).split("/")[0].split(":")[0].lower()
        if host.startswith("www."):
            host = host[4:]
        if not host or "." not in host or any(host == d or host.endswith("." + d) for d in VENDOR_DOMAINS):
            continue
        if INFRA_HOSTS.search(host):
            continue
        add(i, host, where + ":url-host")
        if host.count(".") >= 1:
            lab = host.split(".")[-2] if not re.search(r"\.(co|ac|com|org|edu|gov|net)\.[a-z]{2}$", host) else host.split(".")[-3]
            if len(lab) >= 4 and not generic_word(lab):
                add(i, lab, where + ":url-label", "medium")
    for m in PHONE_RX.finditer(text):
        ph = (m.group(1) or m.group(2) or "").strip()
        digits = re.sub(r"\D", "", ph)
        if 9 <= len(digits) <= 15 and not re.fullmatch(r"(19|20)\d{6}", digits) and len(set(digits)) > 2:
            add(i, ph, where + ":phone")
    for m in ADDR_RX.finditer(text):
        add(i, m.group(0).strip(), where + ":address", "medium")
    for rx, kind in ((COPY_RX, "copyright"), (PROPERTY_RX, "property-of")):
        for m in rx.finditer(text):
            h = m.group(1).strip(" .,-")
            if h and not is_generic(h) and not re.search(r"insert|company name|your company|placeholder", h, re.I) \
                    and not all(generic_word(w) for w in WORD.findall(h)) and len(h) <= 60:
                add(i, h, where + ":" + kind)
    for m in CORP_RX.finditer(text):
        h = m.group(0).strip()
        if re.search(r",\s*(AB|KK|K\.K\.|SA|SL|AG|Oy)$", h) or m.group(2) in ("AB", "Oy", "SA", "SL", "S.A", "S.L"):
            continue  # citation initials ("Harish, AB"), not a company
        if not all(generic_word(w) for w in WORD.findall(m.group(1))):
            add(i, h, where + ":corp", "medium")
    for m in WINUSER_RX.finditer(text):
        add(i, m.group(1), where + ":userpath")


def titleblock_identities(add, i, words):
    """Values next to title-block labels, from PyMuPDF words [p, x0, y0, x1, y1, w]."""
    by_page = collections.defaultdict(list)
    for w in words:
        by_page[w[0]].append(w)
    found = []
    for p, ws in by_page.items():
        ws.sort(key=lambda w: (round(w[2]), w[1]))
        n = len(ws)
        for k, w in enumerate(ws):
            # label of one or two words
            cand = [(norm(w[5]), k, k)]
            if k + 1 < n:
                cand.append((norm(w[5] + " " + ws[k + 1][5]), k, k + 1))
            for lab, a, b in sorted(cand, key=lambda c: -(c[2] - c[1])):
                lab = lab.rstrip(":")
                if lab not in LABEL_SET:
                    continue
                lw = ws[b]
                h = max(1.0, lw[4] - lw[2])
                yc = (lw[2] + lw[4]) / 2
                right = [x for x in ws if x is not lw and abs((x[2] + x[4]) / 2 - yc) < 0.6 * h and x[1] >= lw[3] - 0.5
                         and x[1] - lw[3] < 25 * h]
                right.sort(key=lambda x: x[1])
                val = []
                last_x = lw[3]
                for x in right:
                    nx = norm(x[5]).rstrip(":")
                    if x[1] - last_x > 8 * h and val:
                        break
                    if nx in NONVALUE or DATE_RX.match(x[5]):
                        if val:
                            break
                        continue
                    val.append(x)
                    last_x = x[3]
                    if len(val) >= 3:
                        break
                if not val:
                    below = [x for x in ws if 0 < x[2] - lw[4] < 1.6 * h and x[1] < lw[3] + 2 * h and x[3] > lw[1] - 2 * h]
                    below.sort(key=lambda x: x[1])
                    val = [x for x in below if norm(x[5]).rstrip(":") not in NONVALUE and not DATE_RX.match(x[5])
                           and (re.fullmatch(r"[A-Z]{2,4}", x[5]) or (x[5][:1].isupper() and not generic_word(x[5].strip(",.:"))))][:2]
                if not val:
                    break
                text = " ".join(x[5] for x in val).strip(" :")
                if not re.search(r"[A-Za-zÀ-ɏ一-鿿]", text) or len(text) > 40:
                    break
                ws_ = WORD.findall(text)
                if ws_ and all(generic_word(x) and not x.isupper() for x in ws_):
                    break
                if len(ws_) >= 3 and not all(x[:1].isupper() for x in ws_):
                    break  # running prose, not a name
                if any(x.lower() in ("and", "is", "to", "de", "of", "the", "for", "dieser", "der", "die", "das", "und") for x in ws_):
                    break
                if sum(c.isdigit() for c in text) >= 3 or text.lower().startswith(("for ", "of ", "the ", "to ")):
                    break  # part / drawing numbers, prose
                if all(x.isupper() and generic_word(x) and len(x) > 3 for x in ws_):
                    break  # an upper-case engineering word ("STEEL", "FINISH")
                found.append((lab, text, val))
                break
    for lab, text, val in found:
        add(i, text, "titleblock:" + lab, "strong")
    return found


