"""
gansat_bridge.py — Python subprocess bridge for KLEE ↔ GANSAT communication.

This script is called by gansat_solver.cpp via popen().
It reads an SMT-LIB 2 formula from stdin, runs GANSAT, and writes
the result to stdout in the exact format KLEE expects.

Usage (by C++ plugin):
    echo "(set-logic QF_BV)..." | python gansat_bridge.py

Usage (standalone test):
    python klee_plugin/gansat_bridge.py < tests/sample_bv.smt2

Environment variables:
    GANSAT_MODEL_LIA  — path to trained QF_LIA generator weights
    GANSAT_MODEL_BV   — path to trained QF_BV generator weights
    GANSAT_CANDIDATES — number of GAN candidates (default: 16)
    GANSAT_TIMEOUT_MS — Z3 fallback timeout in ms (default: 20000)
    GANSAT_DEVICE     — "cpu" or "cuda" (default: cpu)
"""

import sys
import os

# Add project root to Python path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from gansat.solver import GANSATSolver, format_output, RESULT_SAT, RESULT_UNSAT


def main():
    smtlib_input = sys.stdin.read()
    if not smtlib_input.strip():
        print("unknown", flush=True)
        sys.exit(1)

    model_lia    = os.environ.get("GANSAT_MODEL_LIA",  None)
    model_bv     = os.environ.get("GANSAT_MODEL_BV",   None)
    n_candidates = int(os.environ.get("GANSAT_CANDIDATES", "16"))
    timeout_ms   = int(os.environ.get("GANSAT_TIMEOUT_MS", "20000"))
    device       = os.environ.get("GANSAT_DEVICE", "cpu")

    # Validate model paths
    if model_lia and not os.path.exists(model_lia):
        model_lia = None
    if model_bv and not os.path.exists(model_bv):
        model_bv = None

    try:
        solver = GANSATSolver(
            model_path    = model_lia,
            bv_model_path = model_bv,
            n_candidates  = n_candidates,
            timeout_ms    = timeout_ms,
            device        = device,
        )
        result, model, elapsed_ms = solver.solve_string(smtlib_input)
    except Exception as e:
        sys.stderr.write(f"[gansat_bridge] error: {e}\n")
        print("unknown", flush=True)
        sys.exit(1)

    print(format_output(result, model), flush=True)
    sys.exit(0 if result in (RESULT_SAT, RESULT_UNSAT) else 1)


if __name__ == "__main__":
    main()
