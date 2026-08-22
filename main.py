"""
GANSAT — SMT-COMP '26 competition entry point.

SMT-COMP interface:
  - Input : SMT-LIB 2 formula via stdin or file argument
  - Output: sat / unsat / unknown  (+ model if sat)
  - Exit  : 0 for sat/unsat, 1 for unknown/error

Usage (SMT-COMP harness):
    python main.py benchmark.smt2
    python main.py --bv-model models/gansat_bv.pt benchmark.smt2
    echo "(set-logic QF_LIA)..." | python main.py --stdin
"""

import sys
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

# Bootstrap bundled dependencies — competition environment (Ubuntu 24.04) does not
# have z3-solver, bitwuzla, networkx, or pysmt; lib/ is pre-installed by build_archive.sh
_LIB = os.path.join(_ROOT, "lib")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)

sys.setrecursionlimit(100000)

import argparse

from gansat.ns_solver import NeuroSymSolver, format_output, RESULT_SAT, RESULT_UNSAT, RESULT_UNKNOWN
from gansat.ns_parser import parse_string
from gansat.ns_fallback import try_external_fallback


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("input_file",  nargs="?", default=None)
    parser.add_argument("--model",     default=os.path.join(_ROOT, "models", "gansat.pt"))
    parser.add_argument("--bv-model",  default=os.path.join(_ROOT, "models", "gansat_bv.pt"))
    parser.add_argument("--lia-model", default=os.path.join(_ROOT, "models", "gansat_lia.pt"))
    parser.add_argument("--stdin",     action="store_true")
    parser.add_argument("--candidates", type=int, default=8)
    parser.add_argument("--timeout",   type=int, default=20_000)
    parser.add_argument("--device",    default="cpu")
    parser.add_argument(
        "--no-fallback", action="store_true",
        help="Disable the external-solver fallback; report NeuroSym's own "
             "'unknown' as-is instead of handing the formula to z3/boolector.")
    parser.add_argument(
        "--fallback-timeout", type=int, default=30_000,
        help="Time budget (ms) for the external-solver fallback, tried only "
             "when NeuroSym's own pipeline returns unknown. Default 30000.")
    args = parser.parse_args()

    bv_model_path  = args.bv_model  if os.path.exists(args.bv_model)  else None
    lia_model_path = args.lia_model if os.path.exists(args.lia_model) else None
    model_path     = args.model     if os.path.exists(args.model)     else None

    solver = NeuroSymSolver(
        model_path=model_path,
        bv_model_path=bv_model_path,
        lia_model_path=lia_model_path,
        n_candidates=args.candidates,
        timeout_ms=args.timeout,
        device=args.device,
    )

    if args.stdin or args.input_file is None:
        smtlib_str = sys.stdin.read()
    else:
        with open(args.input_file) as f:
            smtlib_str = f.read()

    # Parse once, up front -- both to solve it and, before that, to decide
    # *whether* to bother running NeuroSym's own pipeline at all.
    try:
        formula = parse_string(smtlib_str)
    except Exception:
        formula = None

    # NeuroSym's own pipeline always gets first crack, arrays included.
    # (An earlier version skipped array-touching formulas straight to the
    # external fallback, measured back when the CNF search was a
    # from-scratch Python DPLL -- 60+s and still "unknown" on a formula z3
    # solved in under 10s. Now that the CNF search runs through MiniSat
    # (ns_minisat.py) instead, that formula solves in ~14s on its own, so
    # bypassing NeuroSym's own attempt no longer has a clear upside; let it
    # try first, in every case, and escalate only if it actually fails.)
    if formula is not None:
        try:
            result, model, _ = solver.solve_formula(formula)
        except Exception:
            result, model = RESULT_UNKNOWN, None
    else:
        result, model = RESULT_UNKNOWN, None

    # NeuroSym's own pipeline (GAN candidate path + from-scratch DPLL/LIA
    # fallback) can legitimately run out of steam on a genuinely large
    # formula -- that's a real search-cost limit, not always a wrong
    # answer waiting to be found. Rather than report "unknown" (or crash)
    # and give the caller nothing, hand the same formula to a real solver
    # before giving up. This only ever fills in NeuroSym's "I couldn't
    # decide" case -- a sat/unsat verdict NeuroSym already reached is
    # never re-litigated here.
    if result == RESULT_UNKNOWN and not args.no_fallback:
        fb_result, fb_model = try_external_fallback(
            smtlib_str, timeout_s=args.fallback_timeout / 1000.0)
        if fb_result != RESULT_UNKNOWN:
            result, model = fb_result, fb_model

    if formula is None:
        # Nothing could even be parsed -- print the bare verdict, no model.
        print(result, flush=True)
        sys.exit(0 if result in (RESULT_SAT, RESULT_UNSAT) else 1)

    try:
        print(format_output(result, model, formula.variables), flush=True)
    except BrokenPipeError:
        # The caller (e.g. ESBMC under --branch-coverage, which spawns one
        # NeuroSym subprocess per claim) can hit its own timeout and tear
        # down the pipe while we're mid-write. That's the caller giving up
        # on us, not a bug here -- exit quietly instead of an ugly traceback.
        # Standard fix for BrokenPipeError on stdout: redirect stdout to
        # devnull before exit so Python's own shutdown-time flush doesn't
        # raise the same error a second time.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(1)
    sys.exit(0 if result in (RESULT_SAT, RESULT_UNSAT) else 1)


if __name__ == "__main__":
    main()
