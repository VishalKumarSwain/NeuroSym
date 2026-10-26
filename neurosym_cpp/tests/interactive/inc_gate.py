#!/usr/bin/env python3
"""Correctness gate for NeuroSym interactive mode on official SMT-COMP
incremental QF_BV files: every check-sat answer is compared with the file's
official status, Z3 and Bitwuzla.

usage: inc_gate.py OUTDIR TIMEOUT FILE...
Writes OUTDIR/checks.csv (one row per expected check and solver) and
OUTDIR/summary.txt; raw stdout/stderr per run in OUTDIR/raw/.
"""
import csv, os, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

NS = os.path.expanduser("~/neurosym_pushpop_dev/new/bitblast_solver")
SOLVERS = {
    "neurosym": lambda f: ["bash", "-c", f"ulimit -v 16777216; exec {NS} --interactive"],
    "z3": lambda f: ["bash", "-c", "ulimit -v 16777216; exec z3 -in"],
    "bitwuzla": lambda f: ["docker", "run", "--rm", "-i", "--cpus=1", "-m", "16g",
                           "--name", "incgate_" + str(os.getpid()) + "_" + re.sub(r"\W", "_", os.path.basename(f)),
                           "bitwuzla:0.9.0", "bitwuzla", "--lang", "smt2"],
}
ANS = re.compile(r"^(sat|unsat|unknown)$")


def expected_statuses(path):
    """Official status per check-sat, in order (None if not given)."""
    out, pending = [], None
    with open(path, errors="replace") as fh:
        for line in fh:
            m = re.search(r"\(set-info\s+:status\s+(sat|unsat|unknown)\)", line)
            if m:
                pending = m.group(1)
            n = len(re.findall(r"\(check-sat(?:-assuming)?[\s)]", line))
            for _ in range(n):
                out.append(pending)
                pending = None
    return out


def run(solver, path, timeout, rawdir):
    base = os.path.basename(path)
    with open(path, "rb") as fh:
        data = fh.read()
    p = subprocess.Popen(SOLVERS[solver](path), stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    status = "done"
    try:
        out, err = p.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired:
        status = "timeout"
        p.kill()
        if solver == "bitwuzla":
            subprocess.run(["docker", "kill", "incgate_" + str(os.getpid()) + "_" +
                            re.sub(r"\W", "_", base)], capture_output=True)
        out, err = p.communicate()
    with open(os.path.join(rawdir, f"{base}.{solver}.out"), "wb") as fh:
        fh.write(out)
    with open(os.path.join(rawdir, f"{base}.{solver}.err"), "wb") as fh:
        fh.write(err)
    text = out.decode(errors="replace").splitlines()
    answers = [l.strip() for l in text if ANS.match(l.strip())]
    errors = [l for l in text if l.startswith("(error")]
    return status, p.returncode, answers, errors


def main():
    outdir, timeout, files = sys.argv[1], float(sys.argv[2]), sys.argv[3:]
    rawdir = os.path.join(outdir, "raw")
    os.makedirs(rawdir, exist_ok=True)
    jobs = [(s, f) for f in files for s in SOLVERS]
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = dict(zip(jobs, ex.map(lambda j: run(j[0], j[1], timeout, rawdir), jobs)))
    rows, wrong, unresolved = [], [], []
    per_file = []
    for f in files:
        exp = expected_statuses(f)
        base = os.path.basename(f)
        res = {s: results[(s, f)] for s in SOLVERS}
        ns_ans = res["neurosym"][2]
        for i, e in enumerate(exp):
            refs = {"official": e}
            for s in ("z3", "bitwuzla"):
                a = res[s][2]
                refs[s] = a[i] if i < len(a) else ("TIMEOUT" if res[s][0] == "timeout" else "NOT_REACHED")
            a = ns_ans[i] if i < len(ns_ans) else ("TIMEOUT" if res["neurosym"][0] == "timeout" else "NOT_REACHED")
            concl = {k: v for k, v in refs.items() if v in ("sat", "unsat")}
            if len(set(concl.values())) > 1:
                unresolved.append((base, i, refs))
            if a in ("sat", "unsat") and any(v != a for v in concl.values()):
                wrong.append((base, i, a, refs))
            rows.append([base, i, e, refs["z3"], refs["bitwuzla"], a])
        nsat = sum(1 for i in range(len(exp)) if i < len(ns_ans) and ns_ans[i] in ("sat", "unsat"))
        per_file.append((base, len(exp), nsat, res["neurosym"][0], res["neurosym"][1],
                         len(res["neurosym"][3]), sum(1 for x in res["z3"][2] if x in ("sat", "unsat")),
                         sum(1 for x in res["bitwuzla"][2] if x in ("sat", "unsat"))))
    with open(os.path.join(outdir, "checks.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "check", "official", "z3", "bitwuzla", "neurosym"])
        w.writerows(rows)
    with open(os.path.join(outdir, "summary.txt"), "w") as fh:
        fh.write(f"timeout per run: {timeout}s\n")
        fh.write("file | expected checks | neurosym conclusive | ns status | ns rc | ns errors | z3 conclusive | bitwuzla conclusive\n")
        for r in per_file:
            fh.write(" | ".join(map(str, r)) + "\n")
        tot = sum(r[1] for r in per_file)
        fh.write(f"TOTAL expected checks: {tot}; neurosym conclusive: {sum(r[2] for r in per_file)}\n")
        fh.write(f"WRONG neurosym answers: {len(wrong)}\n")
        for x in wrong:
            fh.write(f"  WRONG {x}\n")
        fh.write(f"reference disagreements (unresolved): {len(unresolved)}\n")
        for x in unresolved[:50]:
            fh.write(f"  UNRESOLVED {x}\n")
    print(open(os.path.join(outdir, "summary.txt")).read())


if __name__ == "__main__":
    main()
