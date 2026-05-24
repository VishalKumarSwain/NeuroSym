"""
evaluate.py — Head-to-head: NeuroSym vs Z3 on SMT-COMP 2025 benchmarks.

Usage:
    python -u scripts/evaluate.py --bv        # QF_BV evaluation
    python -u scripts/evaluate.py --lia       # QF_LIA evaluation
    python -u scripts/evaluate.py --synthetic # on our synthetic data only
"""

import argparse
import sys
import time
import random
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import z3
from gansat.solver import GANSATSolver


def solve_z3_only(formula_path: str, timeout_ms: int = 5000):
    try:
        t0 = time.perf_counter()
        solver = z3.Solver()
        solver.set("timeout", timeout_ms)
        solver.from_string(open(formula_path, encoding="utf-8", errors="ignore").read())
        result = solver.check()
        ms = (time.perf_counter() - t0) * 1000
        if result == z3.sat:     return "sat",     ms
        elif result == z3.unsat: return "unsat",   ms
        else:                    return "unknown",  ms
    except Exception:
        return "error", 0.0


def solve_neurosym_timed(ns: GANSATSolver, formula_path: str, timeout_sec: float = 8.0):
    """Run NeuroSym in a thread with hard timeout."""
    result_box = [("error", 0.0)]

    def _run():
        try:
            t0 = time.perf_counter()
            res, _, _ = ns.solve_file(str(formula_path))
            result_box[0] = (res, (time.perf_counter() - t0) * 1000)
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    return result_box[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bv",        action="store_true")
    parser.add_argument("--lia",       action="store_true")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--n",         type=int, default=150)
    parser.add_argument("--timeout",   type=int, default=5000, help="ms per benchmark")
    parser.add_argument("--maxsize",   type=int, default=20,   help="max file size KB")
    parser.add_argument("--seed",      type=int, default=42)
    args = parser.parse_args()

    random.seed(args.seed)

    # Select data source
    base = Path("data")
    if args.synthetic:
        data_dirs = list(base.glob("bv_benchmarks/klee_style")) + \
                    list(base.glob("bv_benchmarks/mixed_arith")) + \
                    list(base.glob("bv_benchmarks/bitwise")) + \
                    list(base.glob("bv_benchmarks/signed"))
        model    = "models/gansat_bv.pt"
        label    = "Synthetic QF_BV"
    elif args.lia:
        data_dirs = [base / "smtcomp2025/extracted/single_query/QF_LIA"]
        model    = "models/gansat_lia.pt"
        label    = "SMT-COMP 2025 QF_LIA"
    else:  # default BV
        data_dirs = [base / "smtcomp2025/extracted/single_query/QF_BV"]
        model    = "models/gansat_bv.pt"
        label    = "SMT-COMP 2025 QF_BV"

    # Collect files — filter by max size
    max_bytes = args.maxsize * 1024
    all_files = []
    for d in data_dirs:
        if d.exists():
            for f in d.rglob("*.smt2"):
                if f.stat().st_size <= max_bytes:
                    all_files.append(f)

    if not all_files:
        print(f"[error] No .smt2 files <= {args.maxsize}KB found.")
        sys.exit(1)

    files = random.sample(all_files, min(args.n, len(all_files)))
    print(f"[eval] {label}")
    print(f"[eval] {len(all_files)} files <= {args.maxsize}KB available, testing {len(files)}")
    print(f"[eval] Timeout: {args.timeout}ms | Model: {model}\n")

    # Load NeuroSym
    print("[eval] Loading NeuroSym...")
    if "bv" in model:
        ns = GANSATSolver(bv_model_path=model, timeout_ms=args.timeout)
    else:
        ns = GANSATSolver(model_path=model, timeout_ms=args.timeout)
    print("[eval] Ready.\n")

    results = []
    print(f"{'#':<5} {'File':<38} {'Z3 result':>10} {'Z3 ms':>8} {'NS result':>10} {'NS ms':>8} {'Spd':>6}  Match")
    print("-" * 94)

    for i, fpath in enumerate(files):
        z3_res, z3_ms = solve_z3_only(str(fpath), args.timeout)
        ns_res, ns_ms = solve_neurosym_timed(ns, fpath, timeout_sec=args.timeout/1000 + 3)

        match   = "OK" if z3_res == ns_res else f"!MISMATCH"
        speedup = z3_ms / ns_ms if ns_ms > 1 else 0.0

        results.append(dict(z3_res=z3_res, ns_res=ns_res,
                             z3_ms=z3_ms, ns_ms=ns_ms,
                             speedup=speedup, match=match))

        sp_s = f"{speedup:.1f}x" if speedup > 0 else "-"
        print(f"{i+1:<5} {fpath.name[:36]:<38} {z3_res:>10} {z3_ms:>7.0f}ms {ns_res:>10} {ns_ms:>7.0f}ms {sp_s:>6}  {match}")

    # ── Summary ───────────────────────────────────────────────────────────────
    n     = len(results)
    sat   = [r for r in results if r["z3_res"] == "sat"]
    unsat = [r for r in results if r["z3_res"] == "unsat"]
    bad   = [r for r in results if r["match"] != "OK"]
    ns_faster = sum(1 for r in results if r["ns_ms"] > 0 and r["ns_ms"] < r["z3_ms"] * 0.95)
    z3_faster = sum(1 for r in results if r["ns_ms"] > 0 and r["z3_ms"] < r["ns_ms"] * 0.95)

    sat_both  = [r for r in sat if r["ns_res"] == "sat"]
    avg_z3    = sum(r["z3_ms"] for r in sat_both) / max(len(sat_both), 1)
    avg_ns    = sum(r["ns_ms"] for r in sat_both) / max(len(sat_both), 1)

    print("\n" + "=" * 94)
    print(f"  RESULTS — {label}")
    print("=" * 94)
    print(f"  Benchmarks tested            : {n}")
    print(f"  SAT / UNSAT / Unknown        : {len(sat)} / {len(unsat)} / {n-len(sat)-len(unsat)}")
    print()
    print(f"  NeuroSym faster              : {ns_faster}/{n}  ({100*ns_faster/n:.1f}%)")
    print(f"  Z3 faster                    : {z3_faster}/{n}  ({100*z3_faster/n:.1f}%)")
    print()
    print(f"  Avg Z3 time   (SAT, both)    : {avg_z3:.1f} ms")
    print(f"  Avg NS time   (SAT, both)    : {avg_ns:.1f} ms")
    if avg_ns > 0 and avg_z3 > 0:
        print(f"  Avg speedup   (SAT, both)    : {avg_z3/avg_ns:.2f}x")
    print()
    if bad:
        print(f"  !! CORRECTNESS ERRORS        : {len(bad)}")
        for r in bad[:5]:
            print(f"     Z3={r['z3_res']}  NS={r['ns_res']}")
    else:
        print(f"  Correctness                  : PERFECT  (0 mismatches)")
    print("=" * 94)


if __name__ == "__main__":
    main()
