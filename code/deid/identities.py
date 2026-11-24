"""Which strings identify someone, derived from the input manifest plus curated lists.

Strong identifiers are replaced wherever they appear (paths and file contents).
Weak ones (generic words such as a repository called "Hardware") are replaced only in
the path component that names the account/project.
"""
import collections
import os
import re

HEX12 = re.compile(r"[0-9a-f]{12}")
ALNUM = re.compile(r"[a-z0-9]+")
SEP = re.compile(r"[^A-Za-z0-9]+")

# Software, formats and generic engineering words: never identifiers on their own.
STOP = set("""
freecad solidworks inventor autodesk catia fusion fusion360 onshape creo cadquery build123d
opencascade kicad blender solidedge autocad openscad github gitlab python arduino raspberry
nema17 stepper lego jetson nvidia step iges stl cad cam cnc pcb diy hardware mechanical
mechanics models model parts part drawings drawing design designs library libraries project
projects assembly public resources files robot robotics robotic gearbox gripper actuator
extruder enclosure bracket brackets printer openscad solvespace cadam microscope ventilator
spectrometer cryostat turbine engine manipulator quadruped humanoid keyboard keyboards
cycloidal antibacklash toolchanger turbofan gearbox planetary transmission differential
solid works edge siemens parasolid acis ptc dassault unigraphics kernel html https http www
3dmodels airquality arxiv assembly2 benchtop biorxiv bloodflow cadmodels cncbasics
coldstagedesign contenthub couplers crusher dimensioning dropbox dspace durablehinge dxcloud
episerver gearcutting highschool houseplan imagekit implantables indium mechanicaldesign
mechatronics mechd microfluidics minifigure networkcable openhardware opensource
plasmacutter portalmillingmachine portfoilo protoplate quadrotor remorque robotique
solidwork starterkit tecnico technic tokyo turbo turbomachine wordpress softjaws reductor
cilindric konstruktion kettenspanner ritzel dstorm shopify missouri kentucky website orion
pacer meerkat winder vademecum torsen upskill wpmucdn cambridge marco
""".split())
# Account-like names made only of everyday words: too generic to replace outside their folder.
PHRASE_STOP = set("website files|cnc network|design technology tutor|fresh teacher|fingers welt".split("|"))

# Field values that name nobody.
GENERIC_VALUES = set("""
none unknown unspecified author user default n/a na anonymous admin administrator owner
student pc guest root public operator test name company organization organisation
designer engineer drafter checker approver me nobody null undefined title subject nx ug
last first lab box occ occt sti cdi mr mr. mrs ms dr president manager staff employee
""".split()) | {"last name", "first name", "pc user", "your name", "your company", "company name"}
PLACEHOLDER = re.compile(
    r"(?:id|nauo-per|nauo-org|per|org|person|organization|user|author|company)[ _-]?\d*$|.*_(?:name|id)$"
    r"|(?:person|organization|org|user) id \d+$|.*(?:first|last)_?name$")
SOFTWARE_HINTS = (
    "solidworks", "autodesk", "freecad", "open cascade", "opencascade", "catia", "creo",
    "ptc inc", "siemens", "unigraphics", "inventor", "fusion", "onshape", "cadquery",
    "kicad", "blender", "st-developer", "acrobat", "adobe", "microsoft", "ghostscript",
    "pdf", "autocad", "bentley", "rhino", "sketchup", "spaceclaim", "ansys", "librecad",
    "qcad", "inkscape", "libreoffice", "openoffice", "office", "word", "excel", "writer",
    "printer", "scanner", "driver", "distiller", "quartz", "cairo", "matplotlib", "latex",
    "tex", "windows", "linux", "mac os", "step.parts", "software", "zwcad", "spatial",
    "corel", "bricscad", "ironcad", "alibre", "tinkercad", "shapr3d", "designspark", "t-flex",
    "kompas", "ascon", "mastercam", "geomagic", "hypermesh", "comsol", "datakit", "hoops",
    "coretechnologie", "tech soft", "opendesign", "teigha", "zw3d", "cimatron", "intergraph",
    "aveva", "tekla", "trimble", "graphisoft", "vectorworks", "nemetschek", "altium",
    "cadence", "mentor graphics", "easyeda", "eagle", "step tools", "cadexchanger", "zwsoft",
    "ezdxf", "librecad", "dxf", "cascade", "sdl-cambridge", "parasolid",
)

GENERIC_USERS = set("""
public administrator admin user users default owner operator shared guest root runner
ubuntu all\\ users appdata desktop documents downloads temp tmp local home lib share
uporabnik gebruiker utilisateur usuario benutzer utente administrador administrateur
""".split()) | {"all users", "default user"}

PUBLIC_MAIL = set("gmail googlemail yahoo hotmail outlook live msn icloud me qq 163 126 gmx web protonmail proton aol mail".split())
VENDOR_DOMAINS = set("3ds autodesk solidworks ptc siemens adobe w3 microsoft example apple google ugs".split())

# Curated identifiers come from the dataset being de-identified, so they live in a local
# file that is never committed: DEID_CURATED (default curated.tsv in the working folder).
# One identifier per line, optionally followed by a tab and a flag:
#   case   short acronym, matched case-sensitively and replaced in paths only
#   inner  acronym also replaced inside longer tokens of PDF text (DEID_PDF_INNER)
# See curated.example.tsv.
def _curated_list(path=None, kind=None):
    """(text, case_sensitive) pairs, or with kind="inner" just the texts flagged inner."""
    path = path or os.environ.get("DEID_CURATED", "curated.tsv")
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            text, _, flag = line.rstrip("\n").partition("\t")
            if kind is None and flag != "inner":
                out.append((text, flag == "case"))
            elif kind == flag:
                out.append(text)
    return out


CURATED = _curated_list()

TWO_LEVEL = set("co ac edu org gov com net".split())
# Shared hosting suffixes: the identifying label is the one in front of them.
HOSTING = set("""github.io gitlab.io github.com wordpress.com blogspot.com amazonaws.com
cloudfront.net sharepoint.com googleusercontent.com weebly.com wixsite.com readthedocs.io
netlify.app herokuapp.com web.app""".split())


def load_english(path="/usr/share/dict/words"):
    if not os.path.isfile(path):
        raise SystemExit("Missing %s (install the 'wamerican' package)." % path)
    with open(path, encoding="utf-8", errors="replace") as f:
        return {w.strip() for w in f if w.strip().islower()}


def read_manifest(path):
    rows = []
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            cols = line.rstrip("\n").split("\t")
            rows.append((cols[0], int(cols[1])))
    return rows


def project_tokens(group, third):
    """Identifying tokens of a project folder name: list of (token, is_account)."""
    if group in ("github", "archives"):
        name = third[:-4] if group == "archives" and third.lower().endswith(".zip") else third
        toks = [t for t in name.split("__") if t and not HEX12.fullmatch(t)]
        return [(t, i == 0) for i, t in enumerate(toks)]
    if group == "gitlab":
        toks = [t for t in third.split("__") if t and t != "gitlab.com"]
        return [(t, i == 0) for i, t in enumerate(toks)]
    return []


def domain_labels(name):
    """Split a host name into (identifying labels, kept suffix labels); None if not a host."""
    if not re.fullmatch(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+", name):
        return None
    labels = name.split(".")
    if not labels[-1].isalpha():
        return None
    keep = 2 if len(labels) > 2 and labels[-2].lower() in TWO_LEVEL and len(labels[-1]) == 2 else 1
    if len(labels) > 2 and ".".join(labels[-2:]).lower() in HOSTING:
        keep = 2
    return labels[:-keep], labels[-keep:]


class Identities:
    def __init__(self, manifest_path, english=None, extra=None):
        """extra: file of 'kind<TAB>value' identities found inside files by an earlier
        pass (see tools/merge_found.py); each becomes a strong identifier."""
        english = load_english() if english is None else english
        self.rare = set()  # every word seen in an identity value: reserved pseudonyms only
        self.english = english
        self.rows = read_manifest(manifest_path)
        cand = collections.defaultdict(set)      # word -> owners it was derived from
        phrases = {}                              # lowercase phrase -> is_account
        owners_with_word = collections.defaultdict(set)
        seen_third = set()
        for path, _size in self.rows:
            parts = path.split("/")
            if parts[0] == "archives":
                group, third, rest = "archives", parts[1], []
            elif len(parts) >= 3:
                group, third, rest = parts[1], parts[2], parts[3:]
            else:
                continue
            owner = group + "/" + third.split("__")[0].lower()
            for comp in rest:
                for w in ALNUM.findall(comp.lower()):
                    if len(w) >= 5:
                        owners_with_word[w].add(owner)
            if (group, third) in seen_third:
                continue
            seen_third.add((group, third))
            toks = project_tokens(group, third)
            if group in ("web", "catalog"):
                dl = domain_labels(third)
                if dl:
                    toks = [(lab, True) for lab in dl[0] if lab.lower() != "www"]
            for tok, is_account in toks:
                words = ALNUM.findall(tok.lower())
                for w in words:
                    cand[w].add(owner)
                if len(words) > 1 and sum(map(len, words)) >= 8:
                    phrases[tok.lower()] = phrases.get(tok.lower(), False) or is_account
        self.words = set()
        for w, owners in cand.items():
            if len(w) < 5 or w.isdigit() or w in english or w in STOP:
                continue
            if w.endswith("s") and (w[:-1] in english or w[:-1] in STOP):
                continue
            if len(owners_with_word.get(w, set()) - owners) >= 3:
                continue  # used by several unrelated projects: a generic word
            self.words.add(w)
        self.phrases = set()
        for ph, is_account in phrases.items():
            words = ALNUM.findall(ph)
            if not (is_account or any(w in self.words for w in words)):
                continue
            if " ".join(words) in PHRASE_STOP:
                continue
            for sep in (None, "-", "_", " "):
                self.phrases.add(ph if sep is None else sep.join(words))
        self.path_only = set()                    # case-sensitive, exact
        for text, case_sensitive in CURATED:
            if case_sensitive:
                self.path_only.add(text)
            elif SEP.search(text):
                self.phrases.add(text.lower())
                if "." not in text:
                    words = [w for w in SEP.split(text.lower()) if w]
                    self.phrases.update(sep.join(words) for sep in ("-", "_", " "))
            else:
                self.words.add(text.lower())
        # Names found only in the text of PDFs (title blocks, named entities) are replaced in
        # file contents but never rename a file or folder: a wrong guess there would move
        # files whose path names nobody.
        self.content_only = set()
        if extra:
            from_pdf, elsewhere = set(), set()
            with open(extra, encoding="utf-8", errors="surrogateescape") as f:
                for line in f:
                    kind, _, value = line.rstrip("\n").rpartition("\t")
                    value = value.strip().lower()
                    if not value:
                        continue
                    if kind == "seen":  # not an identifier to search for: reserve only
                        self.rare.update(ALNUM.findall(value))
                        continue
                    forms = {value}
                    if SEP.search(value):
                        words = [w for w in SEP.split(value) if w]
                        if "." not in value and len(words) > 1:
                            forms.update(sep.join(words) for sep in ("-", "_", " "))
                    (from_pdf if kind.startswith("pdf-") else elsewhere).update(forms)
            known = self.words | self.phrases
            for value in from_pdf | elsewhere:
                (self.phrases if SEP.search(value) else self.words).add(value)
            self.content_only = from_pdf - elsewhere - known
        self.phrases -= self.words

    def all_words(self):
        """Every word that can be pseudonymized as part of a known identifier."""
        out = set(self.rare)
        for group in (self.words, self.phrases, self.path_only):
            for item in group:
                out.update(ALNUM.findall(item.lower()))
        for path, _size in self.rows:
            parts = path.split("/")
            name = parts[1] if parts[0] == "archives" else (parts[2] if len(parts) > 2 else "")
            out.update(ALNUM.findall(name.lower()))
        return out

    def patterns(self):
        """(lowercase latin-1 pattern, exact-case bytes or None) for the content scanner."""
        out = []
        for p in sorted(self.words | self.phrases):
            out.append((p.encode("utf-8").decode("latin-1"), None))
        return out

    def path_patterns(self):
        out = [(p.encode("utf-8").decode("latin-1"), None)
               for p in sorted((self.words | self.phrases) - self.content_only)]
        for p in sorted(self.path_only):
            out.append((p.lower(), p.encode("utf-8")))
        return out
