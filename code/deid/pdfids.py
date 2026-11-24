"""Identity strings in the page text of a PDF.

find(pdf_path) -> list[str]            strings to add to the global identity map
find_detailed(pdf_path) -> list[(kind, string)]   same, with the kind of each string

What is looked for (all local, no outside services):
  * title-block fields found by label proximity: a label (DRAWN BY, CHECKED, APPROVED,
    ENGINEER, COMPANY, CUSTOMER, PART NO., DWG NO., PROJECT, ADDRESS, TEL, E-MAIL, ...,
    also German, French, Spanish, Italian) and the value to its right or below it, or after
    a colon on the same line, measured in the reading frame of the label's own text so that
    rotated pages and vertical text work;
  * regular expressions: e-mail addresses, URLs, phone numbers, street addresses, copyright
    holders;
  * a local spaCy NER model (en_core_web_md, else _sm) over the first pages for PERSON and
    ORG names in running text (authors, acknowledgements, affiliations).

Kinds: person, org, part, project, email, url, phone, address. Granularity matches how the
identity map uses the strings: e-mails and URLs give their identifying words (local part,
host labels, account and repository of a code-hosting URL), not the whole address, so a
replacement never rewrites "https" or ".com". Bare initials ("BJD", "KS") are only in
find_detailed (kind "initials"): too short to replace everywhere in a corpus.
Never raises: an unreadable PDF gives [].
"""
import math
import os
import re
import unicodedata

from .identities import GENERIC_VALUES, PUBLIC_MAIL, STOP, VENDOR_DOMAINS
from .scan import is_generic

MAX_PAGES = 60          # pages searched for labels and patterns
NER_CHARS = 24000       # characters of text given to the NER model per file
NER_MIN_CHARS = 15

# --------------------------------------------------------------------------- labels

PERSON = """drawn by|drawn|drawn/checked|drawn / checked|draw by|drn|dwn|drwn|drawer|drafted by|
draftsman|drafter|draughtsman|designed by|designed|designer|design by|design engineer|checked by|
checked|checker|chk'd|chkd|chk|chkd by|chk by|chk'd by|approved by|approved|approver|appr|apprv'd|
apprvd|appvd|apvd|app'd|appv'd|engineer|engineered by|eng|engr|eng appr|author|authors|created by|
creator|prepared by|prepared|reviewed by|reviewer|released by|issued by|revised by|modified by|
originator|originated by|owner|contact|contact person|manager|project manager|project engineer|
supervisor|signed|signature|responsible|verified by|tested by|inspected by|mfg eng|mfg engr|prod eng|
dept head|name|written by|compiled by|edited by|submitted by|presented by|
gezeichnet|gez|gezeichnet von|gezeichnet durch|geprüft|geprueft|gepr|geprüft von|genehmigt|freigegeben|
bearbeiter|bearbeitet|bearb|konstrukteur|konstruiert|ersteller|erstellt von|zeichner|verfasser|
dessiné par|dessinateur|dessiné|vérifié par|vérifié|verifie|contrôlé|contrôlé par|approuvé|approuvé par|
validé|auteur|créé par|ingénieur|conçu par|dibujado|dibujó|dibujado por|revisado|revisó|aprobado|
diseñado|diseñado por|ingeniero|proyectista|disegnato|disegnato da|controllato|approvato|progettato|
verificato|desenhado|verificado|aprovado|projetado|getekend|gecontroleerd|goedgekeurd|ontworpen|
ritad|kontrollerad|godkänd|konstruerad|rysował|sprawdził|zatwierdził|kreslil|zkontroloval|schválil
""".replace("\n", "")
WEAK_PERSON = {"name", "owner", "contact", "manager", "signed", "signature", "responsible", "design",
               "prepared", "authors", "author", "creator", "eng", "engineer", "supervisor", "approved",
               "checked", "appr", "chk", "drawn", "designed", "designer", "eng appr", "reviewer", "approver"}
# these are strong when the line is only the label or has a colon; weak means "needs name-like value"
ORG = """company|company name|customer|client|manufacturer|manufactured by|made by|supplier|vendor|
unternehmen|firma|kunde|auftraggeber|hersteller|société|entreprise|fabricant|empresa|cliente|
fabricante|azienda|produttore|proprietary to|property of|courtesy of|provided by|supplied by|
sponsored by|funded by|affiliation|company/institution|org|organisation name""".replace("\n", "")
PART = """part no|part number|part #|part num|part nr|p/n|p\\.n|pn|part|teil-nr|teilenummer|sachnummer|
artikelnummer|art\\.-nr|art no|article no|item no|item number|item|dwg no|dwg number|dwg #|dwg|
drawing no|drawing number|drawing #|drawing|doc no|doc number|document no|document number|
document|number|no|nr|zeichnungsnummer|zeichn\\.-nr|zeichnungs-nr|plan no|plan number|
drawing id|dwg id|model no|model number|model|assy no|assembly no|assembly|catalog number|cat no|
sku|stock no|identification no|identification number|id no|id|numéro de plan|
n° de plan|n° de pièce|numero de pieza|numero disegno|job no|job number|order no|order number|
work order|po|po no|purchase order|contract no|contract number|serial no|serial number|s/n""".replace("\n", "")
WEAK_PART = {"number", "no", "nr", "item", "part", "assembly", "model", "document", "drawing", "id", "pn",
             "po", "job no", "order no", "serial no"}
PROJECT = """project|project name|project title|project no|project number|program|programme|product|
product name|job|job name|application|projekt|projet|proyecto|progetto|system""".replace("\n", "")
PHONE = "phone|tel|telephone|tel no|phone no|fax|mobile|mob|cell|telefon|téléphone|telefono|telefax|handy"
EMAIL = "e-mail|email|e mail|mail|courriel"
WEB = "web|website|www|url|homepage|internet|site web"
ADDRESS = """address|addr|street|adresse|anschrift|dirección|direccion|indirizzo|endereço""".replace("\n", "")
WEAK_PROJECT = {"product", "application", "system", "job", "program", "programme", "product name", "job name"}
WEAK_CONTACT = {"mob", "cell", "mail", "web", "www", "tel"}

_LABEL_CLASS = {}
for cls, spec in (("person", PERSON), ("org", ORG), ("part", PART), ("project", PROJECT),
                  ("phone", PHONE), ("email", EMAIL), ("web", WEB), ("address", ADDRESS)):
    for lab in spec.split("|"):
        lab = lab.strip().replace("\\", "").lower()
        if lab:
            _LABEL_CLASS.setdefault(lab, cls)
_ALT = sorted({re.escape(k).replace(r"\ ", r"\s+") for k in _LABEL_CLASS}, key=len, reverse=True)
_LABEL_RX = re.compile(r"(?<![\w'’./-])(" + "|".join(_ALT) + r")(?=[\s:=.]|$)\.?[ \t]*[:=]?", re.I)
_SEP = " \t:=.-–_|/\\,;"
_WEAK = WEAK_PERSON | WEAK_PART | WEAK_PROJECT | WEAK_CONTACT


def _label_class(text):
    key = re.sub(r"\s+", " ", text.lower().replace("’", "'")).strip(" :=.")
    return _LABEL_CLASS.get(key), key


# --------------------------------------------------------------------------- word lists

ENGINEERING = set("""
scale sheet date rev revision material finish title size unit units weight mass tolerance tolerances
unless otherwise specified dimensions dimension mm inch inches degrees all break sharp edges deburr
remove burrs surface roughness general drawing drawings part parts assembly quantity qty item
description number no yes none tbd tba na nil see note notes below above front back top bottom left
right side view section detail typ typical ref max min dia radius thread thru through hole holes
steel aluminum aluminium stainless plate bar rod tube bracket shaft gear bearing housing cover base
frame anodize anodized black white red blue green clear plated plating coating paint powder heat
treat treated hardness rockwell hrc ra rms iso din ansi asme astm jis sae nema ieee iec en bs gb
first angle projection third metric imperial approved checked drawn designed engineer released
proprietary confidential property copyright rights reserved sample unspecified specified default
blank empty page pages of and the for with from not this that revised review reviewed release
produktion production fecha datum date data blatt hoja feuille foglio massstab maßstab escala
échelle scala werkstoff materiale matériau oberfläche surface superficie gewicht poids peso titel
titre título titolo zeichnung plan plano dessin disegno benennung bezeichnung designation
nummer numéro número änderung index version versión ausgeschlossene kanten bohrungen getriebe
matlab simulink python ansys comsol labview arduino raspberry linux windows excel word
sheet1 solidworks inventor fusion autocad creo catia kicad eagle altium
generated created modified updated printed plotted file filename path folder
standard standards assembly assemblies component components module modules system systems
table figure fig tab chapter section appendix reference references
contact email phone tel fax address telephone mobile
kontrolliert gezeichnet geprüft genehmigt freigegeben bearbeitet erstellt kontrolle datum
contrôlé dessiné vérifié approuvé controllato disegnato approvato revisado dibujado aprobado
rod rods guide guides coupler couplings clamp clamping flexure feature features motor motors stepper
screw screws setscrew bolt bolts nut nuts washer pulley belt gear gears shaft sleeve bushing cylinder
piston valve pump spring springs housing enclosure bracket plate plates block blocks mount mounting
assembly subassembly conrod crosshead highlight kit tool tools fixture adapter adaptor extruder
nozzle heater sensor sensors cable cables connector connectors board boards module controller driver
next assy mfg elec mech proj appd qa board name specifications blatt seite cm date.
""".split())
_STD_ORGS = set("""iso din ansi asme astm jis sae nema ieee iec bsi gb ul csa ce rohs reach nist osha
fda epa dod nasa mil asm asq ncsl aws api ihs""".split())
_PARTICLES = {"van", "von", "de", "der", "den", "di", "da", "del", "della", "la", "le", "bin", "al", "el",
              "ben", "ter", "ten", "du", "dos", "das", "y", "e", "af", "av", "zu", "du", "des", "st", "mc", "mac"}
_FUNC = {"the", "of", "and", "to", "is", "are", "was", "for", "with", "in", "on", "at", "by", "that",
         "this", "be", "or", "as", "an", "it", "if", "not", "can", "will", "may", "should", "must",
         "which", "from", "into", "than", "then", "also", "only", "any", "all", "no", "its", "has", "have"}
_PUBLIC_HOSTS = set("""github.com gitlab.com bitbucket.org sourceforge.net github.io gitlab.io
wordpress.com blogspot.com readthedocs.io""".split())
_SKIP_HOSTS = set("""w3.org xmlns.com purl.org creativecommons.org gnu.org apache.org mozilla.org
adobe.com microsoft.com google.com apple.com youtube.com facebook.com twitter.com linkedin.com
instagram.com wikipedia.org wikimedia.org doi.org arxiv.org amazon.com amzn.to iso.org
ieee.org acm.org springer.com elsevier.com sciencedirect.com wiley.com nature.com
openscad.org freecad.org solidworks.com autodesk.com 3ds.com ptc.com siemens.com
python.org pypi.org opensource.org example.com example.org localhost asme.org astm.org ansi.org
din.de nist.gov nasa.gov osha.gov ul.com sae.org ieee.org iec.ch""".split())
_GENERIC_LABELS = set("""www ftp mail web docs cdn static media files download downloads store shop
support help blog news forum wiki api app apps portal members login secure ssl img images
components products product catalog resources resource assets content data cloud online""".split())
_TLD = ("com|org|net|edu|gov|io|de|fr|uk|it|ch|es|nl|se|no|dk|fi|pl|cz|at|be|jp|cn|in|ru|br|au|ca|"
        "us|eu|info|me|co|ac|tw|kr|tech|dev|si|pt|gr|hu|ie|nz|za|mx|ar|cl|tr|il|sg|hk|cc|ly|sh|app|"
        "xyz|cloud|science|lab|shop|store|design")
_GIVEN = set("""
james john robert michael william david richard joseph thomas charles chris christopher daniel matthew
anthony mark donald steven paul andrew joshua kenneth kevin brian george timothy ronald edward jason
jeffrey ryan jacob gary nicholas eric jonathan stephen larry justin scott brandon benjamin samuel
gregory alexander frank patrick raymond jack dennis jerry tyler aaron jose adam nathan henry douglas
zachary peter kyle walter ethan jeremy harold keith christian roger noah gerald carl terry sean
austin arthur lawrence jesse dylan bryan joe jordan billy bruce albert willie gabriel logan alan juan
wayne roy ralph randy eugene vincent russell elijah louis bobby philip johnny bradley mary patricia
jennifer linda elizabeth barbara susan jessica sarah karen nancy lisa betty margaret sandra ashley
kimberly emily donna michelle carol amanda dorothy melissa deborah stephanie rebecca sharon laura
cynthia kathleen amy angela shirley anna brenda pamela emma nicole helen samantha katherine
christine debra rachel carolyn janet catherine maria heather diane olivia julie joyce victoria
kelly christina lauren joan evelyn judith megan cheryl andrea hannah martha jacqueline frances
gloria ann teresa kathryn sara janice jean alice madison doris abigail julia judy grace denise amber
marilyn beverly danielle theresa sophia marie diana brittany natalie isabella charlotte rose alexis
kayla hans peter klaus jürgen juergen wolfgang dieter helmut uwe bernd thomas stefan andreas markus
michael matthias sebastian florian tobias lukas jan jens christoph christian martin werner heinz
günter gunter horst manfred rainer ralf jörg joerg karl kurt otto fritz ernst franz gerhard walter
herbert rolf holger frank sabine petra monika susanne claudia birgit andrea ute renate ingrid
brigitte karin heike angelika anja katrin nicole jean pierre michel philippe alain jacques bernard
marcel andré andre françois francois laurent nicolas stéphane stephane olivier thierry sylvie
isabelle nathalie sophie catherine françoise francoise monique brigitte juan carlos josé jose luis
antonio manuel francisco javier miguel pedro pablo jorge alberto fernando ricardo sergio rafael
diego daniel mario marco giuseppe giovanni luigi paolo andrea alessandro francesco roberto stefano
luca matteo lorenzo davide simone fabio massimo claudio enrico lei li wei wang zhang chen yang liu
hui jun ming xin yan yu jie hao xiao ying lin ahmed mohamed mohammed ali omar hassan hussein ibrahim
rahul amit raj ravi vijay sanjay anil sunil ramesh suresh arun deepak ajay ashok manoj vinod
anna eva ewa maja jana olga irina natalia tatiana elena sergei sergey ivan dmitri dmitry alexei
alexey vladimir nikolai nikolay boris pavel andrei yuri yuriy oleg igor max leon felix lars erik
sven nils olof anders johan bjorn björn henrik mikael tor knut ole hugh dave rob bob bill tim tom jim
jeff mike steve matt ben sam dan don ron ray lee tony andy nick josh pat sue kim rick eddie
""".split())
_FOREIGN_STOP = set("""
von und der die das mit für nach bei aus auf zum zur oder nicht ohne über unter ein eine einer
le la les des du de et en pour avec sans par sur dans une un el los las del para con por una
il lo gli di da del della delle per con senza il het een voor met zonder op uit
""".split())
_LEAD_DROP = set("""usa uk germany france italy spain canada china japan india europe asia america
consult contact visit see from at by with the our your their its new old advanced international
global national american european chinese japanese german british french italian""".split()) - {"advanced"}
_EN_STOP = set("the of and to in is for with that this are be on as by it from at an or not can will which".split())
_OTHER_STOP = set("""der die das und ist nicht mit für von zu den dem des ein eine einen einem auf im auch sich als bei oder
wird werden nach aus über durch sie wir sind wenn dann kann beim zum zur
le la les des du et est pour dans avec sur par une un au aux ce cette qui que ne pas plus sont
el los las del y es para con por una en se no como más pero su sus al
il lo gli dei delle e è che non come più sono per
o os as do da dos das é uma em não mais
de het een en van voor met op zijn niet""".split())
_ENGLISH = None


def _foreign_text(lines_text):
    """True when the text is mostly not English (the English NER model is unreliable then)."""
    en = other = 0
    for t in lines_text:
        for w in re.findall(r"[^\W\d_]+", t.lower()):
            if w in _EN_STOP:
                en += 1
            elif w in _OTHER_STOP:
                other += 1
    return other >= 4 and other > 1.2 * en


def _english():
    global _ENGLISH
    if _ENGLISH is None:
        try:
            with open("/usr/share/dict/words", encoding="utf-8", errors="replace") as f:
                _ENGLISH = {w.strip() for w in f if w.strip().islower()}
        except OSError:
            _ENGLISH = set()
    return _ENGLISH


# Drawing, title-block and instruction words in German, Spanish, French, Italian and Catalan, and
# shop English the system word list lacks. A candidate made only of such words names nobody.
DRAWING_WORDS = set("""
hoja hojas pieza piezas plano planos conjunto subconjunto denominación denominacion cantidad acabado tratamiento
tolerancia tolerancias comprobado fabricado proyecto nombre firma estado observaciones referencia código codigo
unidades medidas cotas vista corte detalle tornillo tuerca arandela eje soporte placa tapa carcasa rodamiento
muelle chapa perfil taladro rosca espesor longitud anchura altura contacto cilindro cilindros módulo modulo
schräge schraege rundung nut passung senkung abstand bereich richtung seite mitte oben unten links rechts vorne
hinten innen außen aussen maß masse form lage größe groesse anzahl bemerkung bemerkungen aenderung freigabe prüfung
pruefung ersatz ursprung zustand ausführung ausfuehrung norm werk abteilung bearbeitung behandlung härte haerte
rauheit naht kontur fläche flaeche linie punkt ebene körper koerper teil teile gruppe übersicht uebersicht liste daten
st pass fail passed failed canister cannister kanban collet collets chuck chucks interferometer interferometers flexures standoff standoffs
heatsink gantry extrusion toolhead endstop hotend leadscrew ballscrew gusset shim spacer dowel grommet
sie platzieren bitte hinweis hinweise allgemeine allgemein vereinfachte baugruppe baugruppen bauteil bauteile
bauteilnummer stückliste stueckliste zeichnungsnummer halbzeug rohteil blätter ansicht schnitt einzelheit kante
bohrung gewinde fase durchmesser länge breite höhe dicke tiefe oberflächen toleranz toleranzen menge stück stueck
pos position zusammenbau montage drehen fräsen fraesen bohren schleifen schweißen schweissen biegen entgraten
gratfrei skizze tabelle profil welle achse lager schraube mutter scheibe platte halter deckel gehäuse gehaeuse
rahmen hebel feder stift bolzen buchse flansch blech rohr ring winkel träger traeger leiste klemme stange schieber
exzenter verlag stahl edelstahl kunststoff messing kupfer verzinkt eloxiert gehärtet gehaertet entlang ausschnitt
feuilles pièce pièces piece ensemble désignation quantité quantite echelle matière matiere finition traitement
tolérance tolérances nom indice modification vue coupe détail vis écrou ecrou rondelle axe support plaque capot
carter roulement ressort tôle tole profilé perçage percage taraudage épaisseur epaisseur longueur largeur hauteur
ecrite présentation presentation
fogli pezzo pezzi complessivo assieme denominazione quantità quantita finitura trattamento tolleranza tolleranze
nome stato sezione dettaglio vite dado rondella albero supporto piastra coperchio cuscinetto molla lamiera
profilo foratura filettatura spessore lunghezza larghezza altezza
full fulls peça peces conjunt subconjunt denominació quantitat acabat tractament tolerància toleràncies dibuixat
comprovat revisat aprovat fabricat projecte signatura estat observacions referència codi unitats mides cotes tall
detall cargol femella volandera eix suport carcassa rodament xapa trepant gruix amplada alçada preparació procés
definició
""".split())
ENGINEERING |= DRAWING_WORDS
# last part of a German compound noun: "Bauteilnummer", "Materialschnitt", "Grundkörper" are drawing words
_DE_TAIL = re.compile(
    r"(?:nummer|gruppen?|teile?n?|zeichnung(?:en)?|schnitte?|fl(?:ä|ae)chen?|wellen?|k(?:ö|oe)rper|daten|darstellung|"
    r"st(?:ü|ue)ck|platten?|halter(?:ung)?|achsen?|lager(?:ung)?|schrauben?|scheiben?|geh(?:ä|ae)use|ansicht(?:en)?|"
    r"liste|bleche?|rohre?|bolzen|federn?|muttern?|stangen?|tr(?:ä|ae)ger|winkel|deckel|rahmen|hebel|buchsen?|flansch|"
    r"motor|antrieb|getriebe|f(?:ü|ue)hrung|aufnahme|leisten?|klemmen?|stifte?|profile?|einheit|werkzeuge?|vorrichtung|"
    r"(?:ü|ue)bersicht|tabelle|hinweise|laschen?|stellung|toleranz(?:en)?|kanten?|bohrung(?:en)?|gewinde|verbindung|"
    r"befestigung|abdeckung|dichtung|kupplung|st(?:ü|ue)tze|schlitten|schere|kolben|zylinder|ventil|pumpe|schalter|"
    r"stecker|kabel|adapter)$")
_VOCAB = None


def _vocab():
    """(lower-case words, capitalised German entries in lower case): the English word list, the
    word lists named in DEID_WORDLISTS (files or directories, e.g. the wngerman, wspanish,
    wfrench, witalian, wcatalan lists) and the words that the file named in DEID_WORD_DF
    (tools/word_df.py output) counts in 10 or more projects. Both variables are optional."""
    global _VOCAB
    if _VOCAB is None:
        english = set(_english())
        lower, caps, paths = set(), set(), []
        for p in os.environ.get("DEID_WORDLISTS", "").split(os.pathsep):
            if os.path.isdir(p):
                paths += [os.path.join(p, n) for n in sorted(os.listdir(p))]
            elif p:
                paths.append(p)
        seen = set()
        for p in paths:
            real = os.path.realpath(p)
            if real in seen or not os.path.isfile(real):
                continue
            seen.add(real)
            german = "german" in os.path.basename(p) or "swiss" in os.path.basename(p)
            try:
                with open(real, encoding="utf-8", errors="replace") as f:
                    for w in f:
                        w = w.strip()
                        if not w or "'" in w:
                            continue
                        if w.islower():
                            lower.add(w)
                        elif german:
                            caps.add(w.lower())
            except OSError:
                continue
        df = os.environ.get("DEID_WORD_DF", "")
        if df:
            try:
                with open(df, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        cols = line.rstrip("\n").split("\t")
                        if len(cols) >= 2 and cols[1].isdigit() and int(cols[1]) >= 10:
                            lower.add(cols[0].lower())
            except OSError:
                pass
        proper = set()     # "California", "Maxwell": names in English, whatever another list says
        try:
            with open("/usr/share/dict/words", encoding="utf-8", errors="replace") as f:
                proper = {w.strip().lower() for w in f if w[:1].isupper()} - english
        except OSError:
            pass
        lower = english | (lower - proper)
        _VOCAB = (lower, caps - _GIVEN - lower - proper)
    return _VOCAB


def _is_word(t, nouns=True):
    """True when t (lower case) is an ordinary word of English, German, Spanish, French, Italian
    or Catalan, a drawing term or a German compound noun: no name on its own. nouns=False leaves
    out the capitalised German entries (family names such as Fischer or Koch are nouns too)."""
    lower, caps = _vocab()
    if t in lower or t in ENGINEERING or (nouns and t in caps):
        return True
    for suf in ("s", "es"):
        if len(t) > len(suf) + 3 and t.endswith(suf) and (t[:-len(suf)] in lower or nouns and t[:-len(suf)] in caps):
            return True
    return len(t) >= 9 and bool(_DE_TAIL.search(t))


# --------------------------------------------------------------------------- regexes

_EMAIL = re.compile(r"(?<![\w.+-])([A-Za-z0-9][A-Za-z0-9._%+-]{0,40})@((?:[A-Za-z0-9-]{1,40}\.)+[A-Za-z]{2,10})(?![\w@])")
_URL = re.compile(r"(?i)\b((?:https?://|ftp://|www\.)[^\s<>\"'()\[\]{}|\\^`]+|"
                  r"(?:[a-z0-9-]{2,40}\.){1,3}(?:%s)\b(?:/[^\s<>\"'()\[\]{}|\\^`]*)?)" % _TLD)
_PHONE = re.compile(
    r"(?<![\w/.-])(?:\+\d{1,3}[ .-]?(?:\(\d{1,4}\)[ .-]?)?\d{1,4}(?:[ .-]?\d{2,4}){2,4}|"
    r"\(\+*\d{1,3}\)[ .-]?\d{1,3}\.?[ .-]?\d{2}[ .-]?\d{2}[ .-]?\d{2}[ .-]?\d{2}|"
    r"\(\d{2,4}\)[ .-]?\d{3,4}[ .-]?\d{3,4}|"
    r"\d{3}[ .-]\d{3}[ .-]\d{4}|0\d{1,4}[ /-]\d{3,4}[ .-]?\d{3,5})(?![\w/-])")
_STREET_END = r"Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way|Court|Ct|Place|Pl|Highway|Hwy|Parkway|Pkwy"
_STREET_US = re.compile(r"\b(\d{1,5}[ \t]+(?:[NSEW]\.?[ \t]+)?(?:[A-Z][A-Za-z.'-]+[ \t]+){1,3}(?:%s)\b\.?)" % _STREET_END)
_STREET_EU = re.compile(
    r"\b((?:Rue|Via|Viale|Calle|Avenida|Avenue|Carrer|Piazza|Plaza|Chemin|Boulevard|Allee)"
    r"[ \t]+(?:de[ \t]+|di[ \t]+|del[ \t]+|des[ \t]+|la[ \t]+|du[ \t]+)?[A-Z][\w.'-]+(?:[ \t]+[A-Z][\w.'-]+){0,2}[ ,]+\d{1,4}[a-z]?)")
_STREET_FR = re.compile(r"\b(\d{1,4}(?:\s?bis)?,?\s+(?:rue|avenue|boulevard|chemin|allée|impasse)\s+[\w'’ -]{3,40})", re.I)
_STREET_DE = re.compile(r"\b([A-ZÄÖÜ][a-zäöüß]+(?:stra(?:ß|ss)e|straße|platz|weg|allee|gasse|ring|damm|ufer)[ \t]+\d{1,4}[a-z]?)\b")
_BOX = re.compile(r"\b(P\.?\s?O\.?\s+Box\s+\d{1,6})", re.I)
_ZIPCITY = re.compile(r"(?:^|, ?)((?:[A-Z]{1,2}-)?\d{4,5}(?:-\d{4})?[ \t]+[A-ZÄÖÜ][a-zäöüéèàç]+(?:[ -][A-ZÄÖÜ][a-zäöüéèàç]+){0,2})(?=$|,)", re.M)
_USZIP = re.compile(r"\b([A-Z][a-z]+(?:[ \t][A-Z][a-z]+)?,[ \t]+[A-Z]{2}[ \t]+\d{5}(?:-\d{4})?)\b")
_COPY = re.compile(r"(?:©|\(c\)|copyright)\s*(?:\(c\)\s*)?(?:19|20)\d\d(?:\s*[-–,]\s*(?:19|20)?\d\d)*[ ,.:]*"
                   r"(?:by\s+)?([A-Z][^\n.;|©]{2,60}?)(?=\s*(?:\.|;|\||,?\s+all\s+rights|,?\s+under|,?\s+licen[cs]ed|\n|$))",
                   re.I)
_ORGSUF = (r"University|Universit[äaéy]t?|Universiteit|Universidad|Universidade|Università|Institute|Institut|"
           r"Laboratory|Laboratories|Laboratoire|Laboratorium|College|Corporation|Corp\.?|Inc\.?|Ltd\.?|LLC|GmbH|"
           r"AG|S\.?A\.?S?|S\.?r\.?l\.?|B\.?V\.?|Co\.|Company|Foundation|Fondation|Stiftung|Association|Associates|"
           r"Industries|Technologies|Technology|Systems|Instruments|Robotics|Electronics|Manufacturing|Engineering|"
           r"Labs|Studio|Works|Solutions|Group|Aerospace|Dynamics|Machines|Machining")
_ORG_RX = re.compile(r"\b((?:[A-Z][\w&'.-]+[ \t]+){1,4}(?:%s))(?![\w])" % _ORGSUF)
_ORG_RX_UP = re.compile(r"\b((?:[A-Z][A-Z0-9&'.-]+[ \t]+){1,4}(?:%s))(?![\w])" % _ORGSUF.upper().replace("\\.?", "\\.?"))
_ORG_OF = re.compile(r"\b((?:University|Institute|Universität|Université|Universidad|Technische Universit[äa]t|"
                     r"College|School)[ \t]+(?:of[ \t]+|für[ \t]+|de[ \t]+|di[ \t]+|du[ \t]+)?[A-Z][a-zäöüéèà-]+(?:[ \t]+[A-Z][a-zäöüéèà-]+){0,3})")
_DATE = re.compile(r"^\d{1,4}[./-]\d{1,2}[./-]\d{1,4}$|^\d{1,2}[./-]\d{2,4}$|^\d{1,2}(st|nd|rd|th)?$|"
                   r"^(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t(ember)?)?|oct(ober)?|nov(ember)?|dec(ember)?)[.,]?$|^(19|20)\d\d$|^\d+/\d+$|"
                   r"^(yy|yyyy|mm|dd|yyyy[./-]mm[./-]dd|dd[./-]mm[./-]yy(yy)?|mm[./-]dd[./-]yy(yy)?)$", re.I)
_ALPHAWORD = re.compile(r"^[^\W\d_][\w.'’-]*\.?$", re.U)


# --------------------------------------------------------------------------- helpers

def _norm(s):
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s).strip()


def _tok(t):
    return t.lower().strip(".,;:'’()[]")


def _generic_phrase(s):
    if is_generic(s):
        return True
    toks = re.findall(r"[^\W\d_]+", s.lower())
    return bool(toks) and all(t in ENGINEERING or t in GENERIC_VALUES or t in STOP or t in _FUNC for t in toks)


def _strip_dates(s):
    return " ".join(t for t in s.split() if not _DATE.match(t.strip(".,;:")))


def _clean_name(p):
    """A name without footnote marks ("Cheng1") and possessive 's."""
    p = re.sub(r"(?<=[^\W\d_])[\d*†‡§¹²³]+(?=$|[\s,])", "", p)
    return re.sub(r"['’]s$", "", p.strip())


def _name_tokens(p):
    """Tokens of a candidate personal name, or None when it cannot be one."""
    p = _clean_name(p)
    toks = p.replace(",", " ").split()
    if not 1 <= len(toks) <= 5 or len(p) > 48:
        return None
    for t in toks:
        if not _ALPHAWORD.match(t) or re.search(r"\d", t):
            return None
        if t[0].islower() and t.lower() not in _PARTICLES:
            return None
        if _tok(t) in ENGINEERING or _tok(t) in _STD_ORGS or _tok(t) in _FOREIGN_STOP or _tok(t) in _FUNC:
            return None
    return toks


def _has_initial(toks):
    return any(re.fullmatch(r"[A-Z]\.", t) or re.fullmatch(r"[A-Z]\.[A-Za-z'’-]{2,}", t) for t in toks)


def _name_evidence(toks, trusted):
    """How sure we are that the tokens are a person's name: 0 no, 1 plausible, 2 sure."""
    low = [_tok(t).replace(".", "") for t in toks]
    # next to a strong label only the lower-case lists count; elsewhere German nouns are words too
    nonwords = [t for t in low if t and t not in _PARTICLES and not _is_word(t, not trusted)]
    if _has_initial(toks):
        return 2
    if len(set(low)) < len(low):
        return 0
    if len(toks) >= 2 and low[0] in _GIVEN and len(low[0]) >= 2:
        return 2
    if len(toks) >= 2 and len(nonwords) >= len([t for t in low if t not in _PARTICLES]):
        return 2          # every token is unknown to the English word list: a foreign or rare name
    if trusted and nonwords:
        return 1
    return 0


_TITLE = re.compile(r"^(?:(?:prof|dr|mr|mrs|ms|miss|sir|dipl|ing|eng|engr|mag|herr|frau|mme|mlle|sr|sra)(?:\.[ -]*|\s+))+", re.I)


def _person(value, weak=False):
    """Names in a value next to a person label: [(kind, string)]."""
    out = []
    parts = re.split(r"\s*(?:[/;&|]|\band\b|\bund\b|\bet\b|\se\s|\s{3,})\s*", value)
    pieces = []
    for part in parts:
        part = _norm(part)
        if part.count(",") >= 1 and (len(part.split()) > 4 or re.search(r"\d", part)):
            pieces.extend(x for x in part.split(",") if x.strip())     # "Anna Meier, tel 044..."
        else:
            pieces.append(part)
    for part in pieces:
        p = _strip_dates(_norm(part)).strip(" ,;:/-|_.()")
        t = _TITLE.match(p)
        if t and len(p) > t.end() + 2:
            p = p[t.end():].strip()
        if len(p) < 2:
            continue
        if "," in p and len(p.split(",")) == 2 and len(p.split(",")[0].split()) == 1:
            a, b = [x.strip() for x in p.split(",")]
            p = b + " " + a if b else a
        p = _clean_name(p)
        toks = _name_tokens(p)
        if toks is None or _generic_phrase(p):
            continue
        compact = re.sub(r"[\s.]", "", p)
        if len(toks) == 1 and re.fullmatch(r"[A-Z]{2,4}", compact):
            out.append(("initials", p))            # BJD, KS: too short to replace everywhere
            continue
        if len(toks) >= 2 and all(re.fullmatch(r"[A-Z]\.?", t) for t in toks):
            out.append(("initials", p))
            continue
        if len(toks) == 1 and re.fullmatch(r"[A-Z]\.?[A-Z]{1,2}\.?", compact) and len(compact) <= 3:
            out.append(("initials", p))
            continue
        if re.search(r"(?i)\b(engineering|design|designs|studio|labs?|group|inc|ltd|gmbh|corp|company|co|srl|spa|sa|ag|bv|llc|plc|sas|oy|ab)\b\.?$", p):
            out.append(("org", p))
            continue
        ev = _name_evidence(toks, trusted=not weak)
        if ev == 0 or (weak and ev < 2 and len(toks) < 2):
            continue
        out.append(("person", p))
    return out


_SUFFIX_WORDS = set("""inc ltd llc gmbh corp corporation co company ag sa sas srl bv plc oy ab university
institute institut college school foundation association laboratory laboratories lab labs group
industries technologies technology systems engineering manufacturing instruments robotics electronics
mfg specification""".split())


def _orgish(value, kind, weak=False):
    p = _strip_dates(_norm(value)).strip(" ,;:/-|_()")
    toks = p.split()
    while len(toks) > 2 and _tok(toks[0]) in ENGINEERING:
        toks = toks[1:]        # "NEXT ASSY CALIFORNIA INSTITUTE" -> the institute
    while len(toks) > 1 and _tok(toks[0]) in _LEAD_DROP:
        toks = toks[1:]        # "Canada Ltd" -> "Ltd" -> nothing
    p = " ".join(toks)
    if "[" in p or "]" in p or (kind == "org" and re.search(r"[A-Za-z]{2,}\d{4,}", p)):
        return []
    if toks and all(_tok(t) in _SUFFIX_WORDS or _tok(t) in ENGINEERING or _tok(t) in _FUNC for t in toks):
        return []
    if any(_tok(t) in _STD_ORGS for t in toks) and len(toks) <= 3:
        return []
    if re.search(r"\b[A-Z]\d{5,}\b", p) and kind == "org":
        return []
    if not toks or len(toks) > 8 or len(p) < 3 or len(p) > 80:
        return []
    if not (p[0].isupper() or p[0].isdigit()) or _generic_phrase(p) or p.lower() in ENGINEERING:
        return []
    low = [_tok(t) for t in toks]
    if any(t in _FUNC for t in low[:1] + low[-1:]) or sum(t in _FUNC for t in low) > 1:
        return []
    if p.count("(") != p.count(")") or re.search(r"[),]$", p) or "date" in low:
        return []
    if len(toks) >= 3 and sum(t[0].islower() and _tok(t) not in _PARTICLES and _tok(t) not in ("of", "for", "and") for t in toks) >= 1:
        return []
    if sum(c.isalpha() for c in p) < 3 or sum(c.isdigit() for c in p) > len(p) / 2:
        return []
    if re.search(r"[.!?]\s+[A-Za-z]|[.!?]$", p) and len(toks) > 3:
        return []
    if len(toks) == 1 and (_is_word(low[0], weak) or low[0] in _STD_ORGS):
        return []
    if kind == "org" and _has_initial(toks) and len(toks) <= 3 and not _ORG_SUFFIX.search(p):
        return _person(p, True)
    if weak and (len(toks) > 5 or all(_is_word(t) or t in _FUNC or t in _FOREIGN_STOP or t in _OTHER_STOP for t in low)):
        return []          # "Collet Chucks", "Platzieren Sie": only vocabulary, in any language
    if all(_tok(t) in _english() or _tok(t) in ENGINEERING for t in toks) and kind == "org" and len(toks) > 3:
        return []
    return [(kind, p)]


_UNIT_TOKEN = re.compile(r"^\d+(?:[.,]\d+)?(?:mm|cm|m|in|v|a|w|hz|khz|mhz|kg|g|lb|pf|nf|uf|k|%|x\d+|deg|°|max|min)$", re.I)
_REFDES = re.compile(r"^[A-Z]{1,3}\d{1,3}$")


def _partno(value, weak=False):
    out = []
    for p in re.split(r"\s*[;,|]\s*", _norm(value)):
        p = p.strip(" ,;:/|_()")
        if not 4 <= len(p) <= 32 or " " in p:
            continue
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._#/\\-]*[A-Za-z0-9]", p):
            continue
        if not re.search(r"\d", p) or not re.search(r"[A-Za-z0-9]{3}", p):
            continue
        if _UNIT_TOKEN.match(p) or _REFDES.match(p) or _DATE.match(p) or is_generic(p):
            continue
        if re.fullmatch(r"[\d.,/:-]+", p) and not (re.search(r"\d{4,}", p) and not re.fullmatch(r"(19|20)\d\d[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.](19|20)?\d\d", p)):
            continue
        if re.fullmatch(r"(19|20)\d\d[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.](19|20)?\d\d", p):
            continue
        if re.fullmatch(r"(?i)(rev|revision|sheet|blatt|seite|page|scale|size)[-_ ]?\w*|[A-Z]\d{1,2}(-\d+)?|M\d{1,2}(x[\d.]+)?|\d+x\d+(x\d+)?", p):
            continue
        if weak and not (re.search(r"[A-Za-z]", p) and sum(c.isdigit() for c in p) >= 2 and (len(p) >= 6 or re.search(r"[-_.]", p))):
            continue
        out.append(("part", p))
    return out


def _phones(value):
    out = []
    if re.match(r"\s*\((?:19|20)\d\d\)", value):
        return out
    for m in _PHONE.finditer(value):
        s = m.group().strip()
        if len(re.sub(r"\D", "", s)) >= 8:
            out.append(("phone", s))
    if not out:
        s = value.strip(" :;,")
        digits = re.sub(r"\D", "", s)
        if 7 <= len(digits) <= 15 and re.fullmatch(r"[+\d()\s./-]+", s) and not _DATE.match(s):
            out.append(("phone", s))
    return out


def _host_ids(url):
    """Identifying words of a URL or host: [string]."""
    u = re.sub(r"(?i)^(?:https?|ftp)://", "", url.strip().rstrip(".,;:)>]}'\""))
    host, _, path = u.partition("/")
    host = host.split("@")[-1].split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return []
    out = []
    labels = host.split(".")
    base = ".".join(labels[-2:])
    if base in _PUBLIC_HOSTS or ".".join(labels[-3:]) in _PUBLIC_HOSTS:
        # account.github.io/repo, github.com/account/repo
        if base in ("github.io", "gitlab.io", "wordpress.com", "blogspot.com", "readthedocs.io") and len(labels) > 2:
            out.append(labels[0])
        segs = [s for s in path.split("/") if s and not s.startswith(("#", "?"))]
        for seg in segs[:2]:
            seg = re.split(r"[?#]", seg)[0]
            if seg.lower() in ("blob", "tree", "raw", "wiki", "issues", "releases", "pull", "master", "main", "orgs", "users"):
                break
            out.append(seg)
        return [x for x in out if len(x) >= 3 and re.fullmatch(r"[\w.-]+", x)]
    if host in _SKIP_HOSTS or base in _SKIP_HOSTS:
        return []
    keep = 2 if len(labels) > 2 and labels[-2] in ("co", "ac", "edu", "org", "gov", "com", "net") and len(labels[-1]) == 2 else 1
    ident = labels[:-keep]
    if not ident:
        return []
    main = ident[-1]
    if main in ENGINEERING or main in _STD_ORGS or main in STOP or len(main) < 3:
        return []
    out.append(main)
    for l in ident[:-1]:
        if len(l) >= 3 and l not in _GENERIC_LABELS and l not in _english() and l not in ("www",):
            out.append(l)
    return out


def _email_ids(local, domain):
    out = []
    labels = domain.lower().split(".")
    if labels[0] in VENDOR_DOMAINS or (len(labels) >= 2 and labels[-2] in VENDOR_DOMAINS):
        return []
    if not (re.fullmatch(r"[0-9a-f]{8,}", local.lower()) or is_generic(local) or local.lower() in
            ("info", "contact", "support", "sales", "admin", "office", "mail", "hello", "webmaster",
             "noreply", "no-reply", "service", "help", "email", "enquiries", "inquiries", "mailto")):
        out.append(local)
    if labels[-2 if len(labels) > 1 else 0] not in PUBLIC_MAIL:
        out.extend(_host_ids(domain))
    return out


# --------------------------------------------------------------------------- layout

class _Line:
    __slots__ = ("text", "x0", "y0", "x1", "y1", "dx", "dy", "size", "cx", "cy", "hl", "ht", "labels", "page")

    def __init__(self, text, bbox, d, size, page):
        self.text, self.page = text, page
        self.x0, self.y0, self.x1, self.y1 = bbox
        self.dx, self.dy = d
        self.size = size or 8.0
        self.cx, self.cy = (self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2
        w, h = self.x1 - self.x0, self.y1 - self.y0
        self.hl = (abs(self.dx) * w + abs(self.dy) * h) / 2      # half length along the text
        self.ht = (abs(self.dx) * h + abs(self.dy) * w) / 2      # half thickness
        self.labels = None


def _lines_of(page, pno):
    import pymupdf as fitz

    flags = getattr(fitz, "TEXTFLAGS_TEXT", 0)
    out = []
    for block in page.get_text("dict", flags=flags)["blocks"]:
        for ln in block.get("lines", []):
            text = _norm("".join(s["text"] for s in ln["spans"]))
            if not text:
                continue
            d = ln.get("dir") or (1.0, 0.0)
            size = max((s.get("size", 0) for s in ln["spans"]), default=8.0)
            out.append(_Line(text, ln["bbox"], d, size, pno))
    return out


def _find_labels(line):
    """[(start, end, cls, key)] of the labels in a line: the first one has to open the line
    (a field label starts its text object; "design" in the middle of a sentence is prose)."""
    if line.labels is None:
        found = []
        for m in _LABEL_RX.finditer(line.text):
            cls, key = _label_class(m.group(1))
            if not cls:
                continue
            if not found and m.start() > 3:
                break
            found.append((m.start(), m.end(), cls, key))
        line.labels = found
    return line.labels


def _neighbours(label, lines):
    """Candidate value lines of a label line: [(rank, side, line)], side R/B/A."""
    cands = []
    for c in lines:
        if c is label or c.dx * label.dx + c.dy * label.dy < 0.9:
            continue
        vx, vy = c.cx - label.cx, c.cy - label.cy
        along, across = vx * label.dx + vy * label.dy, -vx * label.dy + vy * label.dx
        gap = along - label.hl - c.hl
        tol = 0.65 * max(label.ht, c.ht) + 1.0
        if abs(across) <= tol and -3 <= gap <= 90:
            cands.append((gap, "R", c))
            continue
        thick = label.ht + c.ht
        left_l, left_c = -label.hl, along - c.hl
        overlap = min(label.hl, along + c.hl) - max(left_l, left_c)
        aligned = abs(left_c - left_l) < 14 or overlap > 0.3 * min(2 * label.hl, 2 * c.hl)
        reach = 3.2 * max(label.size, c.size)
        if aligned and thick - 2 <= across <= thick + reach:
            cands.append((100 + across, "B", c))
        elif aligned and -(thick + 0.7 * reach) <= across <= -(thick - 2):
            cands.append((300 - across, "A", c))
    cands.sort(key=lambda x: x[0])
    return cands


def _values_of(cls, key, text):
    if cls == "person":
        return _person(text, key in WEAK_PERSON)
    if cls == "org":
        return _orgish(text, "org", False)
    if cls == "project":
        if re.fullmatch(r"[\d\- ]+", text):
            return []
        return _orgish(text, "project", key in WEAK_PROJECT)
    if cls == "part":
        return _partno(text, key in WEAK_PART)
    if cls == "phone":
        return _phones(text)
    if cls == "email":
        return [("email", m.group()) for m in _EMAIL.finditer(text)]
    if cls == "web":
        return [("url", m.group()) for m in _URL.finditer(text)]
    return []


def _address_lines(label, lines, first_value):
    """An address under (or beside) its label: the label's own value plus up to 4 lines
    stacked below it, stopping at the next label."""
    out = [first_value] if first_value else []
    cur, chain = label, []
    sides = ("B",) if first_value else ("R", "B")
    for _ in range(4):
        nxt = None
        for _, side, c in _neighbours(cur, lines):
            if side in sides:
                nxt = None if _find_labels(c) else c
                break
        if nxt is None or nxt in chain:
            break
        chain.append(nxt)
        cur, sides = nxt, ("B",)
    out.extend(c.text for c in chain)
    res = []
    for t in out:
        t = _norm(t).strip(" ,;:")
        if len(t) < 4 or len(t) > 70 or _generic_phrase(t) or sum(ch.isalpha() for ch in t) < 2:
            continue
        if re.search(r"\b(is|are|the|to|of|and)\b", t) and len(t.split()) > 4:
            continue
        if re.fullmatch(r"[A-Za-z]+ \d{1,3}", t) and t.split()[0].lower() in _english() or re.search(r"0x[0-9A-Fa-f]+", t):
            continue
        res.append(("address", t))
    return res


def _title_block(lines):
    """Fields found by label proximity: [(kind, string)]. lines: the lines of one page."""
    found = []
    for line in lines:
        labs = _find_labels(line)
        if not labs:
            continue
        text = line.text
        empty = []
        for i, (s, e, cls, key) in enumerate(labs):
            end = labs[i + 1][0] if i + 1 < len(labs) else len(text)
            val = text[e:end].strip(_SEP)
            colon = ":" in text[s:e + 1] or "=" in text[s:e + 1]
            if len(text) > 90 and not colon:
                continue  # running prose, not a field
            if re.search(r"[^\W_]", val):
                if key in _WEAK and not colon:
                    continue  # "model of the", "part 3": only a field with a separator
                if cls == "address":
                    found.extend(_address_lines(line, lines, val))
                else:
                    found.extend(_values_of(cls, key, val))
            else:
                empty.append((cls, key))
        if len(labs) == 1 and len(empty) == 1 and len(text) <= 40:
            cls, key = empty[0]
            if cls == "address":
                found.extend(_address_lines(line, lines, ""))
                continue
            if cls in ("email", "web"):
                continue
            for rank, side, c in _neighbours(line, lines):
                if side == "R" and rank > 45:
                    continue          # a far cell on the same row is rarely this field's value
                if _find_labels(c):
                    if side == "B":
                        break         # the next row is another field
                    continue
                got = _values_of(cls, key, c.text)
                if got and (side != "A" or cls == "person"):
                    found.extend(got)
                    break
                if side == "B":
                    break
    return found


# --------------------------------------------------------------------------- patterns and NER

def _patterns(text):
    out = []
    for m in _EMAIL.finditer(text):
        out.append(("email", m.group()))
    for m in _URL.finditer(text):
        out.append(("url", m.group().rstrip(".,;:")))
    for m in _PHONE.finditer(text):
        s = m.group().strip()
        digits = re.sub(r"\D", "", s)
        if re.match(r"\((?:19|20)\d\d\)", s):
            continue
        if len(digits) >= 9 and not re.fullmatch(r"(19|20)\d\d[-./ ]\d{1,2}[-./ ]\d{1,2}", s):
            out.append(("phone", s))
    for rx in (_STREET_US, _STREET_EU, _STREET_FR, _STREET_DE, _BOX, _ZIPCITY, _USZIP):
        for m in rx.finditer(text):
            s = _norm(m.group(1))
            if rx is _ZIPCITY:
                city = s.split()[-1].lower() if len(s.split()) == 2 else s.split()[1].lower()
                if city in _english() or city in ENGINEERING or len(city) < 4 or \
                        (re.match(r"^\d{4}\s", s) and not re.match(r"^[A-Z]{1,2}-", s)) or re.match(r"^(19|20)\d\d\b", s):
                    continue
            if len(s) >= 6 and not _generic_phrase(s):
                out.append(("address", s))
    for m in _COPY.finditer(text):
        v = m.group(1).strip(" ,")
        got = _orgish(v, "org", True) or _person(v, True)
        out.extend(got)
    for rx in (_ORG_RX, _ORG_RX_UP, _ORG_OF):
        for m in rx.finditer(text):
            toks = _norm(m.group(1)).strip(" ,.").split()
            if rx is not _ORG_OF:
                # drop leading words that cannot start a company name: countries, verbs, plain nouns
                while len(toks) > 2 and (_tok(toks[0]) in _LEAD_DROP or _tok(toks[0]) in _english() and rx is _ORG_RX):
                    toks = toks[1:]
                if len(toks) > 4:
                    toks = toks[-4:]
            out.extend(_orgish(" ".join(toks), "org", True))
    return out


_NLP = None
_NLP_FAIL = False


def _nlp():
    global _NLP, _NLP_FAIL
    if _NLP is None and not _NLP_FAIL:
        try:
            import spacy
            for name in ("en_core_web_md", "en_core_web_sm"):
                try:
                    _NLP = spacy.load(name, disable=["tagger", "parser", "attribute_ruler", "lemmatizer"])
                    break
                except OSError:
                    continue
            if _NLP is not None:
                _NLP.max_length = 400000
            else:
                _NLP_FAIL = True
        except Exception:
            _NLP_FAIL = True
    return _NLP


# Title-block and notice rules on the plain page text (cells one per line, and table rows joined):
# names the label-proximity search and the NER model miss. Every captured value goes through
# _distinct_words, so only words that are no dictionary or drawing word in any language are kept.
_D_DATE = (r"(?:\d{1,4}[./\-]\d{1,2}[./\-]\d{1,4}|\d{1,2}[ \-]?(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC|"
           r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z]*[ \-.]?\d{2,4})")
_D_LBL = (r"drawn(?:[ \t]+by)?|drwn|dwn|drn|designed(?:[ \t]+by)?|designer|design[ \t]+by|dsgn|checked(?:[ \t]+by)?|"
          r"chk'?d|chkd|chk|approved(?:[ \t]+by)?|approval|appr?'?d|appv'?d|appr|eng\.?[ \t]*appr\.?|"
          r"mfg\.?[ \t]*appr\.?|engineer|author|created[ \t]+by|prepared[ \t]+by|reviewed[ \t]+by|modell?ed[ \t]+by|"
          r"made[ \t]+by|originator|drafter|draftsman|responsible|gezeichnet|gez\.?|bearbeite[tr]|bearb\.?|"
          r"gepr(?:ü|ue?)ft|gepr\.?|freigegeben|erstellt(?:[ \t]+von)?|ersteller|konstrukteur|zeichner|"
          r"dessin[ée](?:[ \t]+par)?|dessinateur|v[ée]rifi[ée](?:[ \t]+par)?|auteur|dibujado(?:[ \t]+por)?|"
          r"dise[ñn]ado(?:[ \t]+por)?|revisado|aprobado|comprobado|disegnato(?:[ \t]+da)?|disegnatore|progettista|"
          r"controllato|approvato|getekend|gecontroleerd|tekenaar")
_D_PERSON = (r"(?:[A-Za-zÀ-ÿ]\.[ ]?){1,2}[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'\-]{2,}"                 # J.Smith, A. Doé, b.lee
             r"|[A-ZÀ-Ý][A-Za-zÀ-ÿ'\-]+(?:[ ](?:de|van|von|der|den|la|le|du|di|da|del|dos|ten|ter))*"
             r"(?:[ ][A-ZÀ-Ý][A-Za-zÀ-ÿ'\-]+){0,2}"                                       # Jerry de Vos, PFRITSHCEL
             r"|[a-z]{2,}[._][a-z]{1,}")                                                   # dedeep.v
_D_NAMELINE = r"(?:[A-ZÀ-Ý]\.[ ]?)?[A-ZÀ-Ý][a-zà-ÿ]+(?:[ ][A-ZÀ-Ý][a-zà-ÿ'\-]+){1,2}"
_D_RULES = (
    # label, then a name on the same line or in the next cell
    ("person", re.compile(r"(?<![A-Za-z])(?i:" + _D_LBL + r")(?![A-Za-z])[ \t]*[:.]?[ \t]*\n?[ \t]*(" + _D_PERSON + r")")),
    # label, value, date on one row: also accounts and lower-case names
    ("person", re.compile(r"(?<![A-Za-z])(?i:" + _D_LBL + r")(?![A-Za-z])[ \t]*[:.]?[ \t]+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ.'\-]{1,24}"
                          r"(?:[ ][A-Za-zÀ-ÿ.'\-]{2,20})?)[ \t]+" + _D_DATE)),
    # initials in a title-block slot
    ("initials", re.compile(r"(?<![A-Za-z])(?i:" + _D_LBL + r")(?![A-Za-z])[ \t]*[:.]?[ \t]+([A-Z]{2,4}(?:/[A-Z]{2,4})?)"
                            r"(?=[ \t]+" + _D_DATE + r"|[ \t]*\n)")),
    # Windows DOMAIN\account in revision tables
    ("account", re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,11}\\{1,2}[a-z][a-z0-9._\-]{2,24})(?![A-Za-z0-9])")),
    # copyright / "property of" holder, company with a legal form, university / institute / laboratory
    ("org", re.compile(r"(?:©|\([cC]\)|(?i:copyright)|(?i:property[ \t]+of)|(?i:eigentum[ \t]+(?:der|von))|"
                       r"(?i:propri[ée]t[ée][ \t]+de))[ \t]*(?:©|\([cC]\))?[ \t]*(?:(?:19|20)\d\d[ ,\-–]*)*(?i:by[ \t]+)?"
                       r"\n?[ \t]*((?:[A-ZÀ-Ý][\w&.'\-]*[ \t]*){1,6})")),
    ("org", re.compile(r"((?:[A-ZÀ-Ý][\w&'\-]+[ \t]){1,4})(?:Inc\.?|LLC|L\.L\.C\.|Ltd\.?|Limited|GmbH|AG|KG|Corp\.?|"
                       r"Corporation|Co\.|S\.A\.|S\.L\.|B\.V\.|N\.V\.|Pty|Oy|SAS|S\.r\.l\.|S\.p\.A\.)(?![A-Za-z])")),
    ("org", re.compile(r"((?:(?:University|Universit[äa]t|Universit[ée]|Universidad|Università|Hochschule|Fachhochschule|"
                       r"Institute|Institut|Instituto|Laboratory|Laboratories|College|Politecnico|UNIVERSITY|INSTITUTE|"
                       r"LABORATORY)(?:[ \t]+(?:of|OF|de|di|für|for|der|des|du))?(?:[ \t]+[A-ZÀ-Ý][\w\-]+){1,3})|"
                       r"(?:(?:[A-ZÀ-Ý][\w\-]+[ \t]+){1,3}(?:University|Universit[äa]t|Institute|Institut|Laboratory|"
                       r"Laboratories|College|Hochschule|UNIVERSITY|INSTITUTE|LABORATORY|LABORATORIES)"
                       r"(?:[ \t]+(?:of|OF)[ \t]+[A-ZÀ-Ý][\w\-]+)?))")),
)
_D_DATE_LINE = re.compile(r"[ \t]*" + _D_DATE + r"[ \t]*$")
_D_NAME_LINE = re.compile(r"[ \t]*(" + _D_NAMELINE + r")[ \t]*$")
_D_NOISE = set("""name date datum scale sheet size title unless otherwise specified tolerances part number description
item drawing signature sign mfg engr format nom visa von durch fecha escala hoja plano rights reserved university
institute laboratory college department dept school technology research center centre mechanical this document page
nombre firma dibujo chk estado aprovado yyyy jan feb mar apr jun jul aug sep sept oct nov dec vref bauteil req
equilibrant by base american italian british universidad institut instituto sistemas técnico hochschule technische
politecnico fachhochschule laboratories limited corporation swiss prof appv apprv appd apvd chkd dwn drwn drn
dsgn gepr gez bearb arxiv chemrxiv biorxiv""".split())


def _distinct_words(value):
    """Words of a captured value that can identify someone: 4+ letters, no dictionary or drawing
    word in any language, no label, standard body, placeholder or software name."""
    out = []
    for w in re.findall(r"[^\W\d_]+", value):
        t = w.lower()
        if len(t) < 4 or _is_word(t) or t in _STD_ORGS or t in _FUNC or t in _FOREIGN_STOP or t in _OTHER_STOP \
                or t in STOP or t in GENERIC_VALUES or t in _D_NOISE or t in _LABEL_CLASS or is_generic(w):
            continue
        out.append(w)
    return out


def _namelike(tok):
    """A word that may stand in a whole personal name: initial, particle, given name or non-word."""
    t = _tok(tok).replace(".", "")
    if not t or t in _PARTICLES or t in _GIVEN or re.fullmatch(r"[^\W\d_]\.?", tok):
        return True
    return tok[:1].isupper() and t not in _D_NOISE and t not in _LABEL_CLASS and not _is_word(t)


def _discover(text):
    """[(kind, string)] of the title-block and notice rules above."""
    out = []
    hits = [(kind, m.group(1)) for kind, rx in _D_RULES for m in rx.finditer(text)]
    # a name alone in its cell with a date cell within three lines (title blocks without labels)
    lines = text.split("\n")
    near = {j for i, line in enumerate(lines) if len(line) <= 40 and _D_DATE_LINE.match(line) for j in range(i - 3, i + 4)}
    for i in sorted(near):
        if 0 <= i < len(lines) and len(lines[i]) <= 60:
            m = _D_NAME_LINE.match(lines[i])
            if m:
                hits.append(("nameline", m.group(1)))
    for kind, value in hits:
        if True:
            value = _norm(value)
            if kind == "initials":
                for v in value.split("/"):
                    if v.lower() not in _D_NOISE and v.lower() not in _LABEL_CLASS and not _is_word(v.lower()) \
                            and v.lower() not in _STD_ORGS:
                        out.append(("initials", v))
                continue
            if kind == "account":
                domain, _, account = value.partition("\\")
                account = account.strip("\\")
                if len(domain) >= 3 and not _is_word(domain.lower()) and domain.lower() not in _STD_ORGS:
                    out.append(("org", domain))
                out.extend(("person", w) for w in _distinct_words(account))
                continue
            words = _distinct_words(value)
            if kind == "nameline":
                toks = _name_tokens(value)
                if toks is None or _name_evidence(toks, False) < 2:
                    continue                       # no label: only a given name, an initial or all non-words
                kind = "person"
            out.extend((kind, w) for w in words)
            toks = _name_tokens(value) if kind == "person" and len(value.split()) >= 2 else None
            if toks is not None and all(_namelike(t) for t in toks) and (words or _name_evidence(toks, False) == 2):
                out.append(("person", value))      # the whole name too: "Jerry de Vos" keeps its short family name
    return out


def _rows(lines):
    """Text of one page with the cells of a table row on one line, left to right."""
    rows = []
    for line in sorted((l for l in lines if abs(l.dx) >= 0.9), key=lambda l: l.cy):
        if rows and line.cy - rows[-1][-1].cy <= 3.0:
            rows[-1].append(line)
        else:
            rows.append([line])
    return "\n".join("   ".join(c.text for c in sorted(cells, key=lambda c: c.x0)) for cells in rows)


_ORG_SUFFIX = re.compile(r"\b(inc|ltd|llc|gmbh|corp|corporation|co|company|ag|sa|sas|srl|bv|plc|oy|ab|"
                         r"university|universität|université|universidad|universidade|universiteit|institute|institut|"
                         r"laboratory|laboratories|lab|labs|college|school|foundation|association|society|"
                         r"consortium|collaboration|group|technologies|technology|systems|industries|"
                         r"engineering|manufacturing|instruments|robotics|electronics|ministry|department)\b\.?$", re.I)


def _ner(lines_text):
    nlp = _nlp()
    if _foreign_text(lines_text):
        return []
    text = "\n".join(t for t in lines_text if sum(c.isalpha() for c in t) >= 4
                     and sum(c.isupper() for c in t) < 0.7 * sum(c.isalpha() for c in t))
    if nlp is None or len(text) < NER_MIN_CHARS:
        return []
    try:
        doc = nlp(text[:NER_CHARS])
    except Exception:
        return []
    out, org_count = [], {}
    for ent in doc.ents:
        if ent.label_ not in ("PERSON", "ORG"):
            continue
        s = _norm(ent.text.replace("\n", " ")).strip(" ,;:.-–()[]\"'")
        if not s or len(s) > 70 or len(re.findall(r"[^\W\d_]", s)) < 3:
            continue
        if ent.label_ == "PERSON":
            s = _clean_name(s)
            toks = _name_tokens(s)
            if toks is None or len(toks) < 2 or _generic_phrase(s) or "," in s:
                continue
            if any(re.fullmatch(r"[A-Z]", t) or re.fullmatch(r"[A-Z]{3,}", t) or re.match(r"[A-Z]{2,}[a-z]", t) for t in toks):
                continue
            if len(toks) > 3 and not _has_initial(toks):
                continue
            if _name_evidence(toks, trusted=False) < 2:
                continue
            out.append(("person", s))
        else:
            low = [_tok(t) for t in s.split()]
            if len(low) < 2 or s.isupper() or _generic_phrase(s):
                continue
            if any(t in _STD_ORGS for t in low) or "\n" in ent.text:
                continue
            suffix = bool(_ORG_SUFFIX.search(s))
            proper = any(len(t) >= 4 and not _is_word(t) and t not in _FUNC for t in low)
            if not all(t[0].isupper() or t in _PARTICLES or t in ("of", "for", "and", "&") for t in s.split()):
                continue
            if re.search(r"\d", s) and not suffix:
                continue
            if suffix or proper:
                org_count[s] = org_count.get(s, 0) + 1
                if suffix or (org_count[s] >= 2 and not any(t in ENGINEERING for t in low)):
                    out.extend(_orgish(s, "org", True))
    return out


# --------------------------------------------------------------------------- entry points

def _expand(items):
    """Turn raw (kind, string) values into the strings the identity map should get."""
    out, seen = [], set()

    def add(kind, s):
        s = _norm(s).strip(" ,;:")
        key = (kind, s.lower())
        if len(s) < 2 or key in seen:
            return
        seen.add(key)
        out.append((kind, s))

    for kind, s in items:
        if kind == "email":
            m = _EMAIL.fullmatch(s) or _EMAIL.search(s)
            if m:
                for x in _email_ids(m.group(1), m.group(2)):
                    add("email", x)
        elif kind == "url":
            for x in _host_ids(s):
                add("url", x)
        elif kind == "address":
            add("address", s)
            parts = [p.strip() for p in s.split(",")]
            if len(parts) > 1:
                for p in parts:
                    if len(p) >= 5 and re.search(r"[^\W\d_]{3}", p) and not _generic_phrase(p):
                        add("address", p)
        else:
            add(kind, s)
    return out


def _field_label(name):
    """A form field name such as "DrawnBy" or "customer_name" as label text."""
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name or "")
    name = re.sub(r"[_.\-\[\]]+", " ", name)
    return _norm(name)


def _annotation_items(doc, pages, ner=True):
    """Strings from comments, form fields and link targets: [(kind, string)]."""
    out, texts = [], []
    for pno in pages:
        try:
            page = doc[pno]
            for a in page.annots() or []:
                info = a.info or {}
                title = _clean_name(_norm(info.get("title") or ""))        # comment author
                if title:
                    out.extend(_person(title, True))
                for key in ("content", "subject"):
                    v = _norm(info.get(key) or "")
                    if v:
                        texts.append(v)
            for w in page.widgets() or []:
                v = _norm(str(w.field_value or ""))
                if not v or v.lower() in ("off", "yes", "no", "true", "false"):
                    continue
                cls, key = _label_class(_field_label(w.field_name))
                texts.append(v)
                if cls:
                    out.extend(_values_of(cls, key, v))
                elif len(v.split()) <= 4:
                    out.extend(_person(v, True))
            for ln in page.get_links():
                uri = ln.get("uri") or ""
                if re.match(r"(?i)(https?|ftp)://", uri):
                    out.append(("url", uri))
                elif uri.lower().startswith("mailto:"):
                    out.extend(("email", m.group()) for m in _EMAIL.finditer(uri))
        except Exception:
            continue
    if texts:
        out.extend(_patterns("\n".join(texts)))
        lines = [_Line(t, (0, i * 10, 100, i * 10 + 9), (1.0, 0.0), 8.0, 0) for i, t in enumerate(texts)]
        out.extend(_title_block(lines))
        if ner:
            out.extend(_ner(texts))
    return out


def _scan_doc(doc, ner, depth):
    raw = []
    if doc.needs_pass and not doc.authenticate(""):
        return raw
    pages = range(min(len(doc), MAX_PAGES))
    by_page, texts = {}, []
    for pno in pages:
        try:
            lines = _lines_of(doc[pno], pno)
        except Exception:
            continue
        by_page[pno] = lines
        texts.extend(l.text for l in lines)
    for lines in by_page.values():
        raw.extend(_title_block(lines))
    raw.extend(_patterns("\n".join(texts)))
    raw.extend(_discover("\n".join(texts)))
    raw.extend(_discover("\n".join(_rows(lines) for lines in by_page.values())))
    if ner:
        raw.extend(_ner(texts))
    try:
        raw.extend(_annotation_items(doc, pages, ner))
    except Exception:
        pass
    if depth < 2:
        try:
            names = doc.embfile_names()
        except Exception:
            names = []
        for name in names[:20]:
            try:
                data = doc.embfile_get(name)
            except Exception:
                continue
            if not data or len(data) > 20_000_000:
                continue
            try:
                if data[:5] == b"%PDF-":
                    import pymupdf as fitz
                    with fitz.open(stream=data, filetype="pdf") as inner:
                        raw.extend(_scan_doc(inner, ner, depth + 1))
                elif b"\x00" not in data[:2000]:
                    text = data[:300000].decode("utf-8", "replace")
                    raw.extend(_patterns(text))
                    if ner:
                        raw.extend(_ner(text.splitlines()[:2000]))
                    lines = [_Line(t, (0, i * 10, 100, i * 10 + 9), (1.0, 0.0), 8.0, 0)
                             for i, t in enumerate(_norm(x) for x in text.splitlines()[:2000]) if t]
                    raw.extend(_title_block(lines))
            except Exception:
                continue
    return raw


def find_detailed(pdf_path, ner=True):
    """[(kind, string)] found in the PDF (page text, comments, form fields, link targets and
    attached files); [] when it cannot be read."""
    try:
        import pymupdf as fitz

        doc = fitz.open(pdf_path)
    except Exception:
        return []
    raw = []
    try:
        raw = _scan_doc(doc, ner, 0)
    except Exception:
        pass
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return _expand(raw)


def find(pdf_path):
    """Identity strings of a PDF (no bare initials), deduplicated, in order of discovery."""
    seen, out = set(), []
    for kind, s in find_detailed(pdf_path):
        if kind == "initials":
            continue
        if s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out
