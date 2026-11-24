#!/usr/bin/env python3
"""check_paths.py <pathmap.tsv> [samples] : sanity checks on an input->output path map.

Checks: same number of components, same extension, same folder depth, unique outputs.
Prints a few renamed examples per source group for eyeballing (private, not for publishing).
"""
import collections, os, random, sys

def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    nshow = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    rows = [l.rstrip("\n").split("\t") for l in open(sys.argv[1], encoding="utf-8", errors="surrogateescape")]
    bad = 0
    outs = set()
    by_group = collections.defaultdict(list)
    changed_deep = 0
    for old, new in rows:
        a, b = old.split("/"), new.split("/")
        if len(a) != len(b) or os.path.splitext(old)[1] != os.path.splitext(new)[1] or new in outs:
            bad += 1
            print("BAD", old, "->", new)
        outs.add(new)
        if a[3:] != b[3:]:
            changed_deep += 1
            by_group["deep:" + (a[1] if len(a) > 1 else a[0])].append((old, new))
        by_group[a[1] if len(a) > 2 else a[0]].append((old, new))
    print("rows", len(rows), "bad", bad, "renamed below the project folder", changed_deep)
    rng = random.Random(3)
    for g in sorted(by_group):
        for old, new in rng.sample(by_group[g], min(nshow, len(by_group[g]))):
            print("[%s]\n   %s\n-> %s" % (g, old[:230], new[:230]))
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
