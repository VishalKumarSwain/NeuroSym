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

Profiling (off by default, never touches stdout -- see --profile/--profile-json):
    python main.py benchmark.smt2 --profile
    python main.py benchmark.smt2 --profile-json out.json
    python main.py benchmark.smt2 --disable-gan   # controlled GAN-first vs symbolic-only comparison
"""

import sys
import os
import json
import time

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
    parser.add_argument(
        "--minisat-timeout", type=int, default=20 * 60_000,
        help="Minimum time budget (ms) for the CNF search (MiniSat when "
             "installed, ns_dpll otherwise) specifically -- independent of "
             "--timeout, and never shorter than it. Only once this is "
             "actually exhausted does NeuroSym report unknown and (unless "
             "--no-fallback) hand the formula to boolector/z3. Default "
             "1200000 (20 min).")
    parser.add_argument("--device",    default="cpu")
    parser.add_argument(
        "--no-fallback", action="store_true",
        help="Disable the external-solver fallback; report NeuroSym's own "
             "'unknown' as-is instead of handing the formula to z3/boolector.")
    parser.add_argument(
        "--fallback-timeout", type=int, default=30_000,
        help="Time budget (ms) for the external-solver fallback, tried only "
             "when NeuroSym's own pipeline returns unknown. Default 30000.")
    parser.add_argument(
        "--profile", action="store_true",
        help="Print a human-readable timing/characteristics breakdown to "
             "stderr after solving. Never writes to stdout -- the SMT-LIB2 "
             "model output there is unchanged. Off by default; adds "
             "negligible overhead (a handful of time.time() calls) when on.")
    parser.add_argument(
        "--profile-json", metavar="PATH", default=None,
        help="Append one JSON object (the same data --profile prints) as a "
             "line to PATH -- machine-readable, for a benchmark sweep. "
             "Implies the same profiling collection as --profile; does not "
             "require --profile to also be passed.")
    parser.add_argument(
        "--disable-gan", action="store_true",
        help="Experimental: force the symbolic-only path even when a GAN "
             "model is configured and the formula would otherwise be "
             "eligible. For controlled GAN-first vs symbolic-only "
             "benchmarking; does not remove or alter the GAN path itself. "
             "Off by default -- normal behavior (GAN-first) is unchanged.")
    args = parser.parse_args()

    profiling = args.profile or bool(args.profile_json)

    bv_model_path  = args.bv_model  if os.path.exists(args.bv_model)  else None
    lia_model_path = args.lia_model if os.path.exists(args.lia_model) else None
    model_path     = args.model     if os.path.exists(args.model)     else None

    solver = NeuroSymSolver(
        model_path=model_path,
        bv_model_path=bv_model_path,
        lia_model_path=lia_model_path,
        n_candidates=args.candidates,
        timeout_ms=args.timeout,
        minisat_timeout_ms=args.minisat_timeout,
        device=args.device,
        profile=profiling,
        disable_gan=args.disable_gan,
    )

    if args.stdin or args.input_file is None:
        smtlib_str = sys.stdin.read()
    else:
        with open(args.input_file) as f:
            smtlib_str = f.read()

    # Parse once, up front -- both to solve it and, before that, to decide
    # *whether* to bother running NeuroSym's own pipeline at all.
    parse_t0 = time.time()
    try:
        formula = parse_string(smtlib_str)
    except Exception:
        formula = None
    outer_parse_ms = (time.time() - parse_t0) * 1000

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

    prof = solver.last_profile  # None unless profiling was on
    if prof is not None:
        # solve_formula() doesn't see this file's own parsing (it solves an
        # already-parsed formula -- see the docstring on solve_formula), so
        # fill in the parse time measured here instead of leaving it 0.
        prof["parse_ms"] = outer_parse_ms
        prof["formula_name"] = args.input_file or "<stdin>"

    # NeuroSym's own pipeline (GAN candidate path + from-scratch DPLL/LIA
    # fallback) can legitimately run out of steam on a genuinely large
    # formula -- that's a real search-cost limit, not always a wrong
    # answer waiting to be found. Rather than report "unknown" (or crash)
    # and give the caller nothing, hand the same formula to a real solver
    # before giving up. This only ever fills in NeuroSym's "I couldn't
    # decide" case -- a sat/unsat verdict NeuroSym already reached is
    # never re-litigated here.
    if result == RESULT_UNKNOWN and not args.no_fallback:
        fb_t0 = time.time()
        fb_result, fb_model = try_external_fallback(
            smtlib_str, timeout_s=args.fallback_timeout / 1000.0)
        if prof is not None:
            prof["fallback_ms"] = (time.time() - fb_t0) * 1000
            prof["external_fallback_used"] = fb_result != RESULT_UNKNOWN
        if fb_result != RESULT_UNKNOWN:
            result, model = fb_result, fb_model

    if prof is not None:
        prof["final_result"] = result
        _emit_profile(prof, args)

    if formula is None:
        # Nothing could even be parsed -- print the bare verdict, no model.
        print(result, flush=True)
        sys.exit(0 if result in (RESULT_SAT, RESULT_UNSAT) else 1)

    try:
        fmt_t0 = time.time()
        output = format_output(result, model, formula.variables)
        if prof is not None:
            prof["format_ms"] = (time.time() - fmt_t0) * 1000
            # format_ms is known only after printing would otherwise already
            # have happened; re-emit is cheap (one JSON line / stderr block)
            # and keeps --profile-json's one-line-per-solve contract intact
            # rather than leaving format_ms permanently at 0.
            _emit_profile(prof, args, rewrite=True)
        print(output, flush=True)
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


def _emit_profile(prof: dict, args, rewrite: bool = False):
    """stderr (human-readable, --profile) and/or a JSONL file
    (machine-readable, --profile-json) -- stdout is never touched, so the
    SMT-LIB2 model output there stays exactly as a caller (ESBMC's
    neurosym_convt, in particular) already expects it.

    `rewrite`: called a second time once format_ms is known, so the JSONL
    line reflects it; the first call's stderr block (if --profile) is left
    as printed rather than reprinted, since format_ms is a small, mostly
    uninteresting number and terminal output isn't meant to be parsed."""
    if args.profile and not rewrite:
        lines = [f"[profile] {prof.get('formula_name')}"]
        for key in (
            "final_result", "logic", "declared_var_count", "assertion_count",
            "uses_arrays", "gan_attempted", "gan_skipped_reason", "gan_success",
            "symbolic_fallback_used", "minisat_used", "dpll_used",
            "external_fallback_used",
        ):
            lines.append(f"  {key:28s} {prof.get(key)}")
        lines.append("  --- timers (ms) ---")
        for key in (
            "parse_ms", "formula_analysis_ms", "torch_import_ms", "model_load_ms",
            "gan_encode_ms", "gan_inference_ms", "gan_verify_ms", "gan_ms",
            "bitblast_ms", "minisat_ms", "dpll_ms", "lia_solver_ms", "verify_ms",
            "fallback_ms", "format_ms", "total_ms",
        ):
            lines.append(f"  {key:28s} {prof.get(key):.3f}")
        print("\n".join(lines), file=sys.stderr, flush=True)

    if args.profile_json:
        # One JSON object per line (JSONL) so a sweep script can append
        # across many formulas/repetitions into one file without needing
        # to parse/rewrite a wrapping array each time. `rewrite=True`
        # appends a second, corrected line rather than mutating the first
        # (files are opened in append mode) -- a sweep script should keep
        # the *last* line per (formula, repetition) if both are present;
        # documented here rather than adding file-seek logic to keep this
        # profiling-only code simple.
        with open(args.profile_json, "a") as f:
            f.write(json.dumps(prof, default=str) + "\n")


if __name__ == "__main__":
    main()
