#!/usr/bin/env python3
"""uniform_sample.py <manifest.tsv> <existing sample.tsv> <n> [seed] > extra.tsv

Corpus-uniform supplement to the stratified sample: n files drawn uniformly from the manifest
(<= 60 MB, not already sampled), plus, for each drawn file, the files of the same folder with the
same stem (drawing/model partners) and, for drawn assemblies and drawings, up to 20 model files of
the same folder (reference targets). The stratified sample under-represents the five giant
repositories that hold half of the corpus (usnistgov 37k, step.parts 17k + 13k, MFCAD 15k files);
a scorer that draws files uniformly lands there half the time."""
import collections
import os
import random
import re
import sys

VER = re.compile(r"\.\d+$")
ASM = set(".sldasm .slddrw .iam .idw .ipn .asm .dft .catproduct .catdrawing .drw".split())
MODEL = set(".sldprt .sldasm .ipt .iam .par .asm .psm .catpart .prt .step .stp .igs .iges .x_t .sat .stl".split())


def stem(p):
    return os.path.splitext(VER.sub("", os.path.basename(p)))[0].lower()


def main():
    man, have, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 20261121
    rows = [l.rstrip("\n").split("\t") for l in open(man, encoding="utf-8", errors="surrogateescape") if not l.startswith("#")]
    taken = {l.split("\t")[0] for l in open(have, encoding="utf-8", errors="surrogateescape")}
    by_dir = collections.defaultdict(list)
    for r in rows:
        by_dir[os.path.dirname(r[0])].append(r)
    pool = [r for r in rows if r[0] not in taken and int(r[1]) <= 60e6]
    rng = random.Random(seed)
    pick = rng.sample(pool, n)
    out = {r[0]: r for r in pick}
    for r in pick:
        mates = by_dir[os.path.dirname(r[0])]
        partners = {}
        for m in mates:  # one partner per other extension (drawing <-> model), not every Creo version
            ext = os.path.splitext(VER.sub("", m[0]))[1].lower()
            if stem(m[0]) == stem(r[0]) and int(m[1]) <= 60e6 and ext not in partners:
                partners[ext] = m
        for m in list(partners.values())[:4]:
            out[m[0]] = m
        if os.path.splitext(VER.sub("", r[0]))[1].lower() in ASM:
            models = [m for m in mates if os.path.splitext(VER.sub("", m[0]))[1].lower() in MODEL and int(m[1]) <= 60e6]
            rng.shuffle(models)
            for m in models[:20]:
                out[m[0]] = m
    for p in sorted(out):
        if p not in taken:
            sys.stdout.write("\t".join(out[p]) + "\n")
    sys.stderr.write("uniform %d + partners -> %d files, %.2f GB\n" % (n, len([p for p in out if p not in taken]),
                     sum(int(out[p][1]) for p in out if p not in taken) / 1e9))


if __name__ == "__main__":
    main()
