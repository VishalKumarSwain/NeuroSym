"""In-process MiniSat backend via a tiny extern "C" shim
(ns_minisat_shim.cpp) around Minisat::Solver, loaded with ctypes --
no pybind11/cffi/Cython dependency, none of those were installed.

Same interface and return contract as ns_dpll.solve_cnf/ns_minisat.solve_cnf:
{var: bool} on SAT, None on UNSAT or if unavailable/times out.

This backend does not serialize to DIMACS text at all: clauses are handed
to Minisat::Solver directly as literal integers, via one bulk C call for
the whole CNF (see ns_ms_add_clauses_bulk in the shim) rather than one
Python->C call per clause -- built specifically to avoid ~2.6M individual
FFI calls on a real captured formula.

MiniSat's own solving algorithm/heuristics/options are completely
untouched -- this only changes how clauses and the result cross the
Python/C boundary, matching the existing subprocess backend's semantics
exactly (same variable numbering: SAT var N == DIMACS var N == literal
magnitude N, matching ns_minisat.py's convention already).
"""

import ctypes
import os
from typing import Dict, List, Optional

try:
    import numpy as np
except ImportError:
    np = None

_LIB_PATH = os.environ.get(
    "NEUROSYM_MINISAT_SHIM",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "libns_minisat_shim.so"),
)

_lib = None
_load_failed = False


def _load():
    global _lib, _load_failed
    if _lib is not None or _load_failed:
        return _lib is not None
    if np is None or not os.path.exists(_LIB_PATH):
        _load_failed = True
        return False
    try:
        lib = ctypes.CDLL(_LIB_PATH)
        # Both the plain-Solver (ns_ms_*) and SimpSolver (ns_mss_*) entry
        # points share the exact same signatures, just different concrete
        # C++ types behind them (see ns_minisat_shim.cpp's comment on why
        # two parallel sets exist rather than one generic one -- Solver::
        # solve() is not virtual, so dispatch through the wrong static type
        # would silently skip SimpSolver's preprocessing).
        for prefix in ("ns_ms", "ns_mss"):
            getattr(lib, f"{prefix}_create").restype = ctypes.c_void_p
            getattr(lib, f"{prefix}_create").argtypes = []
            getattr(lib, f"{prefix}_destroy").argtypes = [ctypes.c_void_p]
            getattr(lib, f"{prefix}_ensure_vars").argtypes = [ctypes.c_void_p, ctypes.c_int]
            fn = getattr(lib, f"{prefix}_add_clauses_bulk")
            fn.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.c_int,
            ]
            fn.restype = ctypes.c_int
            getattr(lib, f"{prefix}_solve").argtypes = [ctypes.c_void_p]
            getattr(lib, f"{prefix}_solve").restype = ctypes.c_int
            getattr(lib, f"{prefix}_model_value").argtypes = [ctypes.c_void_p, ctypes.c_int]
            getattr(lib, f"{prefix}_model_value").restype = ctypes.c_int
            getattr(lib, f"{prefix}_n_vars").argtypes = [ctypes.c_void_p]
            getattr(lib, f"{prefix}_n_vars").restype = ctypes.c_int
            for name in ("conflicts", "decisions", "propagations", "restarts"):
                fn = getattr(lib, f"{prefix}_{name}")
                fn.argtypes = [ctypes.c_void_p]
                fn.restype = ctypes.c_longlong
        lib.ns_mss_eliminated_vars.argtypes = [ctypes.c_void_p]
        lib.ns_mss_eliminated_vars.restype = ctypes.c_int
        _lib = lib
        return True
    except OSError:
        _load_failed = True
        return False


def available() -> bool:
    return _load()


def last_stats() -> Optional[dict]:
    """Populated by the most recent solve_cnf() call in this process, or
    None if none has run yet / the native backend was unavailable. Exposes
    what the subprocess backend (ns_minisat.py) silently discards."""
    return _LAST_STATS[0]


_LAST_STATS = [None]


def solve_cnf(
    clauses: List[List[int]], n_vars: int,
    deadline: Optional[float] = None,
    use_simp: bool = False,
) -> Optional[Dict[int, bool]]:
    """use_simp=True routes through Minisat::SimpSolver (preprocessing
    enabled, matching the installed `minisat` CLI's own default entry
    point) instead of plain Minisat::Solver. Same {var: bool}/None
    contract either way."""
    if not _load() or not clauses:
        return {} if not clauses else None

    p = "ns_mss" if use_simp else "ns_ms"
    create   = getattr(_lib, f"{p}_create")
    destroy  = getattr(_lib, f"{p}_destroy")
    ensure   = getattr(_lib, f"{p}_ensure_vars")
    add_bulk = getattr(_lib, f"{p}_add_clauses_bulk")
    do_solve = getattr(_lib, f"{p}_solve")
    modelval = getattr(_lib, f"{p}_model_value")

    solver = create()
    try:
        ensure(ctypes.c_void_p(solver), n_vars)

        # Flatten via numpy (vectorized, not a million-element Python loop)
        # for the bulk transfer the shim expects: one flat literal array +
        # one clause-length array, both int32.
        flat_lits = np.fromiter(
            (lit for clause in clauses for lit in (clause or [0])), dtype=np.int32
        )
        # An empty clause is trivially unsatisfiable; DIMACS text handles
        # this with two forcing unit clauses (see ns_minisat.py's
        # _write_dimacs) -- here we can just pass it through as a
        # zero-length clause and let addClause's own empty-vec handling
        # (which MiniSat treats as an immediate conflict) do the same job
        # natively, without the two-unit-clause workaround text format
        # needed.
        lens = np.array(
            [0 if not c else len(c) for c in clauses], dtype=np.int32
        )
        # Filter out the placeholder 0 we fed fromiter for empty clauses
        # (fromiter needs at least one element per clause to iterate;
        # zero-length clauses contributed a bogus literal "0" above that
        # must not reach the solver as a real literal).
        if any(not c for c in clauses):
            flat_lits = np.fromiter(
                (lit for clause in clauses for lit in (clause or [])), dtype=np.int32
            )

        lits_ptr = flat_lits.ctypes.data_as(ctypes.POINTER(ctypes.c_int))
        lens_ptr = lens.ctypes.data_as(ctypes.POINTER(ctypes.c_int))
        add_bulk(ctypes.c_void_p(solver), lits_ptr, lens_ptr, len(clauses))

        # deadline is accepted for interface parity with ns_dpll/ns_minisat
        # but not enforced mid-search -- neither Solver::solve() nor
        # SimpSolver::solve() take a wall-clock budget in this build; a
        # genuinely enforced deadline would need MiniSat's own conflict/
        # propagation budget API (setConfBudget/setPropBudget), not
        # implemented in this shim. Timeout enforcement for this backend
        # is therefore weaker than the subprocess backend (which is killed
        # via subprocess.run's own `timeout=`) -- documented limitation,
        # not silently assumed away.
        sat = do_solve(ctypes.c_void_p(solver))

        _LAST_STATS[0] = {
            "conflicts": getattr(_lib, f"{p}_conflicts")(ctypes.c_void_p(solver)),
            "decisions": getattr(_lib, f"{p}_decisions")(ctypes.c_void_p(solver)),
            "propagations": getattr(_lib, f"{p}_propagations")(ctypes.c_void_p(solver)),
            "restarts": getattr(_lib, f"{p}_restarts")(ctypes.c_void_p(solver)),
        }
        if use_simp:
            _LAST_STATS[0]["eliminated_vars"] = _lib.ns_mss_eliminated_vars(
                ctypes.c_void_p(solver))

        if not sat:
            return None

        assignment = {}
        for v in range(1, n_vars + 1):
            val = modelval(ctypes.c_void_p(solver), v)
            # l_Undef (-1): MiniSat left this variable unconstrained --
            # same situation ns_minisat.py's file-based parser already
            # handles by simply omitting it from the returned dict (see
            # _parse_minisat_output: only literals actually present in the
            # model line are added). Match that -- omit rather than guess
            # true/false for a variable MiniSat itself didn't pin down.
            if val >= 0:
                assignment[v] = bool(val)
        return assignment
    finally:
        destroy(ctypes.c_void_p(solver))
