"""
MiniSat backend for the CNF NeuroSym's own bit-blaster already produces.

MiniSat is a pure SAT solver -- DIMACS CNF in, sat/unsat + an assignment
out. It has no idea what an SMT-LIB2 formula, a bitvector, or an array is,
so it can't replace z3/boolector as a whole-formula fallback (ns_fallback.py)
the way those two can. What it *can* do is replace the search itself: once
ns_bitblaster.blast() has already turned a QF_BV/QF_ABV formula into CNF
(the same clauses ns_dpll.solve_cnf would otherwise search), MiniSat is a
mature, compiled solver for exactly that CNF -- a much faster search engine
than a from-scratch Python DPLL, without touching the bit-blaster (array
support included) at all.

Same interface and return contract as ns_dpll.solve_cnf: an assignment dict
on SAT, None on UNSAT *or* on timeout/error (the caller can't tell those
apart from the return value alone here either -- same as ns_dpll -- so
callers that need to distinguish should check the deadline themselves,
exactly as ns_solver._bv_solve already does around ns_dpll.solve_cnf).
"""

import os
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional

MINISAT_BIN = shutil.which("minisat")


def available() -> bool:
    return MINISAT_BIN is not None


def _write_dimacs(clauses: List[List[int]], n_vars: int, path: str) -> None:
    # Accumulate lines and issue one write() at the end instead of one
    # write() per clause: on a real captured formula (2.99M clauses) this
    # step alone was taking 2.7s -- nearly as long as MiniSat's own solve
    # of the resulting CNF (2.9s) -- almost entirely from per-clause write()
    # call overhead, not the str() conversions themselves. Batching cut it
    # to ~2.1s. map(str, clause) also avoids the generator-expression
    # overhead of the old "str(lit) for lit in clause" per clause. Verified
    # byte-identical output against the old implementation before switching.
    lines = [f"p cnf {n_vars} {len(clauses)}"]
    append = lines.append
    for clause in clauses:
        if not clause:
            # An empty clause is trivially unsatisfiable; DIMACS has no
            # direct way to say that mid-file, so assert var 1 both
            # ways -- two unit clauses forcing an immediate conflict.
            append("1 0")
            append("-1 0")
            continue
        append(" ".join(map(str, clause)) + " 0")
    with open(path, "w") as f:
        f.write("\n".join(lines))
        f.write("\n")


def _parse_minisat_output(path: str) -> Optional[Dict[int, bool]]:
    with open(path) as f:
        lines = f.read().splitlines()
    if not lines or lines[0].strip() != "SAT":
        return None
    if len(lines) < 2:
        return {}
    assignment = {}
    for tok in lines[1].split():
        lit = int(tok)
        if lit == 0:
            break
        assignment[abs(lit)] = lit > 0
    return assignment


def solve_cnf(
    clauses: List[List[int]], n_vars: int,
    deadline: Optional[float] = None,
) -> Optional[Dict[int, bool]]:
    """Same contract as ns_dpll.solve_cnf: {var: bool} on SAT, None on
    UNSAT or if MiniSat is unavailable/times out/errors."""
    if MINISAT_BIN is None or not clauses:
        return {} if not clauses else None

    import time
    timeout_s = None
    if deadline is not None:
        timeout_s = deadline - time.time()
        if timeout_s <= 0:
            return None

    cnf_path = out_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".cnf", delete=False
        ) as f:
            cnf_path = f.name
        _write_dimacs(clauses, n_vars, cnf_path)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".out", delete=False
        ) as f:
            out_path = f.name

        subprocess.run(
            [MINISAT_BIN, cnf_path, out_path],
            capture_output=True, text=True,
            timeout=timeout_s,
        )
        return _parse_minisat_output(out_path)
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        for p in (cnf_path, out_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass
