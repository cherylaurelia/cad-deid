#!/usr/bin/env python3
"""build_key.py <input root> <sample.tsv> <manifest.tsv> <key dir> [jobs] [--disc=<earlier disc.tsv> [--disc-files=<list it covered>]]

Builds the grader's answer key from the INPUT sample only (run once per sample; ~5 min on 8 cores):
  key/disc.tsv        identity-like fields found in every input file (kind, value, rel, location)
  key/pdf/<h>.json    every channel of every input PDF (pdfx.extract)
  key/ids.tsv         identity table: id, strength, kinds, value, #files, example file
  key/file_ids.tsv    rel, id, source (path / field / pdf)
  key/presence.tsv    rel, id, classes: where each identity is readable in the input file
  key/files.tsv       rel, ext, size, magic class
Private: holds original identifiers. Keep it on the box.

Discovery over every view of every file is the slow step (~1 h on 8 cores for 8.9k files). With
--disc=<file>, rows of an earlier discovery over the same input (same format: kind, value, rel, loc;
e.g. the corpus auditor's ~/audit/disc_in.tsv) are reused, and only the SolidWorks XML chunks are
read again for the XML fields added since (custom.xml properties, dc:lastModifiedBy)."""
import collections
import hashlib
import json
import multiprocessing
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discover_fields  # noqa: E402
import idkey  # noqa: E402
import pdfident  # noqa: E402
import pdfx  # noqa: E402
import scan  # noqa: E402
import views  # noqa: E402

ROOT = None
KEY = None
REUSE = False
REUSED = set()


def phash(rel):
    return hashlib.md5(rel.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def magic_class(path):
    try:
        with open(path, "rb") as f:
            h = f.read(64)
    except OSError:
        return "missing"
    if not h:
        return "empty"
    if h[:8] == views.OLE_MAGIC:
        return "ole"
    if h[:5] == b"%PDF-":
        return "pdf"
    if views.SW_MARK in h:
        return "sw"
    if h[:4] == b"PK\x03\x04":
        return "zip"
    return "other"


def is_pdf(rel, mc):
    return mc == "pdf" or rel.lower().endswith(".pdf")


def pass_a(rel):
    try:
        return _pass_a(rel)
    except Exception as exc:  # one unreadable file must not stop the key
        path = os.path.join(ROOT, rel)
        return (rel, os.path.splitext(rel)[1].lower(), os.path.getsize(path) if os.path.exists(path) else -1,
                magic_class(path), [("error", repr(exc)[:100], "-")], [])


def _pass_a(rel):
    path = os.path.join(ROOT, rel)
    mc = magic_class(path)
    ext = os.path.splitext(rel)[1].lower()
    size = os.path.getsize(path) if os.path.exists(path) else -1
    rows, pdfrows = [], []
    if is_pdf(rel, mc) and mc == "pdf":
        cached = os.path.join(KEY, "pdf", phash(rel) + ".json")
        rec = None
        if os.path.exists(cached):  # re-running the key build reuses the extracted input PDFs
            try:
                with open(cached) as f:
                    rec = json.load(f)
            except Exception:
                rec = None
        if rec is None:
            rec = pdfx.extract(path)
            with open(cached, "w") as f:
                json.dump(rec, f)

        def add(i, value, kind, st="strong"):
            value = value.strip().strip(",;:()[]\"'")
            if len(idkey.norm(value)) >= 3 and not idkey.norm(value).isdigit():
                pdfrows.append((kind, st, value))
        pdfident.meta_identities(add, 0, rec)
        m = rec.get("mupdf", {})
        pdfident.text_identities(add, 0, "\n".join([rec.get("pdftotext", ""), m.get("text", "")]), "text")
        for a in m.get("annots", []):
            pdfident.text_identities(add, 0, a, "annot")
        for u in m.get("links", []):
            pdfident.text_identities(add, 0, u, "link")
        for e in m.get("embfiles", []):
            pdfident.text_identities(add, 0, e, "attachment")
        pdfident.titleblock_identities(add, 0, m.get("words", []))
    elif REUSE and rel in REUSED:
        if mc == "sw":
            try:
                with open(path, "rb") as f:
                    data = f.read()
                for name, raw in views.sw_chunks(data):
                    if b"<" not in raw[:200]:
                        continue
                    for k, v in discover_fields.extract("sw-chunk", raw, ".xml", False):
                        if k.startswith(("xmlprop:", "xml-dc:lastmodifiedby", "xml-cp:lastmodifiedby", "xml-dc:creator")):
                            v = discover_fields.text_of(v).replace("\t", " ").replace("\n", " ").strip()
                            if 2 <= len(v) <= 300:
                                rows.append((k, v, "sw:" + name[:80]))
            except Exception as exc:
                rows.append(("error", repr(exc)[:100], "-"))
    else:
        tmp = tempfile.mkdtemp(prefix="gkey", dir=os.environ.get("GRADER_TMP", "/tmp"))
        try:
            for loc, kind, b in views.views(path, tmp=tmp):
                inner_ext = os.path.splitext(loc.split(":")[-1] if loc != "file" else path)[1].lower()
                binary = kind in ("ole-stream", "sw-chunk") or b"\x00" in b[:8192]
                # caps keep one huge view from taking minutes: identity fields sit in headers,
                # property streams and the first megabytes of text formats
                b = b[:4 << 20] if binary else b[:16 << 20]
                for k, v in discover_fields.extract(kind, b, inner_ext, binary):
                    v = discover_fields.text_of(v).replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()
                    if 2 <= len(v) <= 300:
                        if k == "prop":
                            k = "prop:" + loc.split("#")[-1]
                        rows.append((k, v, loc[:160].replace("\t", " ").replace("\n", " ")))
        except Exception as exc:
            rows.append(("error", repr(exc)[:100], "-"))
        finally:
            os.system("rm -rf '%s'" % tmp)
    return rel, ext, size, mc, rows, pdfrows


AUTO = None


def init_b(ids_path):
    global AUTO
    ids = []
    for line in open(ids_path, encoding="utf-8", errors="surrogateescape"):
        i, st, kinds, value = line.rstrip("\n").split("\t")[:4]
        if st in ("strong", "medium"):
            ids.append((int(i), value))
    AUTO = scan.build(ids)


def pass_b(task):
    rel, mc = task
    path = os.path.join(ROOT, rel)
    try:
        found = scan.scan_file(path, rel, AUTO, content=mc != "pdf")
    except Exception:
        found = {}
    return rel, found


def main():
    global ROOT, KEY, REUSE
    opts = [a for a in sys.argv[1:] if a.startswith("--")]
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    ROOT, sample, manifest, KEY = args[:4]
    jobs = int(args[4]) if len(args) > 4 else 8
    reuse = [o.split("=", 1)[1] for o in opts if o.startswith("--disc=")]
    REUSE = bool(reuse)
    covered = [o.split("=", 1)[1] for o in opts if o.startswith("--disc-files=")]
    if REUSE:
        # files the earlier discovery read (default: every file it has a row for); the rest get a full pass
        if covered:
            REUSED.update(l.rstrip("\n").split("\t")[0] for l in open(covered[0], encoding="utf-8", errors="surrogateescape"))
    os.makedirs(os.path.join(KEY, "pdf"), exist_ok=True)
    t0 = time.time()
    rels = [l.rstrip("\n").split("\t")[0] for l in open(sample, encoding="utf-8", errors="surrogateescape")
            if l.strip() and not l.startswith("#")]
    rels.sort(key=lambda r: -os.path.getsize(os.path.join(ROOT, r)) if os.path.exists(os.path.join(ROOT, r)) else 0)
    files = {}
    disc = collections.defaultdict(list)
    pdfids = collections.defaultdict(list)
    if REUSE:
        want = set(rels)
        for line in open(reuse[0], encoding="utf-8", errors="surrogateescape"):
            r = line.rstrip("\n").split("\t")
            if len(r) >= 3 and r[2] in want and not r[2].lower().endswith(".pdf"):
                disc[r[2]].append((r[0], r[1]))
                if not covered:
                    REUSED.add(r[2])
        print("reused discovery rows for %d files (%.0fs)" % (len(disc), time.time() - t0), flush=True)
    with multiprocessing.get_context("fork").Pool(jobs) as pool, \
            open(os.path.join(KEY, "disc.tsv"), "w", encoding="utf-8", errors="surrogateescape") as f:
        for n, (rel, ext, size, mc, rows, prow) in enumerate(pool.imap_unordered(pass_a, rels, chunksize=1), 1):
            files[rel] = (ext, size, mc)
            for k, v, loc in rows:
                f.write("%s\t%s\t%s\t%s\n" % (k, v, rel, loc))
                disc[rel].append((k, v))
            pdfids[rel] = prow
            if n % 1000 == 0:
                print("pass A %d/%d (%.0fs)" % (n, len(rels), time.time() - t0), flush=True)
    with open(os.path.join(KEY, "files.tsv"), "w", encoding="utf-8", errors="surrogateescape") as f:
        for rel, (ext, size, mc) in sorted(files.items()):
            f.write("%s\t%s\t%d\t%s\n" % (rel, ext, size, mc))

    # identity table
    table = {}   # norm -> [strength, kinds set, value, files set]
    file_ids = []
    rank = {"strong": 3, "medium": 2, "weak": 1, "drop": 0}

    def add(rel, kind, value, st, src):
        value = " ".join(value.split())
        n = idkey.norm(value)
        if len(n) < 3 and value.isascii() or n.isdigit():
            return
        rec = table.get(n)
        if rec is None:
            rec = table[n] = [st, set(), value, set()]
        elif rank[st] > rank[rec[0]]:
            rec[0] = st
        rec[1].add(kind)
        rec[3].add(rel)
        file_ids.append((rel, n, src))

    for rel in rels:
        for kind, value, st in idkey.path_identities(rel):
            add(rel, kind, value, st, "path")
        for k, v in disc.get(rel, []):
            r = idkey.field_identity(k, v)
            if r:
                st = idkey.field_strength(r[0], r[1])
                if st != "drop":
                    add(rel, r[0], r[1], st if r[0] not in ("email",) else "strong", "field")
        for kind, st, value in pdfids.get(rel, []):
            if idkey.is_generic_value(value) and not kind.endswith(("email", "url-host", "phone")):
                continue
            s2 = idkey.strength(value)
            st = "medium" if st == "medium" and s2 == "strong" else s2 if s2 != "drop" else "weak"
            add(rel, "pdf-" + kind.split(":")[0] + ":" + kind.split(":")[-1], value, st, "pdf")
    # identities that appear in a quarter of all projects are format words, not identities
    nproj = len({idkey.project_key(r) for r in rels})
    ids = {}
    with open(os.path.join(KEY, "ids.tsv"), "w", encoding="utf-8", errors="surrogateescape") as f:
        for k, (n, (st, kinds, value, fs)) in enumerate(sorted(table.items())):
            projs = {idkey.project_key(r) for r in fs}
            if len(projs) > max(12, nproj // 8) and not any(x.startswith("path-owner") for x in kinds):
                st = "weak"
            ids[n] = k
            f.write("%d\t%s\t%s\t%s\t%d\t%s\n" % (k, st, ",".join(sorted(kinds)), value.replace("\t", " "), len(fs), sorted(fs)[0]))
    with open(os.path.join(KEY, "file_ids.tsv"), "w", encoding="utf-8", errors="surrogateescape") as f:
        seen = set()
        for rel, n, src in file_ids:
            if (rel, n, src) in seen:
                continue
            seen.add((rel, n, src))
            f.write("%s\t%d\t%s\n" % (rel, ids[n], src))
    print("identities: %d (%s) (%.0fs)" % (len(table), dict(collections.Counter(v[0] for v in table.values())), time.time() - t0), flush=True)

    # presence of every identity in every input file
    tasks = [(rel, files[rel][2]) for rel in rels]
    with multiprocessing.get_context("fork").Pool(jobs, initializer=init_b, initargs=(os.path.join(KEY, "ids.tsv"),)) as pool, \
            open(os.path.join(KEY, "presence.tsv"), "w", encoding="utf-8", errors="surrogateescape") as f:
        for n, (rel, found) in enumerate(pool.imap_unordered(pass_b, tasks, chunksize=2), 1):
            for ident, cls in found.items():
                f.write("%s\t%d\t%s\n" % (rel, ident, ",".join(sorted(cls))))
            if n % 2000 == 0:
                print("pass B %d/%d (%.0fs)" % (n, len(tasks), time.time() - t0), flush=True)
    with open(os.path.join(KEY, "meta.json"), "w") as f:
        json.dump({"files": len(rels), "identities": len(table), "secs": round(time.time() - t0)}, f)
    print("key done in %.0fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
