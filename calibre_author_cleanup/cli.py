#!/usr/bin/env python3
"""
Author Cleanup for Calibre — scan, cluster, review, apply.

Designed for very large libraries (100k+ books) managed with calibredb.
Runs on the NAS (or anywhere calibredb can reach the library) in three steps:

  1. SCAN    -> pulls the full author list, normalizes and clusters it,
                writes author_review.csv for you to approve.
  2. (you)   -> open the CSV, set action = merge / skip / split per group,
                adjust the canonical name if you want a different winner.
  3. APPLY   -> dry-runs by default; add --commit to actually write merges
                back through calibredb, preserving co-authors and order.

Safety:
  * Never writes anything without --commit.
  * Decisions persist: anything you mark "skip" lands in never_merge.txt and
    is excluded from every future scan. Approved merges land in
    always_merge.txt and are auto-applied on future scans.
  * Back up metadata.db and stop Calibre-Web before running APPLY --commit.

Usage:
  python3 author_cleanup.py scan  --library /path/to/CalibreLibrary
  python3 author_cleanup.py apply --library /path/to/CalibreLibrary            (dry run)
  python3 author_cleanup.py apply --library /path/to/CalibreLibrary --commit

  # Or feed it a text file (one author per line) with no calibredb at all:
  python3 author_cleanup.py scan --authors-file authors.txt

Requires: pip install rapidfuzz
"""

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import defaultdict

try:
    from rapidfuzz import fuzz
except ImportError:
    sys.exit("Missing dependency: pip install rapidfuzz")

# ---------------------------------------------------------------- constants

REVIEW_CSV     = "author_review.csv"
NEVER_FILE     = "never_merge.txt"    # lines: Name A ||| Name B   (pair never merges)
ALWAYS_FILE    = "always_merge.txt"   # lines: Variant ||| Canonical
GLUE_PATTERNS  = [r"\s+and\s+", r"\s*;\s*", r"\s+with\s+", r"\s*/\s*"]

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "esq"}

HIGH = "high"      # identical normalized key — safe to merge
MED  = "medium"    # fuzzy score >= --threshold — review
LOW  = "low"       # fuzzy score in the gray zone — review carefully

# ---------------------------------------------------------------- normalize

def strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))

def norm_key(name: str) -> str:
    """Aggressive canonical key: casefold, de-accent, reorder 'Last, First',
    collapse initials, drop punctuation and suffixes."""
    s = strip_diacritics(name).casefold().strip()

    # "king, stephen" -> "stephen king"  (only one comma = reorder pattern)
    if s.count(",") == 1:
        last, first = [p.strip() for p in s.split(",")]
        if last and first:
            s = f"{first} {last}"

    # unify punctuation to spaces, then collapse
    s = re.sub(r"[.\-_'\u2019]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    tokens = [t for t in s.split(" ") if t]

    # collapse runs of single-letter initials: "j r r tolkien" -> "jrr tolkien"
    out, run = [], []
    for t in tokens:
        if len(t) == 1:
            run.append(t)
        else:
            if run:
                out.append("".join(run)); run = []
            out.append(t)
    if run:
        out.append("".join(run))
    return " ".join(out)

def initials_signature(name: str) -> str:
    """first-initials + full last token — used to keep fuzzy matching honest.
    'jrr tolkien' -> 'j|tolkien' ; 'john tolkien' -> 'j|tolkien'"""
    k = norm_key(name)
    toks = [t for t in k.split(" ") if t not in SUFFIXES]
    if not toks:
        return ""
    return f"{toks[0][:1]}|{toks[-1][:4]}"

def looks_glued(name: str) -> bool:
    return any(re.search(p, name, flags=re.IGNORECASE) for p in GLUE_PATTERNS)

def split_glued(name: str):
    parts = re.split("|".join(GLUE_PATTERNS), name, flags=re.IGNORECASE)
    return [p.strip(" .,&") for p in parts if p.strip(" .,&")]

# ---------------------------------------------------------------- calibredb

def cdb(args, library, capture=True):
    cmd = ["calibredb"] + args + ["--with-library", library]
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"calibredb failed: {' '.join(cmd)}\n{r.stderr}")
    return r.stdout if capture else ""

def fetch_authors(library):
    """Return {display_name: book_count} using calibredb list_categories."""
    out = cdb(["list_categories", "-c", "authors", "--csv"], library)
    counts = {}
    for row in csv.reader(out.splitlines()):
        if len(row) >= 3 and row[0] == "authors":
            try:
                counts[row[1]] = int(row[2])
            except ValueError:
                continue
    if not counts:
        raise RuntimeError("No authors returned — check the --library path.")
    return counts

def books_for_author(library, author):
    out = cdb(["list", "--search", f'authors:"={author}"',
               "--fields", "id,authors", "--for-machine"], library)
    return json.loads(out)

# ---------------------------------------------------------------- pair files

def load_pairs(path):
    pairs = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and "|||" in line and not line.startswith("#"):
                    a, b = [p.strip() for p in line.split("|||", 1)]
                    pairs.add((a, b))
    return pairs

def append_pairs(path, pairs, header):
    new = pairs - load_pairs(path)
    if not new:
        return
    exists = os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if not exists:
            f.write(f"# {header}\n")
        for a, b in sorted(new):
            f.write(f"{a} ||| {b}\n")

def never_key(a, b):
    return tuple(sorted((a, b)))

# ---------------------------------------------------------------- clustering

def pick_canonical(variants, counts):
    """Winner = most books; tie-break: has diacritics/full words > initials,
    then longest, then alphabetical (stable)."""
    def score(v):
        richness = sum(1 for c in v if ord(c) > 127)
        words = len(norm_key(v).split(" "))
        no_comma = "," not in v
        clean = "  " not in v
        dotted = min(v.count("."), 4)
        caps = sum(1 for t in v.split() if t[:1].isupper())
        return (counts.get(v, 0), no_comma, clean, words,
                caps, richness, dotted, len(v), v)
    return sorted(variants, key=score, reverse=True)[0]

def cluster(counts, threshold, gray, never_pairs):
    names = list(counts)

    # Pass 1 — exact normalized key
    by_key = defaultdict(list)
    for n in names:
        by_key[norm_key(n)].append(n)

    groups = []          # (confidence, [variants])
    reps = {}            # norm_key -> representative for pass 2
    for k, vs in by_key.items():
        if len(vs) > 1:
            groups.append((HIGH, vs))
        reps[k] = pick_canonical(vs, counts)

    # Pass 2 — fuzzy within blocking buckets (same initials signature)
    buckets = defaultdict(list)
    for k, rep in reps.items():
        buckets[initials_signature(rep)].append(k)

    merged_keys = {}     # key -> group index in fuzzy_groups
    fuzzy_groups = []    # list of set(norm_keys)
    for sig, keys in buckets.items():
        if len(keys) < 2:
            continue
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = keys[i], keys[j]
                s = fuzz.token_sort_ratio(a, b)
                if s < gray:
                    continue
                if never_key(reps[a], reps[b]) in never_pairs:
                    continue
                conf = MED if s >= threshold else LOW
                gi, gj = merged_keys.get(a), merged_keys.get(b)
                if gi is None and gj is None:
                    fuzzy_groups.append({"keys": {a, b}, "conf": conf})
                    merged_keys[a] = merged_keys[b] = len(fuzzy_groups) - 1
                elif gi is not None and gj is None:
                    fuzzy_groups[gi]["keys"].add(b); merged_keys[b] = gi
                    if conf == LOW: fuzzy_groups[gi]["conf"] = LOW
                elif gj is not None and gi is None:
                    fuzzy_groups[gj]["keys"].add(a); merged_keys[a] = gj
                    if conf == LOW: fuzzy_groups[gj]["conf"] = LOW

    for g in fuzzy_groups:
        variants = [v for k in g["keys"] for v in by_key[k]]
        groups.append((g["conf"], variants))

    return groups, by_key

# ---------------------------------------------------------------- scan

def do_scan(args):
    never_pairs = {never_key(a, b) for a, b in load_pairs(NEVER_FILE)}
    always = dict(load_pairs(ALWAYS_FILE))

    if args.authors_file:
        counts = {}
        with open(args.authors_file, encoding="utf-8") as f:
            for line in f:
                n = line.strip()
                if n:
                    counts[n] = counts.get(n, 0) + 1
        print(f"Loaded {len(counts)} authors from {args.authors_file}")
    else:
        print("Pulling author list from calibredb ...")
        counts = fetch_authors(args.library)
        print(f"Found {len(counts)} distinct authors.")

    groups, _ = cluster(counts, args.threshold, args.gray, never_pairs)

    glued = [n for n in counts if looks_glued(n)]

    rows, gid = [], 0
    for conf, variants in sorted(groups, key=lambda g: -sum(counts.get(v, 0)
                                                            for v in g[1])):
        gid += 1
        canon = pick_canonical(variants, counts)
        # pre-approve pairs already in always_merge.txt
        pre = all(always.get(v, v) == canon or v == canon for v in variants)
        for v in sorted(variants, key=lambda x: -counts.get(x, 0)):
            rows.append({
                "group": gid, "confidence": conf,
                "action": "merge" if (conf == HIGH or pre) else "",
                "canonical": canon, "variant": v,
                "books": counts.get(v, 0),
            })
    for n in sorted(glued, key=lambda x: -counts.get(x, 0)):
        gid += 1
        rows.append({"group": gid, "confidence": "glued",
                     "action": "", "canonical": " & ".join(split_glued(n)),
                     "variant": n, "books": counts.get(n, 0)})

    with open(REVIEW_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["group", "confidence", "action",
                                          "canonical", "variant", "books"])
        w.writeheader()
        w.writerows(rows)

    n_groups = gid
    print(f"\nWrote {REVIEW_CSV}: {n_groups} groups, {len(rows)} rows.")
    print("  high   = same name after normalization (pre-marked 'merge')")
    print("  medium = strong fuzzy match — skim and approve")
    print("  low    = gray zone — check before merging")
    print("  glued  = one string holding multiple authors — action 'split'")
    print("\nEdit the action column: merge / skip / split (or leave blank to ignore).")
    print("Change 'canonical' on any row of a group to pick a different winner.")

# ---------------------------------------------------------------- apply

def author_sort_for(name):
    if "," in name or " " not in name:
        return name
    parts = name.rsplit(" ", 1)
    if parts[1].lower().strip(".") in SUFFIXES and " " in parts[0]:
        head, last = parts[0].rsplit(" ", 1)
        return f"{last} {parts[1]}, {head}"
    return f"{parts[1]}, {parts[0]}"

def do_apply(args):
    if not os.path.exists(REVIEW_CSV):
        sys.exit(f"{REVIEW_CSV} not found — run scan first.")

    merges, splits, skips = {}, {}, set()
    group_canon = {}
    with open(REVIEW_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            act = row["action"].strip().lower()
            g = row["group"]
            if row["canonical"].strip():
                group_canon.setdefault(g, row["canonical"].strip())
            if act == "merge":
                merges[row["variant"]] = g
            elif act == "split":
                splits[row["variant"]] = row["canonical"].strip()
            elif act == "skip":
                skips.add((g, row["variant"]))

    plan = {v: group_canon[g] for v, g in merges.items()
            if group_canon.get(g) and v != group_canon[g]}
    plan.update(splits)

    if not plan:
        sys.exit("Nothing marked 'merge' or 'split' in the CSV.")

    print(f"{len(plan)} author rewrites planned"
          f" ({'COMMIT' if args.commit else 'DRY RUN — add --commit to write'}).\n")

    # persist decisions
    skip_pairs = set()
    for g, v in skips:
        c = group_canon.get(g)
        if c and c != v:
            skip_pairs.add(never_key(v, c))
    append_pairs(NEVER_FILE, skip_pairs, "pairs you rejected — never suggested again")
    append_pairs(ALWAYS_FILE, {(v, c) for v, c in plan.items() if v != c},
                 "approved merges — auto-approved on future scans")

    changed_books = 0
    for old, new in plan.items():
        try:
            books = books_for_author(args.library, old)
        except RuntimeError as e:
            print(f"  !! lookup failed for {old!r}: {e}")
            continue
        print(f"{old!r}  ->  {new!r}   ({len(books)} books)")
        for b in books:
            authors = b["authors"] if isinstance(b["authors"], list) \
                      else [a.strip() for a in str(b["authors"]).split("&")]
            new_list = []
            for a in authors:
                if a == old:
                    new_list.extend([p.strip() for p in new.split("&")])
                else:
                    new_list.append(a)
            # dedupe, keep order
            seen, final = set(), []
            for a in new_list:
                if a not in seen:
                    seen.add(a); final.append(a)
            field = " & ".join(final)
            if args.commit:
                cdb(["set_metadata", str(b["id"]), "-f", f"authors:{field}"],
                    args.library, capture=True)
                if len(final) == 1:
                    cdb(["set_metadata", str(b["id"]), "-f",
                         f"author_sort:{author_sort_for(final[0])}"],
                        args.library, capture=True)
            else:
                print(f"    book {b['id']}: authors -> {field}")
            changed_books += 1

    verb = "updated" if args.commit else "would update"
    print(f"\nDone: {verb} {changed_books} books across {len(plan)} authors.")
    if args.commit:
        print("Run Library -> Check Library in Calibre afterwards, "
              "and restart Calibre-Web when finished.")

# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("scan", help="cluster authors, write review CSV")
    ps.add_argument("--library", help="path to Calibre library root")
    ps.add_argument("--authors-file", help="plain text list instead of calibredb")
    ps.add_argument("--threshold", type=int, default=93,
                    help="fuzzy score for 'medium' confidence (default 93)")
    ps.add_argument("--gray", type=int, default=86,
                    help="lower bound for 'low' confidence rows (default 86)")

    pa = sub.add_parser("apply", help="apply approved CSV (dry run by default)")
    pa.add_argument("--library", required=True)
    pa.add_argument("--commit", action="store_true",
                    help="actually write changes (default is dry run)")

    args = p.parse_args()
    if args.cmd == "scan":
        if not args.library and not args.authors_file:
            p.error("scan needs --library or --authors-file")
        do_scan(args)
    else:
        do_apply(args)

if __name__ == "__main__":
    main()
