#!/usr/bin/env python3
"""Differential push/pop sequences built ONLY from assertions of existing
official SMT-COMP QF_BV files (no invented constraints).

For each source file: all non-assert commands (set-logic, declarations,
define-fun) are kept at the root in their original order, so every symbol
is root-visible and every define-fun keeps its original binding; then a
seeded random sequence of push / pop / assert (an original assertion, verbatim)
/ check-sat / check-sat-assuming (declared Bool constants only) follows.

usage: gen_diff.py OUTDIR SEED SEQS_PER_FILE FILE...
Writes OUTDIR/<file>__s<seed>_<k>.smt2 and OUTDIR/manifest.csv.
"""
import csv, hashlib, os, random, sys


def split_commands(text):
    """Top-level S-expressions, respecting comments, strings and |symbols|."""
    cmds, depth, start, i, n = [], 0, None, 0, len(text)
    while i < n:
        c = text[i]
        if c == ';' and depth >= 0:
            while i < n and text[i] != '\n':
                i += 1
            continue
        if c == '"':
            i += 1
            while i < n:
                if text[i] == '"':
                    if i + 1 < n and text[i + 1] == '"':
                        i += 2
                        continue
                    break
                i += 1
        elif c == '|':
            i += 1
            while i < n and text[i] != '|':
                i += 1
        elif c == '(':
            if depth == 0:
                start = i
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                cmds.append(text[start:i + 1])
        i += 1
    return cmds


def head(cmd):
    return cmd[1:].split(None, 1)[0].rstrip(')') if len(cmd) > 1 else ""


def bool_consts(cmds):
    out = []
    for c in cmds:
        h = head(c)
        toks = c.replace('(', ' ( ').replace(')', ' ) ').split()
        if h == "declare-fun" and toks[3:6] == ['(', ')', 'Bool']:
            out.append(toks[2])
        elif h == "declare-const" and len(toks) >= 4 and toks[3] == 'Bool':
            out.append(toks[2])
    return out


def generate(cmds, rng, length=60, maxdepth=4):
    prefix, asserts = [], []
    for c in cmds:
        h = head(c)
        if h == "assert":
            asserts.append(c)
        elif h in ("set-logic", "declare-fun", "declare-const", "define-fun", "define-sort"):
            prefix.append(c)
        # set-info/check-sat/exit/get-* of the source are dropped
    if not asserts:
        return None
    bools = bool_consts(prefix)
    seq, depth = list(prefix), 0
    for _ in range(length):
        r = rng.random()
        if r < 0.15 and depth < maxdepth:
            seq.append("(push 1)"); depth += 1
        elif r < 0.27 and depth > 0:
            seq.append("(pop 1)"); depth -= 1
            if rng.random() < 0.7:
                seq.append("(check-sat)")          # check right after a pop
        elif r < 0.62:
            seq.append(rng.choice(asserts))
        elif r < 0.70:
            k = rng.randint(1, min(8, len(asserts)))
            seq.extend(rng.sample(asserts, k))     # burst: more UNSAT
        elif r < 0.78 and bools:
            lits = []
            for b in rng.sample(bools, min(len(bools), rng.randint(1, 3))):
                lits.append(b if rng.random() < 0.5 else f"(not {b})")
            if rng.random() < 0.2:
                b = rng.choice(bools)
                lits += [b, f"(not {b})"]           # contradictory for one check only
            seq.append("(check-sat-assuming (" + " ".join(lits) + "))")
            seq.append("(check-sat)")              # next plain check must ignore them
        else:
            seq.append("(check-sat)")
            if rng.random() < 0.2:
                seq.append("(check-sat)")          # repeated check without mutation
    while depth > 0:
        seq.append("(pop 1)"); depth -= 1
        seq.append("(check-sat)")
    seq.append("(exit)")
    return "\n".join(seq) + "\n"


def main():
    outdir, seed, per = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    os.makedirs(outdir, exist_ok=True)
    rows = []
    for path in sys.argv[4:]:
        data = open(path, errors="replace").read()
        sha = hashlib.sha256(data.encode()).hexdigest()
        cmds = split_commands(data)
        for k in range(per):
            rng = random.Random(f"{seed}:{sha}:{k}")
            text = generate(cmds, rng)
            if text is None:
                continue
            name = f"{os.path.basename(path)}__s{seed}_{k}.smt2"
            with open(os.path.join(outdir, name), "w") as fh:
                fh.write("(set-info :source |differential push/pop sequence from " +
                         os.path.basename(path) + "|)\n" + text)
            rows.append([name, path, sha, seed, k, text.count("(check-sat")])
    with open(os.path.join(outdir, "manifest.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["sequence", "source", "source_sha256", "seed", "index", "checks"])
        w.writerows(rows)
    print(len(rows), "sequences")


if __name__ == "__main__":
    main()
