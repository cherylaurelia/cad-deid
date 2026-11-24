#!/usr/bin/env python3
"""compare.py <runs dir> <name>... [--anchor=<run>:<attempt>] [--ref=<run>:<attempt>]

Calibration table: local estimate (est) vs official count (off) per category, for every run
named; official counts come from official.json next to this file.

With --anchor and --ref (default: m:3 and 2:2), every run also gets a prediction (pred):
  pred = official(anchor) + k x (est(run) - est(anchor)),
  k = (official(anchor) - official(ref)) / (est(anchor) - est(ref)), clipped to [0.25, 1.5],
  k = 1 outside the PDF block, and when the two estimates are within 3% of the maximum.
pred is meant for runs at least as good as attempt 2 (it extrapolates badly below that).
The replica reproduces attempt 2 but overstates the PDF gains of attempt 3 about threefold
(k ~ 0.3 for the PDF block), so pred is the number to gate on; est shows where a change acts."""
import json
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
official = json.load(open(os.path.join(here, "official.json")))
args = [a for a in sys.argv[1:] if not a.startswith("--")]
opts = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--"))
runs, names = args[0], args[1:]
anchor_run, anchor_att = opts.get("anchor", "m:3").split(":")
ref_run, ref_att = opts.get("ref", "2:2").split(":")


def load(n):
    p = os.path.join(runs, n, "report.json")
    if not os.path.exists(p):
        sys.exit("no report for %s: %s" % (n, p))
    return json.load(open(p))["categories"]


reps = {n: load(n) for n in set(names) | {anchor_run, ref_run} if os.path.exists(os.path.join(runs, n, "report.json"))}
cats = list(next(iter(reps.values())).keys())


def est(n, c):
    return reps[n][c]["estimate"] or 0


SLOPED = {"pdf_scoped_removal", "cross_pdf_consistency", "noncollision_triplets"}


def slope(c):
    """Only the PDF block gets a learned slope: elsewhere the a2 -> a3 differences are a few points
    and a slope fitted on them would be noise."""
    if anchor_run not in reps or ref_run not in reps or c not in SLOPED:
        return 1.0
    mx = reps[anchor_run][c]["max"]
    de = est(anchor_run, c) - est(ref_run, c)
    do = official[anchor_att][c] - official[ref_att][c]
    if abs(de) < 0.03 * mx:
        return 1.0
    return max(0.25, min(1.5, do / de))


print("%-26s %4s %5s" % ("category", "max", "k") + "".join(" | %-3s %5s %5s %5s" % (n, "est", "pred", "off") for n in names))
tot = {n: [0, 0.0, 0] for n in names}
for c in cats:
    k = slope(c)
    line = "%-26s %4d %5.2f" % (c, reps[names[0]][c]["max"], k)
    for n in names:
        e = est(n, c)
        mx = reps[n][c]["max"]
        pred = e if anchor_run not in reps else official[anchor_att][c] + k * (e - est(anchor_run, c))
        pred = max(0, min(mx, pred))
        off = official.get(n, {}).get(c)
        tot[n][0] += e
        tot[n][1] += pred
        tot[n][2] += off or 0
        line += " | %-3s %5d %5.0f %5s" % ("", e, pred, "-" if off is None else off)
    print(line)
line = "%-26s %4d %5s" % ("TOTAL", 1000, "")
for n in names:
    line += " | %-3s %5d %5.0f %5s" % ("", tot[n][0], tot[n][1], official.get(n, {}).get("_total", "-"))
print(line)
