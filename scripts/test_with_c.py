"""
test_with_c.py — End-to-end pipeline: C file → SMT-LIB → GANSAT solver

Pipeline:
  1. Read C file
  2. Extract path constraints → .smt2 files
  3. Run GANSAT on each path constraint
  4. Print: target path, generated test input, verification result

Usage:
  python scripts/test_with_c.py
  python scripts/test_with_c.py --c tests/sample.c --model models/gansat.pt
"""

import sys
import argparse
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.c_to_smt import convert, PATH_SPECS
from gansat.solver import GANSATSolver, RESULT_SAT, RESULT_UNSAT, RESULT_UNKNOWN
from gansat.parser import parse_file


BANNER = "=" * 60


def run_pipeline(c_file: str, model_path: str = None, n_candidates: int = 16):
    print(f"\n{BANNER}")
    print(f"  GANSAT — C Program Test Case Generator")
    print(f"  C file : {c_file}")
    print(f"  Model  : {model_path or 'untrained (Z3 fallback only)'}")
    print(BANNER)

    # Step 1: Extract path constraints from C file
    out_dir = "data/c_paths"
    print(f"\n[Step 1] Extracting path constraints from {c_file}...")
    smt_files = convert(c_file, out_dir)

    # Step 2: Solve each constraint with GANSAT
    solver = GANSATSolver(
        model_path=model_path,
        n_candidates=n_candidates,
        timeout_ms=10_000,
    )

    print(f"\n[Step 2] Solving {len(smt_files)} path constraints with GANSAT...\n")

    results = []
    for name, smt_path in smt_files:
        spec = next(s for s in PATH_SPECS if s["name"] == name)
        result, model, elapsed_ms = solver.solve_file(smt_path)
        results.append((spec, result, model, elapsed_ms))

    # Step 3: Report
    print(f"\n{BANNER}")
    print(f"  RESULTS — Generated Test Inputs")
    print(BANNER)

    all_passed = True
    for spec, result, model, elapsed_ms in results:
        print(f"\n  Function : {spec['function']}")
        print(f"  Target   : {spec['target']}")
        print(f"  Status   : {result.upper()}  ({elapsed_ms:.1f}ms)")

        if result == RESULT_SAT and model:
            print(f"  Test Input Generated:")
            for var in spec["variables"]:
                val = model.get(var, "?")
                print(f"    {var:20s} = {val}")
            _verify_c_manually(spec, model)

        elif result == RESULT_UNSAT:
            print(f"  [!] UNSAT — this path is unreachable (good finding!)")
            all_passed = False

        elif result == RESULT_UNKNOWN:
            print(f"  [!] UNKNOWN — timeout or out of scope")
            all_passed = False

        print(f"  {'-'*56}")

    print(f"\n{BANNER}")
    solved = sum(1 for _, r, _, _ in results if r == RESULT_SAT)
    print(f"  Summary: {solved}/{len(results)} paths covered")
    print(BANNER)
    return results


def _verify_c_manually(spec: dict, model: dict):
    """Re-evaluate constraints manually with the generated values."""
    name = spec["name"]
    m = model  # shorthand

    try:
        if name == "triangle_equilateral":
            a, b, c = m.get("a", 0), m.get("b", 0), m.get("c", 0)
            ok = a > 0 and b > 0 and c > 0
            ok = ok and (a + b > c) and (a + c > b) and (b + c > a)
            ok = ok and (a == b == c)
            label = "equilateral ✓" if ok else "NOT equilateral ✗"
            print(f"    → classify_triangle({a},{b},{c}) = {label}")

        elif name == "loan_approved":
            age = m.get("age", 0)
            sal = m.get("salary", 0)
            cs  = m.get("credit_score", 0)
            ok = (18 <= age <= 65) and sal >= 30000 and cs >= 700
            ok = ok and (sal + cs * 10 >= 37000)
            label = "APPROVED ✓" if ok else "NOT approved ✗"
            print(f"    → check_loan({age},{sal},{cs}) = {label}")

        elif name == "schedule_no_conflict":
            s1 = m.get("start1", 0); e1 = m.get("end1", 0)
            s2 = m.get("start2", 0); e2 = m.get("end2", 0)
            ok = (e1 > s1) and (e2 > s2) and (e1 <= s2 or e2 <= s1)
            label = "no conflict ✓" if ok else "HAS conflict ✗"
            print(f"    → has_conflict({s1},{e1},{s2},{e2}) = {label}")

        elif name == "safe_buffer_access":
            idx = m.get("index", 0)
            sz  = m.get("size", 0)
            off = m.get("offset", 0)
            ok = sz > 0 and 0 <= idx < sz and off >= 0 and (idx + off) < sz
            label = f"safe → returns {idx+off} ✓" if ok else "UNSAFE ✗"
            print(f"    → safe_access({idx},{sz},{off}) = {label}")

    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--c",          default="tests/sample.c")
    parser.add_argument("--model",      default=None)
    parser.add_argument("--candidates", type=int, default=16)
    args = parser.parse_args()

    model = args.model if args.model and Path(args.model).exists() else None
    run_pipeline(args.c, model_path=model, n_candidates=args.candidates)


if __name__ == "__main__":
    main()
