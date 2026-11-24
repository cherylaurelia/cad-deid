#!/usr/bin/env python3
"""check_packaging.py: packaging, path-findability and repeatability checks for one attempt.

Run on a box from ~/deid with the pipeline's venv (the path checks import code/deid):

  DEID_KEY_FILE=private/deid.key ./venv/bin/python tools/check_packaging.py \
      --manifest manifest.tsv --extra private/identities_extra_N.tsv \
      --attempt attempt-N --private private/aN --input input \
      [--rerun 300] [--other other-run/corpus] [--source code]

Each check runs when its inputs are given; everything else is skipped:
  paths    (--manifest; key; --extra)  map all manifest paths with the pipeline's own PathMapper:
           unique, same depth, same extension and compound suffix (.tar.gz, .prt.3), no
           case-only collisions, top-level archive names; renamed words below the project
           folder that are not the project's own account/project words (findability risk).
  corpus   (--attempt, --private)       one box's shard: every pathmap output present, nothing
           extra (.part/.copy/.ptx/.pvtmp leftovers, logs, maps), no symlinks (upload.py skips
           them), no empty output for a non-empty input, files over 2 GB, object count.
  archives (--attempt, --private, --input) every zip/container/tar/gz/Z/7z/rar output opens,
           same member count and member extensions as its input, zip CRCs pass.
  input    (--input, --manifest)        inputs still match the manifest MD5 (a handler that writes
           through a hard link would change the input and break the next run).
  dups     (--manifest, --private, --attempt) inputs with the same MD5 give identical outputs.
  repeat   (--other)                    a second run of the same files with the same key is
           byte-identical; PDFs that differ only in /ID or dates are named as such.
  rerun    (--rerun N + key + --extra)  runs deid.run again on N files of the shard (all archives,
           rare extensions, then random) with another hash seed and job count, compares with
           --attempt: the cheap way to test repeatability without a second full run.
  package  (--attempt)                  attempt-N holds only corpus/ code/ README.txt; every deid
           module that the code imports is in code/ (and every one --source has); third-party
           imports pinned in requirements + lock; system tools named in README's apt line;
           README's tools/flags exist; no key, token, map, log, pycache or venv in code/;
           README holds no identifier the pipeline's own scanner finds, and its corpus/ paths
           are final paths.

Output is PRIVATE (it prints original names); never ship it. Exit 1 if any FAIL.
"""
import argparse
import ast
import collections
import gzip
import hashlib
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

FAILS, WARNS = [], []
RESIDUE = {}  # original path -> project words still present in its final path
HERE = os.path.dirname(os.path.abspath(__file__))
LEFTOVER = re.compile(r"\.(part|copy|ptx|pvtmp|tmp|swp)$|~$")
PRIVATE_NAMES = re.compile(r"(^|/)(pathmap|found|defects|results|identities_extra[^/]*|word_df[^/]*)\.tsv$|"
                           r"(^|/)(run|upload|setup|fetch)\.log$|\.key$|(^|/)token$|(^|/)deid\.key")
ARCHIVE_EXT = {".zip", ".3mf", ".fcstd", ".f3d", ".f3z", ".odg", ".tar", ".tgz", ".gz", ".z", ".7z", ".rar"}
IMPORT_DIST = {"fitz": "pymupdf", "pymupdf": "pymupdf", "PIL": "pillow", "ahocorasick": "pyahocorasick",
               "spacy": "spacy", "pikepdf": "pikepdf", "olefile": "olefile", "py7zr": "py7zr",
               "zstandard": "zstandard", "numpy": "numpy", "en_core_web_md": "en_core_web_md",
               "en_core_web_sm": "en_core_web_sm", "en_core_web_lg": "en_core_web_lg"}
CMD_APT = {"7z": "p7zip-full", "7za": "p7zip-full", "unrar": "unrar-free", "unrar-free": "unrar-free",
           "rar": "rar", "compress": "ncompress", "uncompress": "ncompress", "tesseract": "tesseract-ocr",
           "qpdf": "qpdf", "exiftool": "libimage-exiftool-perl", "bsdtar": "libarchive-tools",
           "pdftotext": "poppler-utils", "mutool": "mupdf-tools", "gs": "ghostscript"}
BASE_CMDS = {"gzip", "tar", "bash", "sh", "cp", "mv"}


def fail(msg):
    FAILS.append(msg)
    print("FAIL  " + msg, flush=True)


def warn(msg):
    WARNS.append(msg)
    print("WARN  " + msg, flush=True)


def ok(msg):
    print("ok    " + msg, flush=True)


def head(title):
    print("\n== %s ==" % title, flush=True)


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_tsv(path):
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        return [line.rstrip("\n").split("\t") for line in f if line.strip() and not line.startswith("#")]


def suffix_chain(name, known):
    """The extension plus a second one when the pair is a compound suffix (.tar.gz, .prt.3)."""
    parts = name.split(".")
    if len(parts) < 2:
        return ""
    chain = "." + parts[-1]
    if len(parts) >= 3 and (parts[-2].lower() in known and (parts[-1].isdigit() or parts[-1].lower() in ("gz", "z", "bz2", "xz"))):
        chain = "." + parts[-2] + chain
    return chain


def find_code(arg):
    for cand in ([arg] if arg else []) + [os.path.join(HERE, "..", "code"), os.path.join(HERE, "..")]:
        if cand and os.path.isfile(os.path.join(cand, "deid", "run.py")):
            return os.path.abspath(cand)
    return None


# -- paths -----------------------------------------------------------------------

def check_paths(args, code):
    head("paths: every manifest path through the pipeline's PathMapper")
    sys.path.insert(0, code)
    try:
        from deid import run as deid_run
        from deid.identities import ALNUM, STOP, domain_labels, project_tokens
    except Exception as exc:
        fail("cannot import deid from %s (%s: %s)" % (code, type(exc).__name__, exc))
        return None
    deid_run.build(args.manifest, args.extra)
    mapper, ids = deid_run.STATE["mapper"], deid_run.STATE["ids"]
    rows = ids.rows
    known = {os.path.splitext(p)[1][1:].lower() for p, _ in rows}
    out, low, bad = {}, {}, 0
    words_other = collections.Counter()
    word_example = {}
    level = collections.Counter()
    both = []
    archives = []
    residue_words, residue_example, ext_ids = collections.Counter(), {}, []
    path_words = {}
    common_ext = {e for e, n in collections.Counter(os.path.splitext(p)[1][1:].lower() for p, _ in rows).items() if n >= 5}
    for path, _size in rows:
        new = mapper.map(path)
        a, b = path.split("/"), new.split("/")
        problems = []
        if new in out:
            problems.append("collides with " + out[new])
        out[new] = path
        if new.lower() in low and low[new.lower()] != path.lower():
            problems.append("case-only collision with " + low[new.lower()])
        low.setdefault(new.lower(), path)
        if len(a) != len(b):
            problems.append("depth %d -> %d" % (len(a), len(b)))
        if os.path.splitext(path)[1] != os.path.splitext(new)[1]:
            problems.append("extension %r -> %r" % (os.path.splitext(path)[1], os.path.splitext(new)[1]))
        elif suffix_chain(a[-1], known) != suffix_chain(b[-1], known):
            problems.append("compound suffix %r -> %r" % (suffix_chain(a[-1], known), suffix_chain(b[-1], known)))
        if any(p in ("", ".", "..") for p in b) or new.startswith("/"):
            problems.append("empty or dot component")
        if len(new.encode("utf-8", "surrogateescape")) > max(len(path.encode("utf-8", "surrogateescape")), 1024):
            problems.append("longer than the input path")
        if problems:
            bad += 1
            if bad <= 20:
                fail("%s -> %s: %s" % (path, new, "; ".join(problems)))
        if a[0] == "archives":
            archives.append((path, new))
            continue
        if len(a) < 4 or a[0] != "sources":
            continue
        group = a[1]
        own = set()
        toks = project_tokens(group, a[2])
        if group in ("web", "catalog"):
            dl = domain_labels(a[2])
            toks = [(lab, True) for lab in dl[0]] if dl else []
        for tok, _ in toks:
            own.update(ALNUM.findall(tok.lower()))
        changed_dir = False
        mine = set()
        for i in range(3, len(a)):
            if a[i] == b[i]:
                continue
            level["file" if i == len(a) - 1 else "folder"] += 1
            if i < len(a) - 1:
                changed_dir = True
            for m in re.finditer(r"[A-Za-z0-9]+", a[i]):
                if b[i][m.start():m.end()] != m.group() and m.group().lower() not in own:
                    mine.add(m.group().lower())
        for w in mine:  # counted once per path
            words_other[w] += 1
            word_example.setdefault(w, (path, new))
            path_words[path] = mine
        if changed_dir and a[-1] != b[-1]:
            both.append((path, new))
        # the project's own distinctive words should not survive in the final path
        strong = {w for w in own if len(w) >= 4 and w not in ids.english and w not in STOP and not w.isdigit()}
        if strong:
            flat = re.sub(r"[^a-z0-9]+", "", "/".join(b[3:]).lower())
            left = sorted(w for w in strong if w in flat)
            if left:
                RESIDUE[path] = left
                residue_words.update(left)
                residue_example.setdefault(left[0], new)
        ext = os.path.splitext(a[-1])[1]
        if len(ext) > 1 and ext.lower()[1:] not in common_ext and mapper.scanner.text(ext) != ext:
            ext_ids.append(new)
    if not bad:
        ok("%d paths: unique, same depth, same extension and compound suffix, no case-only collisions" % len(rows))
    print("      renamed below the project folder: %d folder components, %d file names; %d files with "
          "both a folder and the file name renamed" % (level["folder"], level["file"], len(both)))
    if RESIDUE:
        warn("%d final paths still hold a distinctive word of their own project's account/project name "
             "below the project folder (another separator or spelling; leakage, and README lists them): top %s" % (
                 len(RESIDUE), ", ".join("%s %d (e.g. %s)" % (w, n, residue_example[w][-90:])
                                         for w, n in residue_words.most_common(8))))
    if ext_ids:
        warn("%d files keep an identifier in what looks like their extension (kept by the path rules), e.g. %s" % (
            len(ext_ids), ext_ids[0][-120:]))
    for path, new in archives:
        a, b = path.split("/")[1], new.split("/")[1]
        kept = [t for t in re.split(r"__", a) if t in b.split("__")]
        print("      top-level archive: %s -> %s (kept parts: %s)" % (a, b, ", ".join(kept) or "none"))
    risky = {w: n for w, n in words_other.items() if w in ids.english or len(w) <= 3 or w.isdigit()}
    if risky:
        n_paths = sum(1 for ws in path_words.values() if ws & set(risky))
        warn("%d paths have a component renamed for a word that is a dictionary word, number or <= 3 "
             "letters and not part of the project's own name: the scorer may not find them. Top words "
             "(paths per word):" % n_paths)
        for w, n in sorted(risky.items(), key=lambda kv: -kv[1])[:30]:
            p, q = word_example[w]
            print("        %6d  %-18s e.g. %s\n                                    -> %s" % (n, w, p[-150:], q[-150:]))
    other = {w: n for w, n in words_other.items() if w not in risky}
    print("      other renamed words below the project folder (not the project's own): %d words, %d paths; top 25: %s" % (
        len(other), sum(other.values()),
        ", ".join("%s %d" % kv for kv in sorted(other.items(), key=lambda kv: -kv[1])[:25])))
    return {old: new for new, old in out.items()}


# -- corpus structure --------------------------------------------------------------

def load_pathmap(priv):
    path = os.path.join(priv, "pathmap.tsv")
    if not os.path.isfile(path):
        fail("no %s" % path)
        return None
    return {old: new for old, new in read_tsv(path)}


def check_corpus(args, pathmap):
    head("corpus: one output per input, nothing else")
    corpus = os.path.join(args.attempt, "corpus")
    if not os.path.isdir(corpus):
        fail("no %s" % corpus)
        return
    expected = set(pathmap.values())
    seen, extra, links, big, nfiles = set(), [], [], [], 0
    for root, dirs, files in os.walk(corpus):
        for d in dirs:
            if os.path.islink(os.path.join(root, d)):
                links.append(os.path.relpath(os.path.join(root, d), corpus))
        for name in files:
            p = os.path.join(root, name)
            rel = os.path.relpath(p, corpus)
            nfiles += 1
            if os.path.islink(p):
                links.append(rel)
            elif os.path.getsize(p) > 2 * 1024 ** 3:
                big.append(rel)
            if rel in expected:
                seen.add(rel)
            else:
                extra.append(rel)
    missing = sorted(expected - seen)
    (fail if missing else ok)("%d of %d expected outputs present" % (len(expected) - len(missing), len(expected)))
    for rel in missing[:10]:
        print("        missing: corpus/" + rel)
    if extra:
        kinds = collections.Counter("leftover " + LEFTOVER.search(r).group() if LEFTOVER.search(r) else
                                    "private file" if PRIVATE_NAMES.search(r) else "unmapped" for r in extra)
        fail("%d files in corpus/ that no input maps to (%s)" % (len(extra), ", ".join("%s: %d" % kv for kv in kinds.items())))
        for rel in extra[:10]:
            print("        extra: corpus/" + rel)
    else:
        ok("no extra files (no .part/.copy/.ptx/.pvtmp leftovers, logs or maps)")
    (fail if links else ok)("%d symlinks (upload.py skips symlinks)" % len(links))
    if big:
        warn("%d files over 2 GB are skipped by the scorer: %s" % (len(big), ", ".join(big[:3])))
    (fail if nfiles > 400000 else ok)("%d objects in this corpus/ (limit 400,000 for the whole attempt)" % nfiles)
    if args.input:
        empty = []
        for old, new in pathmap.items():
            src, dst = os.path.join(args.input, old), os.path.join(corpus, new)
            if os.path.isfile(src) and os.path.isfile(dst) and os.path.getsize(dst) == 0 < os.path.getsize(src):
                empty.append(new)
        (fail if empty else ok)("%d empty outputs of non-empty inputs" % len(empty))
        for rel in empty[:5]:
            print("        empty: corpus/" + rel)


# -- archives ------------------------------------------------------------------------

def members(path, ext):
    """(names, problem) for an archive file; names of regular members."""
    with open(path, "rb") as f:
        magic = f.read(8)
    try:
        if magic.startswith(b"PK"):
            with zipfile.ZipFile(path) as z:
                badcrc = z.testzip()
                names = [i.filename for i in z.infolist() if not i.is_dir()]
                return names, ("CRC error in " + badcrc) if badcrc else ""
        if ext in (".tar", ".tgz", ".gz") or magic[:2] == b"\x1f\x8b":
            try:
                with tarfile.open(path, "r:*") as t:
                    return [m.name for m in t.getmembers() if m.isreg()], ""
            except tarfile.TarError:
                with gzip.open(path, "rb") as g:
                    while g.read(1 << 20):
                        pass
                return ["(gzip stream)"], ""
        if magic[:2] == b"\x1f\x9d":
            done = subprocess.run(["gzip", "-t", path], capture_output=True)
            return ["(compress stream)"], "" if done.returncode == 0 else "gzip -t failed"
        if magic.startswith(b"7z\xbc\xaf\x27\x1c") or magic.startswith(b"Rar!"):
            if not shutil.which("7z"):
                return None, "7z not installed"
            done = subprocess.run(["7z", "l", "-slt", "-p", "-ba", path], capture_output=True, timeout=600)
            text = done.stdout.decode("utf-8", "surrogateescape")
            names = [b.split("\n", 1)[0][7:] for b in text.split("\n\n")
                     if b.startswith("Path = ") and "\nFolder = +" not in b]
            if done.returncode != 0:
                return names, "7z l rc=%d" % done.returncode
            return names, ""
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, str(exc)[:80])
    return None, "not an archive"


def check_archives(args, pathmap):
    head("archives: outputs open with the same members as their inputs")
    corpus = os.path.join(args.attempt, "corpus")
    n = bad = 0
    for old, new in sorted(pathmap.items()):
        ext = os.path.splitext(old)[1].lower()
        if ext not in ARCHIVE_EXT:
            continue
        src, dst = os.path.join(args.input, old), os.path.join(corpus, new)
        if not os.path.isfile(src) or not os.path.isfile(dst) or os.path.getsize(src) == 0:
            continue
        a, pa = members(src, ext)
        if a is None:
            continue  # the input is not a readable archive either (a source defect)
        n += 1
        b, pb = members(dst, ext)
        problem = ""
        if b is None or pb:
            problem = "output does not open (%s)" % (pb or "unknown")
        elif len(a) != len(b):
            problem = "%d members -> %d" % (len(a), len(b))
        elif sorted(os.path.splitext(x)[1].lower() for x in a) != sorted(os.path.splitext(x)[1].lower() for x in b):
            problem = "member extensions differ"
        if problem and not pa:
            bad += 1
            if bad <= 15:
                fail("corpus/%s: %s" % (new, problem))
    if not bad:
        ok("%d archives open with the same member count and member extensions" % n)


# -- inputs, duplicates, repeatability -------------------------------------------

def check_input(args, rows_md5, only=None):
    head("input: unchanged against the manifest MD5")
    n = bad = 0
    for path, digest in rows_md5.items():
        if only is not None and path not in only:
            continue
        src = os.path.join(args.input, path)
        if not digest or not os.path.isfile(src):
            continue
        n += 1
        if md5(src) != digest:
            bad += 1
            if bad <= 10:
                fail("input changed since download: %s" % path)
    if not bad:
        ok("%d inputs match their manifest MD5" % n)


def check_dups(args, pathmap, rows_md5, results):
    head("dups: identical inputs give identical outputs")
    corpus = os.path.join(args.attempt, "corpus")
    groups = collections.defaultdict(list)
    for old in pathmap:
        if rows_md5.get(old):
            groups[rows_md5[old]].append(old)
    stats = collections.Counter()
    examples = collections.defaultdict(list)
    for digest, olds in groups.items():
        if len(olds) < 2:
            continue
        outs = {}
        for old in olds:
            dst = os.path.join(corpus, pathmap[old])
            if os.path.isfile(dst):
                outs[old] = md5(dst)
        if len(set(outs.values())) <= 1:
            stats["same"] += 1
            continue
        projects = {"/".join(o.split("/")[:4]) for o in olds}
        where = "same project" if len(projects) == 1 else "across projects"
        ext = os.path.splitext(olds[0])[1].lower()
        key = "%s %s (%s)" % (where, ext, results.get(pathmap[olds[0]], "?"))
        stats[key] += 1
        examples[key].append([pathmap[o] for o in olds])
    total = sum(stats.values())
    if total == stats["same"]:
        ok("%d duplicate groups, all identical" % total)
        return
    for key, n in sorted(stats.items(), key=lambda kv: -kv[1]):
        if key == "same":
            continue
        (fail if key.startswith("same project") else warn)("%d duplicate groups differ: %s, e.g. %s" % (
            n, key, " | ".join(examples[key][0][:2])))
    print("      %d of %d duplicate groups identical" % (stats["same"], total))


def pdf_diff_kind(a, b):
    """Why two PDFs differ, when it is only the file identifier or dates."""
    try:
        import pikepdf
    except ImportError:
        return ""
    try:
        with pikepdf.open(a) as p, pikepdf.open(b) as q:
            ida, idb = p.trailer.get("/ID"), q.trailer.get("/ID")
            info_a = {k: str(v) for k, v in (p.docinfo or {}).items()}
            info_b = {k: str(v) for k, v in (q.docinfo or {}).items()}
            if ida is not None and idb is not None and [bytes(x) for x in ida] != [bytes(x) for x in idb]:
                return "trailer /ID differs"
            if info_a != info_b:
                return "Info differs in " + ",".join(sorted(k for k in set(info_a) | set(info_b) if info_a.get(k) != info_b.get(k)))
    except Exception:
        return ""
    return "content differs"


def compare_trees(a, b, label, rels=None):
    if rels is None:
        rels = set()
        for root, _d, files in os.walk(a):
            for name in files:
                rels.add(os.path.relpath(os.path.join(root, name), a))
    diff = collections.Counter()
    shown = 0
    for rel in sorted(rels):
        x, y = os.path.join(a, rel), os.path.join(b, rel)
        if not os.path.isfile(y):
            diff["missing in second"] += 1
            continue
        if os.path.getsize(x) == os.path.getsize(y) and md5(x) == md5(y):
            continue
        ext = os.path.splitext(rel)[1].lower()
        why = pdf_diff_kind(x, y) if ext == ".pdf" else ("size differs" if os.path.getsize(x) != os.path.getsize(y) else "bytes differ")
        diff["%s: %s" % (ext, why)] += 1
        if shown < 15:
            shown += 1
            print("        differs (%s): %s" % (why, rel))
    if diff:
        fail("%s: %d of %d files not byte-identical: %s" % (label, sum(diff.values()), len(rels),
                                                          ", ".join("%s %d" % kv for kv in diff.most_common())))
    else:
        ok("%s: %d files byte-identical" % (label, len(rels)))


def check_rerun(args, code, pathmap, rows_md5):
    head("rerun: the same files again, another hash seed and job count")
    key = os.environ.get("DEID_KEY_FILE") or os.environ.get("DEID_KEY")
    if not key:
        fail("rerun needs DEID_KEY_FILE (the attempt's key)")
        return
    rng = random.Random(5)
    by_ext = collections.defaultdict(list)
    for old in sorted(pathmap):
        src = os.path.join(args.input, old)
        if os.path.isfile(src) and os.path.getsize(src) <= 40 << 20:
            by_ext[os.path.splitext(old)[1].lower()].append(old)
    pick = []
    for ext, olds in sorted(by_ext.items()):
        share = max(3, args.rerun // max(len(by_ext), 1))
        take = min(len(olds), max(20, share) if ext in ARCHIVE_EXT else share)
        pick += rng.sample(olds, take)
    work = tempfile.mkdtemp(prefix="pkcheck-", dir=args.private)
    shard = os.path.join(work, "shard.tsv")
    sizes = dict(read_manifest_sizes(args.manifest))
    with open(shard, "w", encoding="utf-8", errors="surrogateescape") as f:
        for old in pick:
            f.write("%s\t%d\n" % (old, sizes.get(old, 0)))
    out, priv = os.path.join(work, "corpus"), os.path.join(work, "private")
    cmd = [sys.executable, "-m", "deid.run", "--manifest", os.path.abspath(args.manifest), "--shard", shard,
           "--input", os.path.abspath(args.input), "--output", out, "--private", priv, "--jobs", "2"]
    if args.extra:
        cmd += ["--extra", os.path.abspath(args.extra)]
    env = dict(os.environ, PYTHONHASHSEED=str(rng.randrange(1, 1 << 30)))
    if env.get("DEID_KEY_FILE"):  # the run starts in the code folder
        env["DEID_KEY_FILE"] = os.path.abspath(env["DEID_KEY_FILE"])
    print("      %d files, PYTHONHASHSEED=%s, jobs 2, scratch %s" % (len(pick), env["PYTHONHASHSEED"], work), flush=True)
    done = subprocess.run(cmd, cwd=code, env=env, capture_output=True, timeout=args.rerun_timeout)
    if done.returncode != 0:
        fail("rerun failed rc=%d: %s" % (done.returncode, done.stderr.decode("utf-8", "replace")[-400:]))
        return
    compare_trees(os.path.join(args.attempt, "corpus"), out, "attempt vs rerun", {pathmap[o] for o in pick})
    if args.keep_rerun:
        print("      kept " + work)
    else:
        shutil.rmtree(work, ignore_errors=True)


def read_manifest_sizes(path):
    for cols in read_tsv(path):
        yield cols[0], int(cols[1])


# -- package -------------------------------------------------------------------------

def imports_of(path):
    """(local deid modules, third-party top-level modules, optional ones, commands, spacy models)."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    local, third, optional, cmds, models = set(), set(), set(), set(), set()

    def guarded(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.Try):
                return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                if node.module:
                    local.add(node.module.split(".")[0])
                else:
                    local.update(a.name for a in node.names)
            elif node.module:
                (optional if guarded(node) else third).add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top == "deid":
                    local.add(a.name.split(".")[1] if "." in a.name else "")
                else:
                    (optional if guarded(node) else third).add(top)
        elif isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant) \
                and isinstance(node.elts[0].value, str) and node.elts[0].value in set(CMD_APT) | BASE_CMDS:
            cmds.add(node.elts[0].value)
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
            if name == "which" and node.args and isinstance(node.args[0], ast.Constant):
                cmds.add(str(node.args[0].value))
            if name == "load" and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str) and node.args[0].value.startswith("en_core"):
                models.add(node.args[0].value)
        elif isinstance(node, ast.Tuple) and node.elts and all(isinstance(e, ast.Constant) for e in node.elts):
            vals = [e.value for e in node.elts]
            if vals and vals[0] in CMD_APT and all(isinstance(v, str) for v in vals) and len(vals) <= 3:
                cmds.update(v for v in vals if v in CMD_APT)
    local.discard("")
    return local, third, optional, cmds, models


def req_names(path):
    names = {}
    if not os.path.isfile(path):
        return names
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip() if not line.lstrip().startswith(("en_core", "-e")) else line.strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"([A-Za-z0-9_.\-]+)\s*(.*)", line)
        if m:
            names[m.group(1).lower().replace("_", "-")] = m.group(2).strip()
    return names


def check_package(args, code_src, pathmap):
    head("package: attempt folder, code/ and README.txt")
    att = args.attempt
    entries = sorted(os.listdir(att))
    extra = [e for e in entries if e not in ("corpus", "code", "README.txt")]
    (fail if extra else ok)("attempt folder holds %s" % ", ".join(entries))
    for need in ("code", "README.txt"):
        if need not in entries:
            fail("no %s in %s" % (need, att))
    code = os.path.join(att, "code")
    readme_path = os.path.join(att, "README.txt")
    if not os.path.isdir(code):
        return
    pkg = os.path.join(code, "deid")
    have = {f[:-3] for f in os.listdir(pkg) if f.endswith(".py")} if os.path.isdir(pkg) else set()
    if not have:
        fail("no code/deid/*.py")
        return
    local, third, optional, cmds, models = set(), set(), set(), set(), set()
    tool_third = set()
    files = [os.path.join(pkg, f) for f in sorted(os.listdir(pkg)) if f.endswith(".py")]
    tools_dir = os.path.join(code, "tools")
    if os.path.isdir(tools_dir):
        files += [os.path.join(tools_dir, f) for f in sorted(os.listdir(tools_dir)) if f.endswith(".py")]
    for path in files:
        try:
            lo, th, op, cm, mo = imports_of(path)
        except SyntaxError as exc:
            fail("%s does not parse: %s" % (path, exc))
            continue
        if path.startswith(pkg):
            local |= lo
            third |= th
        else:
            tool_third.update(th)
        optional |= op
        cmds |= cm
        models |= mo
    missing = sorted(local - have)
    (fail if missing else ok)("deid modules imported by the code: %s%s" % (
        ", ".join(sorted(local)), ("; MISSING from code/deid: " + ", ".join(missing)) if missing else ""))
    if code_src and os.path.isdir(os.path.join(code_src, "deid")):
        src_have = {f[:-3] for f in os.listdir(os.path.join(code_src, "deid")) if f.endswith(".py")}
        src_local = set()
        for f in src_have:
            try:
                src_local |= imports_of(os.path.join(code_src, "deid", f + ".py"))[0]
            except SyntaxError:
                pass
        gone = sorted((src_local & src_have) - have)
        (fail if gone else ok)("every module the source tree imports is packaged%s" % (
            ": missing " + ", ".join(gone) if gone else ""))
        unused = sorted(src_have - have - src_local)
        if unused:
            print("      in the source tree, imported by nothing, not packaged: " + ", ".join(unused))
    std = set(getattr(sys, "stdlib_module_names", ())) | {"deid", "compression"}  # compression: 3.14 stdlib
    local_tools = {f[:-3] for f in os.listdir(tools_dir) if f.endswith(".py")} if os.path.isdir(tools_dir) else set()
    reqs = req_names(os.path.join(code, "requirements.txt"))
    lock = req_names(os.path.join(code, "requirements.lock.txt"))
    if not lock:
        fail("no code/requirements.lock.txt (pip freeze of the venv that produced the attempt)")
    for kind, mods in (("required", third), ("optional", optional - third), ("tools/", tool_third - third - optional)):
        for mod in sorted(mods - std - local_tools - set(have)):
            dist = IMPORT_DIST.get(mod, mod).lower()
            spec = reqs.get(dist)
            if spec is None and dist not in lock:
                (fail if kind == "required" else warn)("%s import %s (%s) is in neither requirements.txt nor the lock" % (kind, mod, dist))
                continue
            elif spec is None:
                warn("%s import %s (%s) only in the lock, not in requirements.txt" % (kind, mod, dist))
            elif not re.search(r"==|@", spec):
                warn("requirements.txt does not pin %s (lock has %s)" % (dist, lock.get(dist, "nothing")))
    for model in sorted(models):
        if model.replace("_", "-") not in reqs and model not in reqs:
            fail("spaCy model %s is not in requirements.txt" % model)
    ok("third-party imports: %s" % ", ".join(sorted((third | optional | tool_third) - std - local_tools - set(have))))
    readme = open(readme_path, encoding="utf-8", errors="surrogateescape").read() if os.path.isfile(readme_path) else ""
    apt = set()
    for line in readme.splitlines():
        if "apt-get install" in line or "apt install" in line:
            apt.update(t for t in line.split("install", 1)[1].split() if not t.startswith("-"))
    for cmd in sorted(cmds - BASE_CMDS):
        pkg_name = CMD_APT.get(cmd)
        if pkg_name and pkg_name not in apt:
            (warn if cmd in ("rar", "7za", "bsdtar", "unrar-free") else fail)(
                "the code runs '%s' (apt %s) but README's apt line does not install it" % (cmd, pkg_name))
    ok("system commands the code runs: %s" % ", ".join(sorted(cmds)))
    if os.path.isfile(os.path.join(code, "SYSTEM.txt")):
        ok("code/SYSTEM.txt records system package versions")
    else:
        warn("no code/SYSTEM.txt: tesseract, wamerican (/usr/share/dict/words), fonts, 7z versions change outputs; record them")
    # README references
    for tool in sorted(set(re.findall(r"tools/([A-Za-z0-9_]+\.(?:py|sh))", readme))):
        if not os.path.isfile(os.path.join(code, "tools", tool)):
            fail("README uses tools/%s, which is not in code/tools" % tool)
    run_py = os.path.join(pkg, "run.py")
    run_flags = set(re.findall(r'add_argument\("(--[a-z-]+)"', open(run_py).read())) if os.path.isfile(run_py) else set()
    for line in readme.splitlines():
        if "deid.run" in line or (line.startswith(" ") and "--" in line and "merge_found" not in line):
            for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", line):
                if run_flags and flag not in run_flags and flag not in ("--with-code",):
                    fail("README passes %s to deid.run, which run.py does not accept" % flag)
    mf = os.path.join(code, "tools", "merge_found.py")
    if os.path.isfile(mf):
        mf_src = open(mf).read()
        for flag in re.findall(r"merge_found\.py[^\n]*", readme):
            for f in re.findall(r"(--[a-z-]+)", flag):
                if f not in mf_src:
                    fail("README passes %s to merge_found.py, which it does not accept" % f)
    # secrets and private files
    key_value = ""
    if os.environ.get("DEID_KEY_FILE") and os.path.isfile(os.environ["DEID_KEY_FILE"]):
        key_value = open(os.environ["DEID_KEY_FILE"], encoding="utf-8").read().strip()
    key_value = key_value or os.environ.get("DEID_KEY", "")
    total = 0
    for root, dirs, fs in os.walk(code):
        for d in dirs:
            if d in ("__pycache__", "venv", ".venv", "private", "input", ".git", "corpus"):
                fail("code/ contains a %s folder" % d)
        for name in fs:
            p = os.path.join(root, name)
            rel = os.path.relpath(p, code)
            total += os.path.getsize(p)
            if PRIVATE_NAMES.search(rel) or name.endswith((".pyc", ".tsv", ".log")):
                fail("code/%s looks like a private run file, key, map or log" % rel)
            if os.path.getsize(p) > 5 << 20:
                warn("code/%s is %d MB" % (rel, os.path.getsize(p) >> 20))
                continue
            data = open(p, "rb").read()
            if key_value and key_value.encode() in data:
                fail("code/%s contains the DEID key" % rel)
            if re.search(rb"ya29\.[A-Za-z0-9_\-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}", data):
                fail("code/%s contains what looks like a credential" % rel)
    ok("code/ is %.1f MB" % (total / 1e6))
    if readme and key_value and key_value in readme:
        fail("README.txt contains the DEID key")
    check_readme(args, readme, pathmap)


def check_readme(args, readme, pathmap):
    if not readme:
        fail("README.txt missing or empty")
        return
    for section in ("How to run", "Limitations", "defects"):
        if section.lower() not in readme.lower():
            fail("README has no '%s' section" % section)
    # identifiers: the pipeline's own scanners over the README text
    try:
        from deid import run as deid_run
        st = deid_run.STATE
        if "content" in st:
            hits = []
            text = st["content"].text(readme)
            if text != readme:
                for m in re.finditer(r"[A-Za-z0-9][A-Za-z0-9._-]*", readme):
                    if text[m.start():m.end()] != m.group():
                        hits.append(m.group())
            hits = sorted(set(hits))
            (fail if hits else ok)("README text: %d words the pipeline itself would replace%s" % (
                len(hits), (": " + ", ".join(hits[:15])) if hits else ""))
    except Exception as exc:
        warn("README identifier scan skipped (%s)" % exc)
    if pathmap:
        finals, origs = set(pathmap.values()), {o for o, n in pathmap.items() if o != n}
        listed = [l.strip()[7:] for l in readme.splitlines() if re.match(r"\s*corpus/\S", l)]
        orig = [p for p in listed if p in origs]
        (fail if orig else ok)("README lists %d corpus/ paths; %d are original (input) paths" % (len(listed), len(orig)))
        unknown = [p for p in listed if p not in finals and p not in origs]
        if unknown and args.full_pathmap:
            warn("%d README paths are not outputs of this attempt, e.g. %s" % (len(unknown), unknown[0]))
        back = {n: o for o, n in pathmap.items()}
        leaks = [p for p in listed if p in back and RESIDUE.get(back[p])]
        (fail if leaks else ok)("README paths with a project's own name left in them: %d%s" % (
            len(leaks), (", e.g. corpus/" + leaks[0]) if leaks else ""))


# -- main ------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest")
    ap.add_argument("--extra", help="identities_extra file the attempt used")
    ap.add_argument("--attempt", help="attempt-N folder (corpus/, code/, README.txt)")
    ap.add_argument("--private", help="the run's --private folder (pathmap.tsv, results.tsv)")
    ap.add_argument("--input")
    ap.add_argument("--code", help="pipeline code dir holding deid/ (default: ../code or ..)")
    ap.add_argument("--source", help="development code dir, to check that packaging copied every module")
    ap.add_argument("--other", help="corpus/ of a second run of the same files with the same key")
    ap.add_argument("--rerun", type=int, default=0, help="re-run this many files and compare")
    ap.add_argument("--rerun-timeout", type=int, default=3600)
    ap.add_argument("--keep-rerun", action="store_true")
    ap.add_argument("--skip", default="", help="comma list of checks to skip (paths,corpus,archives,input,dups,package)")
    args = ap.parse_args()
    skip = set(args.skip.split(",")) - {""}
    for name in ("manifest", "extra", "private", "input", "other", "attempt"):
        v = getattr(args, name)
        if v and not os.path.exists(v):
            sys.exit("%s does not exist: %s" % (name, v))
    code = find_code(args.code)
    if code is None:
        sys.exit("no deid/run.py under --code, ../code or ..")
    args.full_pathmap = False
    full = None
    if args.manifest and "paths" not in skip:
        if not (os.environ.get("DEID_KEY_FILE") or os.environ.get("DEID_KEY")):
            sys.exit("set DEID_KEY_FILE (the attempt's key) for the path checks, or --skip paths")
        full = check_paths(args, code)
        args.full_pathmap = full is not None
    pathmap = load_pathmap(args.private) if args.private else None
    rows_md5 = {}
    results = {}
    if args.manifest:
        rows_md5 = {c[0]: (c[2] if len(c) > 2 else "") for c in read_tsv(args.manifest)}
    if args.private and os.path.isfile(os.path.join(args.private, "results.tsv")):
        results = {c[0]: c[1] for c in read_tsv(os.path.join(args.private, "results.tsv")) if len(c) > 1}
    if pathmap and full:
        wrong = [o for o, n in pathmap.items() if full.get(o) != n]
        (fail if wrong else ok)("the run's pathmap.tsv agrees with the full-manifest map (%d rows)" % len(pathmap))
    if args.attempt and pathmap is not None:
        if "corpus" not in skip:
            check_corpus(args, pathmap)
        if args.input and "archives" not in skip:
            check_archives(args, pathmap)
        if rows_md5 and "dups" not in skip:
            check_dups(args, pathmap, rows_md5, results)
    if args.input and rows_md5 and "input" not in skip:
        check_input(args, rows_md5, set(pathmap) if pathmap else None)
    if args.other and args.attempt:
        head("repeat: second run byte-identical")
        compare_trees(os.path.join(args.attempt, "corpus"), args.other, "attempt vs --other")
    if args.rerun and args.attempt and pathmap and args.input and args.manifest:
        check_rerun(args, code, pathmap, rows_md5)
    if args.attempt and "package" not in skip:
        check_package(args, args.source, full or pathmap)
    print("\n%d FAIL, %d WARN" % (len(FAILS), len(WARNS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
