"""
Evaluate GANSAT vs Z3 baseline on a benchmark set.

Metrics:
  - solved_count      : how many benchmarks solved correctly
  - gan_fast_path_pct : % solved via GAN fast path (no Z3 search)
  - avg_time_ms       : average wall-clock time per benchmark
  - speedup           : GANSAT vs Z3-only average time

Usage:
    python scripts/evaluate.py --data data/benchmarks --model models/gansat.pt
"""

import argparse
import sys
import time
from pathlib import Path

import z3
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gansat.parser import parse_file
from gansat.solver import GANSATSolver, RESULT_SAT, RESULT_UNSAT, RESULT_UNKNOWN
from gansat.encoder import encode


def z3_solve_time(formula_path: str, timeout_ms: int = 20_000) -> tuple:
    t0 = time.time()
    solver = z3.Solver()
    solver.set("timeout", timeout_ms)
    solver.from_file(formula_path)
    result = solver.check()
    elapsed = (time.time() - t0) * 1000
    if result == z3.sat:
        return RESULT_SAT, elapsed
    elif result == z3.unsat:
        return RESULT_UNSAT, elapsed
    return RESULT_UNKNOWN, elapsed


def evaluate(
    data_dir: str,
    model_path: str = None,
    n_candidates: int = 16,
    timeout_ms: int = 20_000,
    max_files: int = 500,
    device: str = "cpu",
):
    files = list(Path(data_dir).rglob("*.smt2"))[:max_files]
    if not files:
        print("[error] No .smt2 files found.")
        sys.exit(1)
    print(f"[eval] Evaluating on {len(files)} benchmarks")

    gansat = GANSATSolver(
        model_path=model_path,
        n_candidates=n_candidates,
        timeout_ms=timeout_ms,
        device=device,
    )

    stats = {
        "total": 0, "gansat_correct": 0, "z3_correct": 0,
        "gan_fast": 0, "z3_fallback": 0, "unknown": 0,
        "gansat_times": [], "z3_times": [],
        "mismatches": [],
    }

    for path in tqdm(files):
        stats["total"] += 1
        path_str = str(path)

        # GANSAT
        try:
            g_result, g_model, g_time = gansat.solve_file(path_str)
        except Exception as e:
            g_result, g_model, g_time = RESULT_UNKNOWN, None, timeout_ms
        stats["gansat_times"].append(g_time)

        # Z3 baseline
        try:
            z_result, z_time = z3_solve_time(path_str, timeout_ms)
        except Exception:
            z_result, z_time = RESULT_UNKNOWN, timeout_ms
        stats["z3_times"].append(z_time)

        # Compare
        if z_result == RESULT_UNKNOWN:
            stats["unknown"] += 1
            continue

        if g_result == z_result:
            stats["gansat_correct"] += 1
        else:
            stats["mismatches"].append((path_str, g_result, z_result))

        stats["z3_correct"] += 1

        if g_result == RESULT_SAT and g_time < z_time * 0.9:
            stats["gan_fast"] += 1
        else:
            stats["z3_fallback"] += 1

    _print_report(stats)


def _print_report(stats: dict):
    total    = stats["total"]
    valid    = total - stats["unknown"]
    g_times  = stats["gansat_times"]
    z_times  = stats["z3_times"]
    avg_g    = sum(g_times) / len(g_times) if g_times else 0
    avg_z    = sum(z_times) / len(z_times) if z_times else 0
    speedup  = avg_z / avg_g if avg_g > 0 else 1.0
    accuracy = stats["gansat_correct"] / max(valid, 1) * 100

    print("\n" + "="*50)
    print("  GANSAT Evaluation Report")
    print("="*50)
    print(f"  Benchmarks evaluated : {total}")
    print(f"  Valid (non-timeout)  : {valid}")
    print(f"  GANSAT accuracy      : {accuracy:.1f}%")
    print(f"  GAN fast-path wins   : {stats['gan_fast']}")
    print(f"  Z3 fallback used     : {stats['z3_fallback']}")
    print(f"  Avg GANSAT time (ms) : {avg_g:.1f}")
    print(f"  Avg Z3-only time (ms): {avg_z:.1f}")
    print(f"  Speedup vs Z3        : {speedup:.2f}x")
    if stats["mismatches"]:
        print(f"\n  Mismatches ({len(stats['mismatches'])}):")
        for path, g, z in stats["mismatches"][:5]:
            print(f"    {Path(path).name}: GANSAT={g}, Z3={z}")
    print("="*50)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data",        default="data/benchmarks")
    parser.add_argument("--model",       default=None)
    parser.add_argument("--candidates",  type=int,   default=16)
    parser.add_argument("--timeout",     type=int,   default=20_000)
    parser.add_argument("--max",         type=int,   default=500)
    parser.add_argument("--device",      default="cpu")
    args = parser.parse_args()

    evaluate(
        data_dir=args.data,
        model_path=args.model,
        n_candidates=args.candidates,
        timeout_ms=args.timeout,
        max_files=args.max,
        device=args.device,
    )


if __name__ == "__main__":
    main()
