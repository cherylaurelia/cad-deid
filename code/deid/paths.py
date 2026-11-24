"""Output path for every input path: identifying components renamed, the rest kept."""
import os
import re

from .identities import HEX12, domain_labels, project_tokens
from .pseudo import KEEP
from .scan import is_generic

WORDS = re.compile(r"[A-Za-z0-9]+")

# Native CAD files whose contents are not rewritten (Creo, some NX): other files point to
# them by file name, so their base names are kept. SolidWorks, Inventor, Solid Edge and
# CATIA files are renamed like everything else: the names stored inside them are replaced
# with the same pseudonyms.
NATIVE_LINKED = set(".prt .asm .drw".split())
VERSIONED = re.compile(r"\.(prt|asm|drw)\.\d+$", re.I)
EXT = re.compile(r"\.[A-Za-z0-9_~-]{1,16}$")
PIECE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")  # camelCase pieces of a name, digits dropped


class PathMapper:
    def __init__(self, pseudo, path_scanner, everyday=frozenset()):
        self.pseudo = pseudo
        self.scanner = path_scanner
        self.everyday = everyday  # ordinary words: not a project name on their own
        self.cache = {}

    def project_names(self, path):
        """The account and project names of the folder a corpus path lives in. They are
        replaced in that project's own documents even when too generic for the whole
        corpus; a project called by one ordinary word ("Hardware") is left alone."""
        parts = path.split("/")
        if parts[0] == "archives" and len(parts) == 2:
            group, name = "archives", parts[1]
        elif parts[0] == "sources" and len(parts) > 3:
            group, name = parts[1], parts[2]
        else:
            return ()
        return self._cached("names:" + group, name, lambda n: self._project_names(group, n))

    def _project_names(self, group, name):
        toks = project_tokens(group, name)
        if group in ("web", "catalog"):
            dl = domain_labels(name)
            toks = [(label, True) for label in dl[0] if label.lower() != "www"] if dl else []
        out = []
        for tok, is_account in toks:
            words = WORDS.findall(tok)
            size = sum(map(len, words))
            if is_account and size >= 4:
                out.append(tok)
            elif size >= 5 and (len(words) > 1 or words[0].lower() not in self.everyday):
                out.append(tok)
        return tuple(out)

    def scope_parts(self, path):
        """Distinctive pieces of the account / project / catalog name of the folder a corpus
        path lives in, to replace in that folder's own documents: (words matched in any
        case, acronyms matched in exact case)."""
        parts = path.split("/")
        if parts[0] == "archives" and len(parts) == 2:
            group, name = "archives", parts[1]
        elif parts[0] == "sources" and len(parts) > 3:
            group, name = parts[1], parts[2]
        else:
            return (), ()
        return self._cached("parts:" + group, name, lambda n: self._scope_parts(group, n))

    def _scope_parts(self, group, name):
        loose, exact = set(), set()
        toks = project_tokens(group, name)
        if group == "catalog" and not domain_labels(name):
            lead = []  # the words before the description: "ACME Widget2" of "ACME Widget2 original ..."
            for word in name.split():
                if word.islower():
                    break
                lead.append(word)
            toks = [(word, True) for word in lead]
        for tok, is_account in toks:
            for piece in PIECE.findall(tok):
                low = piece.lower()
                if low in self.everyday or low in KEEP or is_generic(piece) or (low.endswith("s") and low[:-1] in self.everyday):
                    continue  # an ordinary word, a company form (LLC) or a software name
                if len(piece) >= 4:
                    loose.add(piece.lower())
                elif is_account and len(piece) == 3 and piece.isupper():
                    exact.add(piece)
        return tuple(sorted(loose)), tuple(sorted(exact))

    def _account_repo(self, name):
        return "__".join(t if HEX12.fullmatch(t) or t == "gitlab.com" else self.pseudo.span(t)
                         for t in name.split("__"))

    def project(self, group, name):
        """The folder (or archive) that names the account/project or the web host."""
        if group in ("github", "gitlab"):
            return self._account_repo(name)
        if group == "archives":
            m = EXT.search(name)
            stem, ext = (name[:m.start()], m.group()) if m else (name, "")
            return self._account_repo(stem) + ext
        dl = domain_labels(name)
        if dl:
            host, suffix = dl
            return ".".join([l if l.lower() == "www" else self.pseudo.span(l) for l in host] + suffix)
        return self.scanner.text(name)

    def local_rules(self, group, name):
        """A multi-word project name is also replaced where it reappears inside its own
        folder tree (for example '<project>.extracted/<project>-master/')."""
        rules = []
        for tok, _ in project_tokens(group, name):
            words = WORDS.findall(tok)
            if len(words) >= 2 and sum(map(len, words)) >= 6:
                # the same words with any separators (My-Project, My_Project, MyProject)
                body = r"[-_. ]*".join(re.escape(w) for w in words)
                rules.append(re.compile(r"(?<![A-Za-z])" + body + r"(?![A-Za-z])", re.I))
        return rules

    def _local(self, text, rules):
        for rx in rules:
            text = rx.sub(lambda m: self.pseudo.span(m.group()), text)
        return text

    def dir_name(self, name, rules):
        return self._local(self.scanner.text(name), rules)

    def file_name(self, name, rules=()):
        m = EXT.search(name)
        stem, ext = (name[:m.start()], m.group()) if m else (name, "")
        if ext.lower() in NATIVE_LINKED:
            return name
        return self._local(self.scanner.text(stem), rules) + ext

    def _cached(self, kind, name, fn):
        key = (kind, name)
        out = self.cache.get(key)
        if out is None:
            out = self.cache[key] = fn(name)
        return out

    def rules_of(self, path):
        """In-tree rename rules of the project a corpus path belongs to."""
        parts = path.split("/")
        if parts[0] == "sources" and len(parts) > 3:
            group = parts[1]
            return self._cached("rules:" + group, parts[2], lambda n: self.local_rules(group, n))
        return ()

    def map(self, path):
        parts = path.split("/")
        out = list(parts)
        if parts[0] == "archives" and len(parts) == 2:
            out[1] = self._cached("archives", parts[1], lambda n: self.project("archives", n))
            return "/".join(out)
        start, scope, rules = 1, "", ()
        if parts[0] == "sources" and len(parts) > 3:
            group = parts[1]
            out[2] = self._cached(group, parts[2], lambda n: self.project(group, n))
            rules = self._cached("rules:" + group, parts[2], lambda n: self.local_rules(group, n))
            scope = parts[2] if rules else ""
            start = 3
        # DEID_PATH_SUBDIRS=keep: folders below the project folder keep their names (the
        # drawing/model check loses pairs whose folders were renamed below the project)
        stop = len(parts) - 1
        if os.environ.get("DEID_PATH_SUBDIRS") == "keep":
            stop = min(stop, max(start, 2))
        for i in range(start, stop):
            out[i] = self._cached("dir:" + scope, parts[i], lambda n: self.dir_name(n, rules))
        out[-1] = self._cached("file:" + scope, parts[-1], lambda n: self.file_name(n, rules))
        return "/".join(out)
