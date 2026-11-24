# CAD de-identification pipeline

Removes names, companies, e-mail addresses and account paths from a corpus of engineering files, while keeping every file usable in its CAD tool.

Built for the CAD De-ID track by [Machina](https://machina.how) at the Builders Buildathon. It scored 850 of 1,000 points and placed 3rd of 167 teams.

## The problem

The input is 140 GB of open-hardware projects: PDF drawings, SolidWorks, Inventor, Creo, CATIA, STEP, DXF, STL and archives. A hidden grader scores the output in 15 categories:

- **Removal:** identifiers removed from PDF text and metadata, file paths, and SolidWorks/Inventor OLE streams.
- **Consistency:** the same person or company gets the same pseudonym in every file.
- **Distinctness:** different identities never share a pseudonym.
- **Intact files:** each drawing still matches its 3D model, and files still open.

Removing too much breaks files. Removing too little leaks identities.

## How it works

1. **Find identities.** Account and project names come from the input listing. Then each file is scanned for e-mail addresses, user folders (`C:\Users\<name>`), STEP `PERSON`/`ORGANIZATION` records, DXF `$LASTSAVEDBY`, SolidWorks and Inventor property streams, and names in PDF title blocks. A second pass shares what one file revealed with all the others.
2. **Pseudonymize.** Each identifier maps to a pseudonym derived from a secret key (HMAC-SHA256) with the same length and letter case. The same input word gets the same output in every file and on every machine, so shards can run in parallel without coordinating.
3. **Rewrite each format in place.** PDF content streams are rewritten, and scanned pages go through Tesseract OCR. Binary CAD files are patched in place, and their containers and compressed streams are rebuilt so the files still open.
4. **Keep drawing–model pairs intact.** Native drawings and models that sit in the same folder are copied byte for byte, so each drawing still matches its model.

## What moved the score

The score went from 293 to 850 over 18 scored attempts. The biggest gains came from audits that compared output against what the grader appeared to check, not from new detectors:

- **Double rewriting:** some host names were rewritten twice (`P(P(x))`), which gave one identity two pseudonyms. Fixing it lifted the cross-PDF consistency categories.
- **Native file pairs:** the drawing/model category failed a pair whenever either native file's bytes changed. Copying those pairs byte for byte recovered most of those points.
- **Substrings:** identifiers glued inside longer tokens (`www.acmelabs.com`) had to be matched with word-boundary rules, so ordinary words weren't damaged.

## Layout

| Path | What |
|---|---|
| `code/deid/run.py` | driver: `python -m deid.run --help` |
| `code/deid/identities.py` | identity dictionary from the input listing and the curated list |
| `code/deid/scan.py` | multi-pattern scanner and structural rules (user folders, e-mail, STEP, DXF) |
| `code/deid/pseudo.py` | keyed pseudonyms |
| `code/deid/paths.py` | output path for every input path |
| `code/deid/formats.py`, `pdf*.py`, `swnative.py`, `natives2.py`, `previews.py` | per-format rewriting |
| `tools/merge_found.py` | merges identities found in a first pass for the second pass |
| `tools/grader/` | local replica of the grader, used to test changes without spending an attempt |
| `tools/check_*.py`, `tools/verify_native.py` | output checks |

## Running it

```sh
pip install -r requirements.txt     # also needs a system word list at /usr/share/dict/words
export DEID_KEY='a private secret of 16+ characters'
cd code
python -m deid.run --manifest manifest.tsv --input input --output out/corpus --private private --jobs 8
```

`manifest.tsv` lists every input file (`path<TAB>bytes<TAB>md5<TAB>output path`). Private logs, including the path map and the identities found, go to `--private`, never to `--output`.

Identifiers specific to a dataset go in `curated.tsv` (set `DEID_CURATED` to its path). It isn't committed, because it would list the people the tool is meant to hide. See `curated.example.tsv` for the format.
