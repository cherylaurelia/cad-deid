"""De-identify a shard of the corpus.

python -m deid.run --manifest manifest.tsv --shard shard.tsv --input input \
    --output attempt/corpus --private private --jobs 8

--manifest is the full input listing (identifiers and output paths are derived from it,
so every machine agrees); --shard lists the rows this machine processes (default: all).
Private logs (path map, found identities, defects) go to --private, never to --output.
"""
import argparse
import collections
import multiprocessing
import os
import re
import sys
import time

from . import formats
from .identities import Identities, read_manifest
from .paths import PathMapper
from .pseudo import Pseudo, load_key
from .scan import Scanner

STATE = {}


def build(manifest, extra=None):
    pseudo = Pseudo(load_key())
    ids = Identities(manifest, extra=extra)
    STATE["fixed"] = pseudo.reserve(ids.all_words())
    STATE["ids"] = ids
    STATE["content"] = Scanner(pseudo, ids.patterns(), "text")
    STATE["binary"] = Scanner(pseudo, ids.patterns(), "binary")
    STATE["mapper"] = PathMapper(pseudo, Scanner(pseudo, ids.path_patterns(), "path"), ids.english)
    STATE["ctx"] = formats.Ctx(STATE["content"], STATE["binary"], STATE["mapper"], None)
    STATE["ctx"].global_map = {p: pseudo.span(p) for p in ids.words | ids.phrases
                               if p.isascii() and pseudo.span(p) != p}
    return ids


PAIR_DRAW = set(".slddrw .idw .dft .drw .pdf .dxf .dwg .catdrawing".split())
PAIR_MODEL = set(".sldprt .sldasm .ipt .iam .par .psm .asm .prt .step .stp .fcstd .iges .igs .x_t .catpart .catproduct".split())
PAIR_KEEP = set(".slddrw .sldprt .sldasm .idw .ipt .iam .dft .par .psm .catpart .catproduct .catdrawing".split())


PAIR_KEEP_ALL = (PAIR_DRAW | PAIR_MODEL) - {".step", ".stp"}
PAIR_DRAW_NATIVE = {".slddrw", ".idw", ".dft", ".catdrawing"}
_VERSION = re.compile(r"\.\d+$")


def _is_ole(path):
    try:
        with open(path, "rb") as f:
            return f.read(8) == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    except OSError:
        return False


def paired_natives(rows, every=False, pdf=True):
    """Native SolidWorks / Inventor / Solid Edge / CATIA files that form a drawing/model pair
    (same folder, same stem, a drawing and a model extension). With DEID_KEEP_PAIRS=1 they
    are copied byte for byte: rewriting any byte of them broke the drawing/model check."""
    groups = collections.defaultdict(list)
    for path, _size in rows:
        stem, ext = os.path.splitext(_VERSION.sub("", os.path.basename(path)) if every else os.path.basename(path))
        groups[(os.path.dirname(path), stem.lower())].append((ext.lower(), path))
    keep = set()
    for files in groups.values():
        exts = {e for e, _ in files}
        if exts & PAIR_DRAW and exts & PAIR_MODEL:
            keep.update(p for e, p in files if e in (PAIR_KEEP_ALL if every else PAIR_KEEP) and (pdf or e != ".pdf"))
    return keep


def work(task):
    src, dst, ext, path = task
    ctx = STATE["ctx"]
    if path in STATE.get("keep", ()):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        formats.place(src, dst)
        return src, dst, "kept-pair", 0, "", {}
    ctx.rules = STATE["mapper"].rules_of(path)
    ctx.project = STATE["mapper"].project_names(path)
    ctx.parts = STATE["mapper"].scope_parts(path)
    ctx.text.found = []
    ctx.binary.found = []
    try:
        action, n, defect = formats.process(src, dst, ext, ctx)
    except Exception as exc:  # never drop a file: keep the original bytes and report it
        try:
            if os.path.lexists(dst):
                os.remove(dst)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            formats.place(src, dst)
            action, n, defect = "error-copied", 0, "could not be processed (%s); kept as is" % type(exc).__name__
        except Exception as exc2:
            return src, dst, "FAILED", 0, "%s / %s" % (exc, exc2), {}
    return src, dst, action, n, defect, collections.Counter(ctx.text.found + ctx.binary.found)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--shard")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--private", required=True)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--extra", help="identities found by an earlier pass (tools/merge_found.py)")
    ap.add_argument("--only", help="file of input paths: process just these rows of the shard")
    args = ap.parse_args()
    for path in (args.manifest, args.shard or args.manifest):
        if not os.path.isfile(path):
            sys.exit("not a file: %s" % path)
    if not os.path.isdir(args.input):
        sys.exit("not a directory: %s" % args.input)
    os.makedirs(os.path.join(args.private, "tmp"), exist_ok=True)
    os.makedirs(args.output, exist_ok=True)

    t0 = time.time()
    if args.extra and not os.path.isfile(args.extra):
        sys.exit("not a file: %s" % args.extra)
    ids = build(args.manifest, args.extra)
    mapper = STATE["mapper"]
    STATE["ctx"].tmp = os.path.join(args.private, "tmp")
    print("identifiers: %d words, %d phrases, %d path-only; %d reserved pseudonyms, %d collisions resolved (%.0fs)" % (
        len(ids.words), len(ids.phrases), len(ids.path_only), len(ids.all_words()), STATE["fixed"],
        time.time() - t0), flush=True)

    # Output paths for the whole corpus must be unique.
    seen = {}
    for path, _size in ids.rows:
        new = mapper.map(path)
        if new in seen:
            sys.exit("output path collision: two inputs map to %r" % new)
        seen[new] = path
    path_map = {old: new for new, old in seen.items()}
    mode = os.environ.get("DEID_KEEP_PAIRS")
    if mode in ("dir", "dir-new"):
        # every SolidWorks / Inventor / Solid Edge / CATIA file in a folder that holds a native
        # drawing: drawings reference models by path, not only by a shared file name
        draw = {os.path.dirname(p) for p, _ in ids.rows if os.path.splitext(p)[1].lower() in PAIR_DRAW_NATIVE}
        STATE["keep"] = paired_natives(ids.rows) | {
            p for p, _ in ids.rows if os.path.splitext(p)[1].lower() in PAIR_KEEP and os.path.dirname(p) in draw}
        if mode == "dir-new":  # OLE files (older SolidWorks, Inventor, Solid Edge) keep the full rewrite
            STATE["keep"] = {p for p in STATE["keep"] if not _is_ole(os.path.join(args.input, p))}
        print("drawing/model pairs: %d native files kept byte for byte" % len(STATE["keep"]), flush=True)
    elif mode in ("1", "all", "natives"):
        # "all": every member of a pair (PDF/DXF drawings, Creo .prt.N, FCStd too; not STEP);
        # "natives": the same without the PDF drawings
        STATE["keep"] = paired_natives(ids.rows, mode != "1", mode != "natives")
        print("drawing/model pairs: %d native files kept byte for byte" % len(STATE["keep"]), flush=True)
    print("path map: %d paths, %d renamed (%.0fs)" % (
        len(path_map), sum(1 for o, n in path_map.items() if o != n), time.time() - t0), flush=True)

    rows = read_manifest(args.shard) if args.shard else ids.rows
    if args.only:
        with open(args.only, encoding="utf-8", errors="surrogateescape") as f:
            wanted = {line.rstrip("\n") for line in f if line.strip()}
        rows = [row for row in rows if row[0] in wanted]
    if args.limit:
        rows = rows[:args.limit]
    tasks, missing = [], []
    with open(os.path.join(args.private, "pathmap.tsv"), "w", encoding="utf-8", errors="surrogateescape") as f:
        for path, size in rows:
            src = os.path.join(args.input, path)
            if not os.path.isfile(src):
                missing.append(path)
                continue
            new = path_map[path]
            f.write("%s\t%s\n" % (path, new))
            ext = os.path.splitext(path)[1].lower()
            tasks.append((src, os.path.join(args.output, new), ext, path))
    if missing:
        sys.exit("%d input files are missing, e.g. %s" % (len(missing), missing[0]))
    tasks.sort(key=lambda t: -os.path.getsize(t[0]))

    actions = collections.Counter()
    changed = collections.Counter()
    found = collections.Counter()
    failed = 0
    with multiprocessing.get_context("fork").Pool(args.jobs) as pool, \
            open(os.path.join(args.private, "results.tsv"), "w", encoding="utf-8", errors="surrogateescape") as res, \
            open(os.path.join(args.private, "defects.tsv"), "w", encoding="utf-8", errors="surrogateescape") as defects:
        for i, (src, dst, action, n, defect, hits) in enumerate(pool.imap_unordered(work, tasks, chunksize=1), 1):
            actions[action] += 1
            if n:
                changed[action] += 1
            if action == "FAILED":
                failed += 1
            rel = os.path.relpath(dst, args.output)
            res.write("%s\t%s\t%d\n" % (rel, action, n))
            if defect:
                defects.write("%s\t%s\n" % (rel, defect))
            found.update(hits)
            if i % 5000 == 0:
                print("  %d/%d files (%.0fs)" % (i, len(tasks), time.time() - t0), flush=True)
    with open(os.path.join(args.private, "found.tsv"), "w", encoding="utf-8", errors="surrogateescape") as f:
        for (kind, value), n in sorted(found.items(), key=lambda kv: -kv[1]):
            f.write("%s\t%d\t%s\n" % (kind, n, value.replace("\t", " ").replace("\n", " ")))
    print("done: %d files in %.0fs" % (len(tasks), time.time() - t0))
    for action in sorted(actions):
        print("  %-16s %7d files, %7d changed" % (action, actions[action], changed[action]))
    if failed:
        sys.exit("%d files FAILED (see results.tsv)" % failed)


if __name__ == "__main__":
    main()
