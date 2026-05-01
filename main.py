"""
GANSAT — SMT-COMP '26 competition entry point.

SMT-COMP interface:
  - Input : SMT-LIB 2 formula via stdin or file argument
  - Output: sat / unsat / unknown  (+ model if sat)
  - Exit  : 0 for sat/unsat, 1 for unknown/error

Usage (SMT-COMP harness):
    python main.py benchmark.smt2
    python main.py --model models/gansat.pt benchmark.smt2
    echo "(set-logic QF_LIA)..." | python main.py --stdin
"""

import sys
import os
import argparse

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

from gansat.solver import GANSATSolver, format_output, RESULT_SAT, RESULT_UNSAT


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("input_file", nargs="?", default=None)
    parser.add_argument("--model",      default=os.path.join(_ROOT, "models", "gansat.pt"))
    parser.add_argument("--stdin",      action="store_true")
    parser.add_argument("--candidates", type=int, default=16)
    parser.add_argument("--timeout",    type=int, default=20_000)
    parser.add_argument("--device",     default="cpu")
    args = parser.parse_args()

    model_path = args.model if os.path.exists(args.model) else None

    solver = GANSATSolver(
        model_path=model_path,
        n_candidates=args.candidates,
        timeout_ms=args.timeout,
        device=args.device,
    )

    try:
        if args.stdin or args.input_file is None:
            smtlib_str = sys.stdin.read()
            result, model, _ = solver.solve_string(smtlib_str)
        else:
            result, model, _ = solver.solve_file(args.input_file)
    except Exception as e:
        print("unknown", flush=True)
        sys.exit(1)

    print(format_output(result, model), flush=True)
    sys.exit(0 if result in (RESULT_SAT, RESULT_UNSAT) else 1)


if __name__ == "__main__":
    main()
