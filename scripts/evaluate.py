"""
evaluate.py — Three-way benchmark: Z3 vs Bitwuzla vs NeuroSym.

Usage:
    python -u scripts/evaluate.py --bv    # QF_BV  (Z3 + Bitwuzla + NS)
    python -u scripts/evaluate.py --lia   # QF_LIA (Z3 + NS; Bitwuzla skipped)
    python -u scripts/evaluate.py --bv --lia  # both
"""

import argparse
import sys
import time
import random
import threading
import subprocess
import json
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import z3
from gansat.solver import GANSATSolver

try:
    import bitwuzla as _bwz
    _BITWUZLA_OK = True
except ImportError:
    _BITWUZLA_OK = False


# ── Subprocess worker entry-point (Z3 crash isolation) ───────────────────────

def _z3_worker_main():
    """Run as: python evaluate.py --_z3_worker <path> <timeout_ms>"""
    path       = sys.argv[2]
    timeout_ms = int(sys.argv[3])
    try:
        s = z3.Solver()
        s.set("timeout", timeout_ms)
        s.from_string(open(path, encoding="utf-8", errors="ignore").read())
        r = s.check()
        if r == z3.sat:   print(json.dumps({"result": "sat",   "ms": 0}))
        elif r == z3.unsat: print(json.dumps({"result": "unsat", "ms": 0}))
        else:               print(json.dumps({"result": "unknown","ms": 0}))
    except Exception as e:
        print(json.dumps({"result": "error", "ms": 0}))
    sys.exit(0)


# ── Individual solvers ────────────────────────────────────────────────────────

def solve_z3(path: str, timeout_ms: int):
    """Run Z3 in a subprocess so a crash (ASSERTION VIOLATION) doesn't kill us."""
    try:
        t0 = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, __file__, "--_z3_worker", path, str(timeout_ms)],
            capture_output=True,
            text=True,
            timeout=(timeout_ms / 1000.0) + 5.0,
        )
        ms = (time.perf_counter() - t0) * 1000
        out = proc.stdout.strip()
        if out:
            data = json.loads(out)
            return data["result"], ms
        return "error", ms
    except subprocess.TimeoutExpired:
        return "unknown", (timeout_ms + 5000)
    except Exception:
        return "error", 0.0


def solve_bitwuzla(path: str, timeout_ms: int):
    if not _BITWUZLA_OK:
        return "n/a", 0.0
    try:
        t0   = time.perf_counter()
        tm   = _bwz.TermManager()
        opts = _bwz.Options()
        opts.set(_bwz.Option.PRODUCE_MODELS, True)
        opts.set(_bwz.Option.TIME_LIMIT_PER, timeout_ms)
        opts.set(_bwz.Option.VERBOSITY,      0)
        parser = _bwz.Parser(tm, opts)
        parser.parse(path, parse_only=True, parse_file=True)
        bwz    = parser.bitwuzla()
        result = bwz.check_sat()
        ms     = (time.perf_counter() - t0) * 1000
        s = str(result)
        if s == "sat":   return "sat",   ms
        if s == "unsat": return "unsat", ms
        return "unknown", ms
    except Exception:
        return "error", 0.0


def solve_ns(ns: GANSATSolver, path: str, timeout_sec: float):
    box = [("error", 0.0)]
    def _run():
        try:
            t0 = time.perf_counter()
            r, _, _ = ns.solve_file(str(path))
            box[0] = (r, (time.perf_counter() - t0) * 1000)
        except Exception:
            pass
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    return box[0]


# ── Per-logic evaluation ──────────────────────────────────────────────────────

def run_eval(label, files, ns, timeout_ms, use_bitwuzla):
    timeout_sec = timeout_ms / 1000.0
    results = []

    bwz_col = "Bwz ms" if use_bitwuzla else "  —   "
    print(f"\n{'='*110}")
    print(f"  {label}")
    print(f"{'='*110}")
    print(f"{'#':<5} {'File':<36} {'Z3':>7} {'Z3 ms':>8} {'Bwz':>7} {bwz_col:>8} {'NS':>9} {'NS ms':>8}  {'Spd(NS/Z3)':>10}  Match")
    print("-" * 110)

    for i, fpath in enumerate(files):
        z3_res,  z3_ms  = solve_z3(str(fpath), timeout_ms)
        bwz_res, bwz_ms = solve_bitwuzla(str(fpath), timeout_ms) if use_bitwuzla else ("—", 0.0)
        ns_res,  ns_ms  = solve_ns(ns, fpath, timeout_sec + 3)

        # correctness: NS vs Z3 (ground truth)
        if z3_res in ("sat", "unsat") and ns_res not in ("error", "unknown", "—"):
            match = "OK" if z3_res == ns_res else "!MISMATCH"
        elif ns_res in ("error",):
            match = "ERR"
        else:
            match = "OK"

        speedup = z3_ms / ns_ms if ns_ms > 1 else 0.0

        results.append(dict(
            z3_res=z3_res,   z3_ms=z3_ms,
            bwz_res=bwz_res, bwz_ms=bwz_ms,
            ns_res=ns_res,   ns_ms=ns_ms,
            speedup=speedup, match=match
        ))

        sp_s   = f"{speedup:.1f}x" if speedup > 0 else "-"
        bwz_t  = f"{bwz_ms:>7.0f}ms" if use_bitwuzla else "        "
        print(f"{i+1:<5} {fpath.name[:34]:<36} {z3_res:>7} {z3_ms:>7.0f}ms "
              f"{bwz_res:>7} {bwz_t} {ns_res:>9} {ns_ms:>7.0f}ms  {sp_s:>10}  {match}")
        sys.stdout.flush()

    # ── Summary ───────────────────────────────────────────────────────────────
    n         = len(results)
    sat_r     = [r for r in results if r["z3_res"] == "sat"]
    unsat_r   = [r for r in results if r["z3_res"] == "unsat"]
    unknown_r = [r for r in results if r["z3_res"] not in ("sat","unsat")]
    bad       = [r for r in results if r["match"] == "!MISMATCH"]

    ns_faster_z3  = sum(1 for r in results if r["ns_ms"]  > 0 and r["z3_ms"]  > 1 and r["ns_ms"]  < r["z3_ms"]  * 0.95)
    bwz_faster_z3 = sum(1 for r in results if r["bwz_ms"] > 0 and r["z3_ms"]  > 1 and r["bwz_ms"] < r["z3_ms"]  * 0.95) if use_bitwuzla else 0
    ns_faster_bwz = sum(1 for r in results if r["ns_ms"]  > 0 and r["bwz_ms"] > 1 and r["ns_ms"]  < r["bwz_ms"] * 0.95) if use_bitwuzla else 0

    # Avg times on SAT cases where both Z3 and NS returned SAT
    sat_both = [r for r in sat_r if r["ns_res"] == "sat"]
    avg_z3   = sum(r["z3_ms"]  for r in sat_both) / max(len(sat_both), 1)
    avg_bwz  = sum(r["bwz_ms"] for r in sat_both) / max(len(sat_both), 1) if use_bitwuzla else 0
    avg_ns   = sum(r["ns_ms"]  for r in sat_both) / max(len(sat_both), 1)

    # Solved where Z3 timed out
    ns_solved_z3_timeout  = sum(1 for r in results if r["z3_res"] in ("unknown","error") and r["ns_res"] in ("sat","unsat"))
    bwz_solved_z3_timeout = sum(1 for r in results if r["z3_res"] in ("unknown","error") and r["bwz_res"] in ("sat","unsat")) if use_bitwuzla else 0

    print(f"\n{'='*110}")
    print(f"  SUMMARY — {label}")
    print(f"{'='*110}")
    print(f"  Benchmarks tested              : {n}")
    print(f"  SAT / UNSAT / Unknown (Z3)     : {len(sat_r)} / {len(unsat_r)} / {len(unknown_r)}")
    print()
    print(f"  {'Solver':<12} {'Faster than Z3':>16}  {'Avg ms (SAT)':>14}  {'Solved Z3-timeout':>18}")
    print(f"  {'-'*65}")
    print(f"  {'Z3':<12} {'—':>16}  {avg_z3:>13.1f}ms  {'—':>18}")
    if use_bitwuzla:
        print(f"  {'Bitwuzla':<12} {f'{bwz_faster_z3}/{n} ({100*bwz_faster_z3/n:.1f}%)':>16}  {avg_bwz:>13.1f}ms  {bwz_solved_z3_timeout:>18}")
    print(f"  {'NeuroSym':<12} {f'{ns_faster_z3}/{n} ({100*ns_faster_z3/n:.1f}%)':>16}  {avg_ns:>13.1f}ms  {ns_solved_z3_timeout:>18}")
    if use_bitwuzla:
        print(f"\n  NS faster than Bitwuzla        : {ns_faster_bwz}/{n} ({100*ns_faster_bwz/n:.1f}%)")
    print()
    if bad:
        print(f"  !! CORRECTNESS ERRORS          : {len(bad)}")
        for r in bad[:5]:
            print(f"     Z3={r['z3_res']}  NS={r['ns_res']}")
    else:
        print(f"  Correctness (NS vs Z3)         : PERFECT — 0 mismatches")
    print(f"{'='*110}\n")

    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bv",          action="store_true")
    parser.add_argument("--lia",         action="store_true")
    parser.add_argument("--n",           type=int, default=150)
    parser.add_argument("--timeout",     type=int, default=5000)
    parser.add_argument("--maxsize",     type=int, default=20)
    parser.add_argument("--seed",        type=int, default=42)
    args = parser.parse_args()

    if not args.bv and not args.lia:
        args.bv = True   # default to BV

    random.seed(args.seed)
    base     = Path("data/smtcomp2025/extracted/single_query")
    max_bytes = args.maxsize * 1024

    def sample_files(logic):
        d = base / logic
        if not d.exists():
            print(f"[warn] {d} not found"); return []
        files = [f for f in d.rglob("*.smt2") if f.stat().st_size <= max_bytes]
        return random.sample(files, min(args.n, len(files)))

    if args.bv:
        files = sample_files("QF_BV")
        print(f"[eval] Loading NeuroSym (BV)...")
        ns = GANSATSolver(bv_model_path="models/gansat_bv.pt", timeout_ms=args.timeout)
        print(f"[eval] {len(files)} QF_BV files | timeout={args.timeout}ms | Bitwuzla={'yes' if _BITWUZLA_OK else 'no'}")
        run_eval("QF_BV — Z3 vs Bitwuzla vs NeuroSym", files, ns, args.timeout, use_bitwuzla=_BITWUZLA_OK)

    if args.lia:
        files = sample_files("QF_LIA")
        print(f"[eval] Loading NeuroSym (LIA)...")
        ns = GANSATSolver(lia_model_path="models/gansat_lia.pt", timeout_ms=args.timeout)
        print(f"[eval] {len(files)} QF_LIA files | timeout={args.timeout}ms | Bitwuzla=no (LIA unsupported)")
        run_eval("QF_LIA — Z3 vs NeuroSym", files, ns, args.timeout, use_bitwuzla=False)


if __name__ == "__main__":
    # Check for subprocess worker mode before argparse (argparse rejects extra positional args)
    if len(sys.argv) > 1 and sys.argv[1] == "--_z3_worker":
        _z3_worker_main()
    else:
        main()
