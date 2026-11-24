#!/usr/bin/env python3
"""grade.py <key dir> <input root> <corpus dir> <pathmap.tsv> <attempt dir or -> <out dir> [jobs]

Local replica of the Machina CAD De-ID grader. Reads the answer key built by build_key.py, finds
every sampled file in <corpus dir> the way the scorer does (same extension, same non-identifying
path parts), compares it with its input, and prints the 15 official categories with estimated
pass counts scaled to the official maxima. Writes <out dir>/report.json and <out dir>/items.tsv
(every check item with its status and cause). Private: items.tsv names original identifiers.

<pathmap.tsv> (input path TAB output path, written by deid.run into --private) is used only to
pair files; the scorer's own path rule decides whether a file counts as found."""
import collections
import hashlib
import json
import multiprocessing
import os
import pickle
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import families  # noqa: E402
import features  # noqa: E402
import idkey  # noqa: E402
import pdfx  # noqa: E402
import scan  # noqa: E402

MAXIMA = collections.OrderedDict([
    ("leakage", 20), ("pseudonym_consistency", 15), ("distinct_identities", 10), ("relationships", 20),
    ("engineering_preservation", 15), ("native_ole_hidden", 10), ("packaging", 10),
    ("pdf_scoped_removal", 300), ("cross_pdf_consistency", 200), ("noncollision_triplets", 100),
    ("drawing_model", 200), ("ole_saved_account", 25), ("ole_residue", 25), ("ole_preview", 25),
    ("ole_container", 25)])
ENG_EXT = set(".step .stp .p21 .igs .iges .sat .dxf .stl .obj .wrl .x_t .xmt_txt .off .brep .unv .neu .fcstd .3mf "
              ".sldprt .sldasm .ipt .iam .par .asm .psm .png .jpg .jpeg .svg".split())

AUTO = None
CTX = {}


class Key:
    def __init__(self, d):
        self.dir = d
        self.files = {}
        for line in open(os.path.join(d, "files.tsv"), encoding="utf-8", errors="surrogateescape"):
            rel, ext, size, mc = line.rstrip("\n").split("\t")
            self.files[rel] = (ext, int(size), mc)
        self.ids = {}
        for line in open(os.path.join(d, "ids.tsv"), encoding="utf-8", errors="surrogateescape"):
            i, st, kinds, value, nf, ex = line.rstrip("\n").split("\t")
            kinds = kinds.split(",")
            self.ids[int(i)] = (idkey.adjust(kinds, value, st), kinds, value)
        self.file_ids = collections.defaultdict(dict)
        for line in open(os.path.join(d, "file_ids.tsv"), encoding="utf-8", errors="surrogateescape"):
            rel, i, src = line.rstrip("\n").split("\t")
            self.file_ids[rel].setdefault(int(i), set()).add(src)
        self.presence = collections.defaultdict(dict)
        for line in open(os.path.join(d, "presence.tsv"), encoding="utf-8", errors="surrogateescape"):
            rel, i, cls = line.rstrip("\n").split("\t")
            self.presence[rel][int(i)] = set(cls.split(","))

    def pdf_in(self, rel):
        p = os.path.join(self.dir, "pdf", phash(rel) + ".json")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f)


def phash(rel):
    return hashlib.md5(rel.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def init(ids_path, out_dir):
    global AUTO
    ids = []
    for line in open(ids_path, encoding="utf-8", errors="surrogateescape"):
        i, st, kinds, value = line.rstrip("\n").split("\t")[:4]
        if st in ("strong", "medium"):
            ids.append((int(i), value))
    AUTO = scan.build(ids)
    CTX["out"] = out_dir


def work(task):
    """All per-file comparisons for one (input, output) pair."""
    rel, out_rel, src, dst, ext, mc, deep, vals = task
    r = {"rel": rel}
    if not os.path.isfile(dst):
        r["missing"] = True
        return r
    try:
        if mc == "pdf":
            rec = pdfx.extract(dst, deep=True, values=vals)
            with open(os.path.join(CTX["out"], "pdf", phash(rel) + ".json"), "w") as f:
                json.dump(rec, f)
            r["pdf"] = True
            r["presence"] = {k: sorted(v) for k, v in scan.scan_file(dst, out_rel, AUTO, content=False).items()}
        else:
            r["presence"] = {k: sorted(v) for k, v in scan.scan_file(dst, out_rel, AUTO, content=deep).items()}
        if mc == "ole":
            r["ole"] = features.ole_compare(src, dst)
        if mc == "sw":
            r["sw_crc"] = (features.sw_crc_bad(src), features.sw_crc_bad(dst))
        if ext in features.ASM_EXT:
            r["refs_in"] = features.refs_of(src)
            r["refs_out"] = features.refs_of(dst)
        if ext in ENG_EXT:
            r["eng_in"] = features.eng_fp(src, ext)
            r["eng_out"] = features.eng_fp(dst, ext)
        with open(src, "rb") as f1, open(dst, "rb") as f2:
            a, b = f1.read(1 << 20), f2.read(1 << 20)
        r["same_head"] = a == b
        r["size_out"] = os.path.getsize(dst)
        if os.path.getsize(src) <= (64 << 20):
            r["md5_out"] = hashlib.md5(open(dst, "rb").read()).hexdigest()
    except Exception as exc:
        r["error"] = repr(exc)[:200]
    return r


def main():
    keydir, inp, corpus, pathmap, attempt, out = sys.argv[1:7]
    jobs = int(sys.argv[7]) if len(sys.argv) > 7 else 8
    t0 = time.time()
    os.makedirs(os.path.join(out, "pdf"), exist_ok=True)
    key = Key(keydir)
    pm = {}
    for line in open(pathmap, encoding="utf-8", errors="surrogateescape"):
        a, b = line.rstrip("\n").split("\t")
        pm[a] = b
    tasks = []
    for rel, (ext, size, mc) in key.files.items():
        o = pm.get(rel)
        if o is None:
            continue
        deep = mc != "pdf" and (any("content" in c or "name" in c for c in key.presence.get(rel, {}).values())
                                or mc in ("ole", "sw", "zip") or ext in (".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".z"))
        vals = []
        if mc == "pdf":  # identities of the PDF, searched in the raw bytes of the output
            vals = [(pid, v) for pid, v, _scope in families.pdf_identities(key, rel)]
        tasks.append((rel, o, os.path.join(inp, rel), os.path.join(corpus, o), ext, mc, deep, vals))
    tasks.sort(key=lambda t: -key.files[t[0]][1])
    cache = os.path.join(out, "results.pkl")
    if os.environ.get("GRADER_REUSE") and os.path.exists(cache):
        # re-score only (after a change to families.py): reuse the per-file comparisons
        with open(cache, "rb") as f:
            results = pickle.load(f)
        print("reused per-file results of %d files" % len(results), flush=True)
    else:
        results = {}
        with multiprocessing.get_context("fork").Pool(jobs, initializer=init, initargs=(os.path.join(keydir, "ids.tsv"), out)) as pool:
            for n, r in enumerate(pool.imap_unordered(work, tasks, chunksize=1), 1):
                results[r["rel"]] = r
                if n % 2000 == 0:
                    print("graded %d/%d files (%.0fs)" % (n, len(tasks), time.time() - t0), flush=True)
        with open(cache, "wb") as f:
            pickle.dump(results, f)
        print("per-file pass done (%.0fs)" % (time.time() - t0), flush=True)
    report = families.run_all(key, pm, results, inp, corpus, attempt, out)
    report["secs"] = round(time.time() - t0)
    with open(os.path.join(out, "report.json"), "w") as f:
        json.dump(report, f, indent=1, default=str)
    families.print_report(report, MAXIMA)


if __name__ == "__main__":
    main()
