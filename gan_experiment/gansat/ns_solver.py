"""
NeuroSym standalone solver — no Z3, no Bitwuzla.

Pipeline:
  1. Parse with ns_parser (own SMT-LIB2 parser)
  2. GAN fast path (if PyTorch available):
       QF_BV/QF_ABV → bv_encode → BVIterativeGenerator → bv_decode → ns_evaluator verify
       QF_LIA       → encode    → IterativeGenerator   → decode    → ns_evaluator verify
  3. Symbolic fallback (own solvers):
       QF_BV/QF_ABV → ns_bitblaster → ns_dpll
       QF_LIA       → ns_lia
"""

import time
from typing import Optional, Tuple

from .ns_parser    import parse_file, parse_string
from .ns_ast       import NsFormula, BVSort, IntSort, BoolSort, ArraySort, App
from .ns_evaluator import evaluate

# PyTorch, numpy, and the GAN/encoder modules are only imported when a
# trained model is actually supplied — they're used exclusively by the GAN
# fast path (_lia_gan_path / _bv_gan_path). Importing them unconditionally
# at module load time cost ~1.1s total (torch ~1s, numpy via ns_encoder/
# ns_bv_encoder ~70-90ms) even on runs with no model to load, which was
# every run we ever tested with no models/ directory present.
torch = None
IterativeGenerator = BVIterativeGenerator = None
encode = decode_assignment = bv_encode = bv_decode_assignment = None


def _import_torch():
    """Import torch, the GAN generator classes, and the numpy-based
    encoders on first actual use, and cache them at module scope so
    repeated calls are free. Returns True if torch is available."""
    global torch, IterativeGenerator, BVIterativeGenerator
    global encode, decode_assignment, bv_encode, bv_decode_assignment
    if torch is not None:
        return True
    try:
        import torch as _torch
        from .gan    import IterativeGenerator as _lia_gen_cls
        from .bv_gan import BVIterativeGenerator as _bv_gen_cls
        from .ns_encoder    import encode as _encode, decode_assignment as _decode
        from .ns_bv_encoder import bv_encode as _bv_encode, bv_decode_assignment as _bv_decode
        torch = _torch
        IterativeGenerator = _lia_gen_cls
        BVIterativeGenerator = _bv_gen_cls
        encode, decode_assignment = _encode, _decode
        bv_encode, bv_decode_assignment = _bv_encode, _bv_decode
        return True
    except ImportError:
        return False


from .ns_bitblaster import blast, reconstruct
from .ns_dpll       import solve_cnf as _dpll_solve_cnf
from .ns_lia        import solve_lia
from . import ns_minisat
from . import ns_minisat_native
from .ns_fallback   import formula_uses_arrays
import os

# Which CNF backend solve_cnf() actually used on the last call -- profiling
# only, read by NeuroSymSolver right after calling it. A module-level flag
# rather than a return value change: solve_cnf()'s {var: bool}/None contract
# is used elsewhere (ns_minisat direct callers, tests) and must not change.
_last_cnf_backend = None

# Default as of the native-SimpSolver acceptance benchmark: in-process
# Minisat::SimpSolver (ns_minisat_native.py) via ctypes, no DIMACS file,
# no subprocess. Measured faster than the subprocess backend on every
# real formula tested (small formulas: 1.23-1.95x end-to-end; test.c:
# consistently 15-21% faster across repeated real ESBMC runs, though one
# standalone main.py measurement session was noisy/inconclusive on test.c
# specifically -- see the promotion report). Falls back automatically to
# "subprocess" if the native shared library isn't available in this
# environment (ns_minisat_native.available() is False) -- a missing/
# unbuilt .so is a deployment condition, not a reason to report UNKNOWN.
# NEUROSYM_MINISAT_BACKEND=subprocess forces the old behavior;
# =native selects plain Minisat::Solver (no preprocessing) for
# diagnostics/comparison only, not expected to be the fastest choice.
_MINISAT_BACKEND = os.environ.get("NEUROSYM_MINISAT_BACKEND", "native-simp")


def solve_cnf(clauses, n_vars, deadline=None):
    """CNF entry point for _bv_solve: MiniSat when it's installed (a
    mature, compiled SAT solver -- much faster search than a from-scratch
    Python DPLL on the exact same clauses our own bit-blaster produces),
    falling back to ns_dpll's own solver when it isn't. Same contract
    either way: {var: bool} on SAT, None on UNSAT or timeout.

    NEUROSYM_MINISAT_BACKEND=native-simp/native/subprocess selects the CNF
    backend; falls back to the subprocess backend (not straight to DPLL)
    if the chosen native shared library isn't available -- native-
    unavailable is an environment/build condition, not a signal that
    MiniSat itself can't help this formula.
    "native-simp" routes through Minisat::SimpSolver (preprocessing
    enabled, matching the installed `minisat` CLI's own default entry
    point) -- measured faster than both "subprocess" and plain "native"
    (Minisat::Solver, no preprocessing) on every real formula tested.
    "native" (plain Solver) is kept only as a diagnostic/comparison path,
    not because it's expected to win."""
    global _last_cnf_backend
    if _MINISAT_BACKEND in ("native-simp", "native") and ns_minisat_native.available():
        _last_cnf_backend = ("minisat_native_simp" if _MINISAT_BACKEND == "native-simp"
                              else "minisat_native")
        return ns_minisat_native.solve_cnf(
            clauses, n_vars, deadline=deadline,
            use_simp=(_MINISAT_BACKEND == "native-simp"))
    if ns_minisat.available():
        _last_cnf_backend = "minisat"
        return ns_minisat.solve_cnf(clauses, n_vars, deadline=deadline)
    _last_cnf_backend = "dpll"
    return _dpll_solve_cnf(clauses, n_vars, deadline=deadline)


RESULT_SAT     = "sat"
RESULT_UNSAT   = "unsat"
RESULT_UNKNOWN = "unknown"

_BV_LOGICS  = {"QF_BV", "QF_ABV", "QF_AUFBV", "BV"}
_LIA_LOGICS = {"QF_LIA", "QF_NIA", "QF_LRA", "LIA"}


def _analyze_ast(assertions):
    """(node_count, max_depth, operator_counts) over a list of assertion
    Terms -- iterative (explicit stack), not recursive, so a pathologically
    deep real formula can't blow the profiling code's own stack even where
    the parser itself raised sys.setrecursionlimit. Profiling-only: never
    called unless prof.enabled. O(n) over the AST, no memoization needed
    since this walks each formula once, not once per candidate/round."""
    node_count = 0
    max_depth  = 0
    op_counts: dict = {}
    stack = [(a, 1) for a in assertions]
    while stack:
        term, depth = stack.pop()
        node_count += 1
        if depth > max_depth:
            max_depth = depth
        if isinstance(term, App):
            op_counts[term.op] = op_counts.get(term.op, 0) + 1
            for child in term.args:
                stack.append((child, depth + 1))
    return node_count, max_depth, op_counts


class _Prof:
    """Profiling accumulator for one _solve() call. A no-op when disabled
    (record() just returns immediately) so the profiled and unprofiled code
    paths are otherwise byte-identical -- profiling must never change what
    gets solved or returned, only what gets additionally recorded.

    Convention: every field this collects is present in `data` after a
    solve, using 0 (not null/None) for a stage that did not run -- e.g.
    torch_import_ms is 0 both when the GAN path was never attempted at all
    and when torch was already imported by an earlier call in the same
    process (nothing to import, correctly distinct from "import happened
    but took 0ms", which does not occur in practice at this granularity).
    """

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.data: dict = {
            # timers (ms) -- see module docstring / README for the full list
            "parse_ms": 0.0,
            "formula_analysis_ms": 0.0,
            "torch_import_ms": 0.0,
            "model_load_ms": 0.0,
            "gan_encode_ms": 0.0,
            "gan_inference_ms": 0.0,
            "gan_verify_ms": 0.0,
            "gan_ms": 0.0,
            "bitblast_ms": 0.0,
            "minisat_ms": 0.0,
            "dpll_ms": 0.0,
            "lia_solver_ms": 0.0,
            "verify_ms": 0.0,
            "fallback_ms": 0.0,
            "format_ms": 0.0,
            "total_ms": 0.0,
            # formula characteristics
            "formula_name": None,
            "logic": None,
            "declared_var_count": 0,
            "ast_node_count": 0,
            "max_ast_depth": 0,
            "operator_counts": {},
            "bv_var_count": 0,
            "bool_var_count": 0,
            "array_var_count": 0,
            "total_bv_width": 0,
            "cnf_variable_count": 0,
            "cnf_clause_count": 0,
            "cnf_literal_count": 0,
            "cnf_unit_clause_count": 0,
            "cnf_binary_clause_count": 0,
            "cnf_max_clause_length": 0,
            "cnf_mean_clause_length": 0.0,
            "cnf_clause_var_ratio": 0.0,
            "assertion_count": 0,
            "uses_arrays": False,
            # GAN-specific
            "gan_attempted": False,
            "gan_skipped_reason": None,
            "gan_success": False,
            "gan_rounds_configured": None,
            "gan_candidate_count": None,
            # path taken
            "symbolic_fallback_used": False,
            "minisat_used": False,
            "dpll_used": False,
            "external_fallback_used": False,
            "external_fallback_program": None,
            "final_result": None,
        }

    def record(self, key: str, ms: float):
        if self.enabled:
            self.data[key] = self.data.get(key, 0.0) + ms

    def set(self, key: str, value):
        if self.enabled:
            self.data[key] = value


class _Timer:
    """`with _Timer(prof, "gan_ms"): ...` -- adds elapsed wall time to
    prof.data[key] (accumulates rather than overwrites, so a key touched
    from more than one call site in one solve, e.g. verify_ms from both
    the GAN path and the symbolic path, totals correctly instead of the
    second call silently clobbering the first)."""

    __slots__ = ("prof", "key", "_t0")

    def __init__(self, prof: "_Prof", key: str):
        self.prof = prof
        self.key = key

    def __enter__(self):
        if self.prof.enabled:
            self._t0 = time.time()
        return self

    def __exit__(self, *exc):
        if self.prof.enabled:
            self.prof.record(self.key, (time.time() - self._t0) * 1000)
        return False


class NeuroSymSolver:
    def __init__(
        self,
        model_path:     Optional[str] = None,
        bv_model_path:  Optional[str] = None,
        lia_model_path: Optional[str] = None,
        n_candidates:      int  = 8,
        timeout_ms:        int  = 20_000,
        minisat_timeout_ms: int = 20 * 60_000,
        device:            str  = "cpu",
        profile:           bool = False,
        disable_gan:       bool = False,
    ):
        self.n_candidates = n_candidates
        self.timeout_ms   = timeout_ms
        # The CNF search (MiniSat when installed, ns_dpll otherwise) gets
        # its own, larger minimum budget, independent of --timeout: a
        # genuinely large formula can need real search time, and the
        # GAN/LIA paths' short default timeout shouldn't cut that attempt
        # off early just because it governs everything else. Falling
        # through to the external solver chain (ns_fallback.py) only
        # happens once *this* budget is actually exhausted.
        self.minisat_timeout_ms = minisat_timeout_ms
        self._device_str  = device
        self.profile = profile
        # Experimental, off by default: forces the symbolic-only path even
        # when a GAN model is configured and the formula would otherwise
        # be eligible -- for controlled GAN-first vs symbolic-only
        # benchmarking. Does not remove or alter the GAN path itself.
        self.disable_gan = disable_gan
        self.last_profile: Optional[dict] = None

        # A model *path* being configured only means the GAN is available
        # to try, not that anything has been loaded yet -- torch import,
        # network construction, and the state_dict load (~2s, measured)
        # are deferred to _ensure_bv_gan_loaded/_ensure_lia_gan_loaded,
        # called only right before _solve() is actually about to attempt
        # that path. An array-touching formula skips the GAN branch
        # entirely (see _solve), so it now also skips paying this loading
        # cost at all -- not just the forward pass.
        self._lia_path   = lia_model_path or model_path
        self._bv_path    = bv_model_path
        self._use_lia_gan = bool(self._lia_path)
        self._use_bv_gan  = bool(self._bv_path)
        self._lia_gan_loaded = False
        self._bv_gan_loaded  = False
        self.device  = None
        self.lia_gen = None
        self.bv_gen  = None

    def _ensure_lia_gan_loaded(self, prof: _Prof) -> bool:
        if self._lia_gan_loaded:
            return self.lia_gen is not None
        self._lia_gan_loaded = True
        already_imported = torch is not None
        with _Timer(prof, "torch_import_ms" if not already_imported else "model_load_ms"):
            if not (self._use_lia_gan and _import_torch()):
                self._use_lia_gan = False
                return False
            self.device  = self.device or torch.device(self._device_str)
            self.lia_gen = IterativeGenerator().to(self.device)
            self.lia_gen.eval()
            state = torch.load(self._lia_path, map_location=self.device,
                               weights_only=True)
            self.lia_gen.load_state_dict(state)
        return True

    def _ensure_bv_gan_loaded(self, prof: _Prof) -> bool:
        if self._bv_gan_loaded:
            return self.bv_gen is not None
        self._bv_gan_loaded = True
        # torch's own import is by far the dominant one-time cost (~1.75s
        # measured); network construction + state_dict load after that is
        # small (~0.02s measured) -- split into two timer buckets so a
        # profile can tell "torch was already warm in this process" from
        # "this model's own load cost" instead of one conflated number.
        already_imported = torch is not None
        with _Timer(prof, "torch_import_ms" if not already_imported else "model_load_ms"):
            if not (self._use_bv_gan and _import_torch()):
                self._use_bv_gan = False
                return False
            self.device = self.device or torch.device(self._device_str)
            self.bv_gen = BVIterativeGenerator().to(self.device)
            self.bv_gen.eval()
            state = torch.load(self._bv_path, map_location=self.device,
                               weights_only=True)
            self.bv_gen.load_state_dict(state)
        return True

    # ── Public API ────────────────────────────────────────────────────────────

    def solve_file(self, path: str) -> Tuple[str, Optional[dict], float, NsFormula]:
        prof = _Prof(self.profile)
        with _Timer(prof, "parse_ms"):
            formula = parse_file(path)
        result, model, elapsed_ms = self._solve(formula, prof, formula_name=path)
        return result, model, elapsed_ms, formula

    def solve_string(
        self, smtlib_str: str
    ) -> Tuple[str, Optional[dict], float, NsFormula]:
        prof = _Prof(self.profile)
        with _Timer(prof, "parse_ms"):
            formula = parse_string(smtlib_str)
        result, model, elapsed_ms = self._solve(formula, prof)
        return result, model, elapsed_ms, formula

    def solve_formula(self, formula: NsFormula) -> Tuple[str, Optional[dict], float]:
        """Solve an already-parsed formula. For callers that need to
        inspect the parsed formula before deciding whether to run
        NeuroSym's own pipeline at all (e.g. skipping straight to an
        external-solver fallback for array-heavy formulas -- see
        ns_fallback.formula_uses_arrays) rather than parsing twice.
        parse_ms is 0 here: the caller already parsed it, outside this
        timer's reach -- time it on the caller's side if needed."""
        prof = _Prof(self.profile)
        return self._solve(formula, prof)

    # ── Internal dispatch ─────────────────────────────────────────────────────

    def _solve(
        self, formula: NsFormula, prof: _Prof, formula_name: Optional[str] = None
    ) -> Tuple[str, Optional[dict], float]:
        t0    = time.time()
        logic = formula.logic.upper()
        is_bv = logic in _BV_LOGICS

        deadline = t0 + self.timeout_ms / 1000.0

        if prof.enabled:
            with _Timer(prof, "formula_analysis_ms"):
                prof.set("formula_name", formula_name)
                prof.set("logic", logic)
                prof.set("declared_var_count", len(formula.variables))
                prof.set("assertion_count", len(formula.assertions))
                node_count, max_depth, op_counts = _analyze_ast(formula.assertions)
                prof.set("ast_node_count", node_count)
                prof.set("max_ast_depth", max_depth)
                prof.set("operator_counts", op_counts)
                bv_vars    = sum(1 for v in formula.variables.values() if isinstance(v.sort, BVSort))
                bool_vars  = sum(1 for v in formula.variables.values() if isinstance(v.sort, BoolSort))
                array_vars = sum(1 for v in formula.variables.values() if isinstance(v.sort, ArraySort))
                prof.set("bv_var_count", bv_vars)
                prof.set("bool_var_count", bool_vars)
                prof.set("array_var_count", array_vars)
                prof.set("total_bv_width", sum(
                    v.sort.width for v in formula.variables.values()
                    if isinstance(v.sort, BVSort)))

        # Array-touching formulas: go straight to bit-blast + MiniSat, skip
        # the GAN entirely. The GAN's own formula encoder (bv_encoder.py)
        # has no representation for array theory at all -- it was trained
        # on a fixed (variables, constraints) encoding with nothing to
        # capture select/store semantics -- so a forward pass here is not a
        # "fast guess that might miss", it is a guess the encoding can't
        # possibly ground truth against. Trying it first only adds the
        # GAN's own overhead (model already loaded, but still a real
        # forward pass) before falling through to the path that can
        # actually reason about the formula. ns_bitblaster's own array
        # encoding (select/store/as-const, read-over-write + weak
        # consistency axioms) already understands arrays and MiniSat
        # resolves the resulting CNF fast -- let it run immediately.
        uses_arrays = is_bv and formula_uses_arrays(formula)
        prof.set("uses_arrays", uses_arrays)

        # ── GAN fast path (only if a trained model was actually loaded) ────────
        # Tried running this concurrently with the symbolic path (a thread
        # each, racing for whichever finishes first) -- measured slower in
        # practice, not faster: Python's GIL means the two threads don't get
        # genuine parallelism for this CPU-bound mix (torch's own import
        # machinery in particular holds the GIL heavily), so a trivial
        # formula that solved symbolically alone in 0.16s took 10.75s with
        # the GAN thread running alongside it -- worse than the plain
        # sequential 2.8s baseline it was meant to improve on. True
        # parallelism would need separate processes (the loaded torch model
        # isn't trivially shareable across a process boundary) -- a bigger
        # rewrite, not attempted here. Back to sequential: GAN first, since
        # it's normally fast when it does hit, symbolic only once it either
        # doesn't apply (arrays) or comes back inconclusive.
        gan_eligible = not uses_arrays and not self.disable_gan and (
            (is_bv and self._use_bv_gan) or (not is_bv and self._use_lia_gan)
        )
        if uses_arrays:
            prof.set("gan_skipped_reason", "uses_arrays")
        elif self.disable_gan:
            prof.set("gan_skipped_reason", "disable_gan_flag")
        elif not ((is_bv and self._use_bv_gan) or (not is_bv and self._use_lia_gan)):
            prof.set("gan_skipped_reason", "no_model_configured")

        if gan_eligible:
            prof.set("gan_attempted", True)
            try:
                if is_bv:
                    gan_ready = self._ensure_bv_gan_loaded(prof)
                    r, m = self._bv_gan_path(formula, prof) if gan_ready else (RESULT_UNKNOWN, None)
                elif logic in _LIA_LOGICS or formula.variables:
                    gan_ready = self._ensure_lia_gan_loaded(prof)
                    r, m = self._lia_gan_path(formula, prof) if gan_ready else (RESULT_UNKNOWN, None)
                else:
                    r, m = RESULT_UNKNOWN, None

                if r == RESULT_SAT:
                    prof.set("gan_success", True)
                    prof.record("gan_ms",
                        prof.data["gan_encode_ms"] + prof.data["gan_inference_ms"]
                        + prof.data["gan_verify_ms"])
                    prof.set("final_result", r)
                    prof.record("total_ms", (time.time() - t0) * 1000)
                    self.last_profile = prof.data if prof.enabled else None
                    return r, m, (time.time() - t0) * 1000
            except Exception:
                pass
            prof.record("gan_ms",
                prof.data["gan_encode_ms"] + prof.data["gan_inference_ms"]
                + prof.data["gan_verify_ms"])

        # ── Symbolic fallback — own solvers only ──────────────────────────────
        prof.set("symbolic_fallback_used", True)
        if is_bv:
            # The CNF search gets its own minimum window (minisat_timeout_ms,
            # default 20 min) regardless of the shorter --timeout that
            # governs the GAN path above -- never *shorter* than the general
            # deadline (a larger --timeout still wins), only ever extended.
            cnf_deadline = max(deadline, t0 + self.minisat_timeout_ms / 1000.0)
            if cnf_deadline - time.time() <= 0:
                result, model = RESULT_UNKNOWN, None
            else:
                result, model = self._bv_solve(formula, cnf_deadline, prof)
        else:
            remaining = deadline - time.time()
            if remaining <= 0:
                result, model = RESULT_UNKNOWN, None
            else:
                result, model = self._lia_solve(formula, deadline, prof)

        prof.set("final_result", result)
        prof.record("total_ms", (time.time() - t0) * 1000)
        self.last_profile = prof.data if prof.enabled else None
        return result, model, (time.time() - t0) * 1000

    # ── GAN paths ─────────────────────────────────────────────────────────────

    def _lia_gan_path(self, formula: NsFormula, prof: _Prof) -> Tuple[str, Optional[dict]]:
        prof.set("gan_rounds_configured", getattr(self.lia_gen, "n_rounds", None))
        prof.set("gan_candidate_count", self.n_candidates)
        with _Timer(prof, "gan_encode_ms"):
            enc   = encode(formula)
            enc_t = torch.tensor(enc, dtype=torch.float32,
                                 device=self.device).unsqueeze(0)
        with _Timer(prof, "gan_inference_ms"):
            with torch.no_grad():
                candidates = self.lia_gen.sample(enc_t, n_samples=self.n_candidates)

        with _Timer(prof, "gan_verify_ms"):
            for i in range(self.n_candidates):
                vec        = candidates[0, i].cpu().numpy()
                assignment = decode_assignment(vec, formula)
                if evaluate(formula, assignment):
                    return RESULT_SAT, assignment
        return RESULT_UNKNOWN, None

    def _bv_gan_path(self, formula: NsFormula, prof: _Prof) -> Tuple[str, Optional[dict]]:
        prof.set("gan_rounds_configured", getattr(self.bv_gen, "n_rounds", None))
        prof.set("gan_candidate_count", self.n_candidates)
        with _Timer(prof, "gan_encode_ms"):
            enc   = bv_encode(formula)
            enc_t = torch.tensor(enc, dtype=torch.float32,
                                 device=self.device).unsqueeze(0)
        with _Timer(prof, "gan_inference_ms"):
            with torch.no_grad():
                candidates = self.bv_gen.sample(enc_t, n_samples=self.n_candidates)

        with _Timer(prof, "gan_verify_ms"):
            for i in range(self.n_candidates):
                vec        = candidates[0, i].cpu().numpy()
                assignment = bv_decode_assignment(vec, formula)
                if evaluate(formula, assignment):
                    return RESULT_SAT, assignment
        return RESULT_UNKNOWN, None

    # ── Symbolic solvers ──────────────────────────────────────────────────────

    def _bv_solve(self, formula: NsFormula,
                  deadline: float, prof: _Prof) -> Tuple[str, Optional[dict]]:
        try:
            # Previously unbounded: for a large enough formula, bit-blasting
            # itself (not the CNF search that follows it) could run past
            # the deadline with no way to detect that and hand off to the
            # external fallback -- measured directly, a 160,671-assignment
            # formula was still bit-blasting past the 20-minute MiniSat
            # floor, MiniSat never even reached. Passing the same deadline
            # through here lets blast() raise BlastTimeout (caught below,
            # same as any other blast failure) instead of running forever.
            with _Timer(prof, "bitblast_ms"):
                blast_stats = {} if prof.enabled else None
                clauses, n_vars, var_map = blast(
                    formula, deadline=deadline, stats_out=blast_stats)
        except Exception:
            return RESULT_UNKNOWN, None

        if prof.enabled:
            # Collected after blast() returns, before solve_cnf() -- read-only
            # over the already-produced clause list, does not reorder,
            # simplify, or otherwise touch what MiniSat/DPLL actually solves.
            lit_counts = [len(c) for c in clauses]
            prof.set("cnf_variable_count", n_vars)
            prof.set("cnf_clause_count", len(clauses))
            prof.set("cnf_literal_count", sum(lit_counts))
            prof.set("cnf_unit_clause_count", sum(1 for n in lit_counts if n == 1))
            prof.set("cnf_binary_clause_count", sum(1 for n in lit_counts if n == 2))
            prof.set("cnf_max_clause_length", max(lit_counts) if lit_counts else 0)
            prof.set("cnf_mean_clause_length",
                     (sum(lit_counts) / len(lit_counts)) if lit_counts else 0.0)
            prof.set("cnf_clause_var_ratio",
                     (len(clauses) / n_vars) if n_vars else 0.0)
            for k, v in (blast_stats or {}).items():
                prof.set(k if k.startswith("eq_") else f"const_{k}", v)

        if not clauses and not var_map:
            return RESULT_SAT, {}

        with _Timer(prof, "_cnf_ms"):
            sat_assign = solve_cnf(clauses, n_vars, deadline=deadline)
        cnf_ms = prof.data.pop("_cnf_ms", 0.0)
        if _last_cnf_backend in ("minisat", "minisat_native", "minisat_native_simp"):
            prof.set("minisat_used", True)
            prof.set("minisat_backend", _last_cnf_backend)
            prof.record("minisat_ms", cnf_ms)
        else:
            prof.set("dpll_used", True)
            prof.record("dpll_ms", cnf_ms)

        if sat_assign is None:
            # DPLL returned None — could be UNSAT or timeout
            remaining = deadline - time.time()
            if remaining <= 0:
                return RESULT_UNKNOWN, None
            return RESULT_UNSAT, None

        bv_assign = reconstruct(sat_assign, var_map)
        # Verify with our own evaluator
        with _Timer(prof, "verify_ms"):
            ok = evaluate(formula, bv_assign)
        if ok:
            return RESULT_SAT, bv_assign
        # Assignment found by DPLL but evaluator disagrees — encoding mismatch
        return RESULT_UNKNOWN, None

    def _lia_solve(self, formula: NsFormula,
                   deadline: float, prof: _Prof) -> Tuple[str, Optional[dict]]:
        try:
            with _Timer(prof, "lia_solver_ms"):
                result, assignment = solve_lia(formula, deadline)
        except Exception:
            return RESULT_UNKNOWN, None

        if result == RESULT_SAT and assignment is not None:
            with _Timer(prof, "verify_ms"):
                ok = evaluate(formula, assignment)
            if ok:
                return RESULT_SAT, assignment
            # LIA solver returned SAT but evaluator disagrees (e.g. non-linear
            # constraints skipped) — fall back to unknown
            return RESULT_UNKNOWN, None

        return result, assignment


# ── Output formatting ─────────────────────────────────────────────────────────

def format_output(
    result: str,
    model: Optional[dict] = None,
    variables: Optional[dict] = None,
) -> str:
    """`variables` is NsFormula.variables (name -> Var); when a name resolves
    to a BVSort, the value is emitted as a sized BV literal
    ("(_ BitVec N) #xHH...") rather than Int, matching what
    GANSATSolverImpl::parseModel (gansat_solver.cpp) parses back on the KLEE
    side. Without sort info (or for non-BV variables) it falls back to Int.

    `model` can be missing entries for declared variables that the DPLL/
    bit-blaster pipeline never had to pin down to a specific value (e.g. a
    "pure" literal eliminated during preprocessing, or one whose value
    never affected satisfiability either way) -- a real, observed gap: on
    one captured formula, 48 of 49 declared boolean bookkeeping variables
    were missing from `model`. Any value is sound for a variable the solve
    never needed to constrain, so every declared name gets a default (0 /
    #x00.../false-equivalent) here rather than being silently absent from
    the printed model -- a consumer (e.g. ESBMC's --neurosym backend, see
    neurosym_convt::get_bv()/l_get()) that looks a declared variable up
    directly should never get a miss on it."""
    lines = [result]
    if result == RESULT_SAT and model:
        complete_model = dict(model)
        if variables:
            for name in variables:
                complete_model.setdefault(name, 0)
        lines.append("(model")
        for name, val in sorted(complete_model.items()):
            var = variables.get(name) if variables else None
            if var is not None and isinstance(var.sort, BVSort):
                width = var.sort.width
                hex_digits = (width + 3) // 4
                hex_val = val & ((1 << width) - 1)
                lines.append(
                    f"  (define-fun {name} () (_ BitVec {width}) "
                    f"#x{hex_val:0{hex_digits}x})"
                )
            else:
                lines.append(f"  (define-fun {name} () Int {val})")
        lines.append(")")
    return "\n".join(lines)
