#!/usr/bin/env python3
"""merge_found.py [--df=word_df.tsv] <found.tsv>... > identities_extra.tsv

Turns the identities that a first pass found inside files (authors, organisations, user
folders, e-mail names: deid.run writes them to <private>/found.tsv) into strong
identifiers for the second pass, so a name found in one file's metadata is also replaced
in every other file's text. Keeps multi-word names and distinctive single tokens; drops
everyday words, given names from the system word list, software names and placeholders.
With --df (the summed output of tools/word_df.py), a single word that occurs in the file
contents of 10 or more projects is dropped: it is part of a file format, not a name.
The output is a private file: pass it to deid.run --extra, never publish it.
"""
import os, re, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "code"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from deid.identities import STOP  # noqa: E402
from deid.scan import is_generic  # noqa: E402

WORD = re.compile(r"[A-Za-z0-9]+")
# Part and drawing numbers and project titles found in PDFs stay out: they name nobody, and a
# global entry would rename files. Names found only by a PDF's own text stay scoped to that PDF
# unless they are distinctive (see the filters below).
KINDS = ("person", "org", "user", "email", "pdf-person", "pdf-org", "pdf-email", "pdf-url")


def main():
    widespread = set()
    for arg in [a for a in sys.argv[1:] if a.startswith("--df=")]:
        sys.argv.remove(arg)
        with open(arg[5:], encoding="utf-8", errors="replace") as f:
            for line in f:
                word, _, count = line.rstrip("\n").partition("\t")
                if count.split("\t")[0].isdigit() and int(count.split("\t")[0]) >= 10:
                    widespread.add(word.lower())
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    with open("/usr/share/dict/words", encoding="utf-8", errors="replace") as f:
        known = {w.strip().lower() for w in f}
    out = set()
    for path in sys.argv[1:]:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            for line in f:
                cols = line.rstrip("\n").split("\t")
                if len(cols) < 3 or cols[0] not in KINDS:
                    continue
                kind, value = cols[0], cols[2].strip()
                if len(set(value.lower()) - set(" .-_")) <= 2:
                    continue  # "iiiiiiii", "aaaa": noise read from binary data
                if kind == "email":
                    value = value.split("@")[0]
                # "DOMAIN\user (HOST)" and similar: each piece is its own name
                for piece in re.split(r"[\\()/;|]+", value):
                    piece = piece.strip(" .,:-_\"'")
                    if not 3 <= len(piece) <= 60 or is_generic(piece) or not piece.isprintable():
                        continue
                    try:
                        piece.encode("ascii")
                    except UnicodeEncodeError:
                        continue
                    words = [w.lower() for w in WORD.findall(piece)]
                    if not words or all(w.isdigit() for w in words):
                        continue
                    if len(words) == 1:
                        w = words[0]
                        if w in known or w in STOP or w in widespread or re.fullmatch(r"[0-9a-f]{8,}", w):
                            continue
                        if re.fullmatch(r"[a-z]+_?ids?|[a-z]+params?|[a-z]+_?(?:name|type|value|count|index)", w):
                            continue  # a field name read as a value
                        if len(w) >= 6 or (len(w) >= 5 and any(c.isdigit() for c in w)):
                            out.add((kind, piece))
                    elif len(words) <= 5 and sum(map(len, words)) >= 8:
                        if all(w in known or w in STOP or len(w) < 2 for w in words):
                            continue  # only everyday words: too generic to replace everywhere
                        out.add((kind, piece))
    for kind, value in sorted(out):
        print("%s\t%s" % (kind, value))
    # every word of every value found (also the ones too generic to search for): the run
    # reserves a distinct pseudonym for each, so different names can never share one
    seen = set()
    for path in sys.argv[1:]:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            for line in f:
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 3 and cols[0] != "tok":
                    seen.update(w.lower() for w in WORD.findall(cols[2]) if len(w) >= 2 and w.isascii())
    for w in sorted(seen):
        print("seen\t%s" % w)


if __name__ == "__main__":
    main()
