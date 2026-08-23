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
from .ns_ast       import NsFormula, BVSort, IntSort
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
from .ns_fallback   import formula_uses_arrays

def solve_cnf(clauses, n_vars, deadline=None):
    """CNF entry point for _bv_solve: MiniSat when it's installed (a
    mature, compiled SAT solver -- much faster search than a from-scratch
    Python DPLL on the exact same clauses our own bit-blaster produces),
    falling back to ns_dpll's own solver when it isn't. Same contract
    either way: {var: bool} on SAT, None on UNSAT or timeout."""
    if ns_minisat.available():
        return ns_minisat.solve_cnf(clauses, n_vars, deadline=deadline)
    return _dpll_solve_cnf(clauses, n_vars, deadline=deadline)


RESULT_SAT     = "sat"
RESULT_UNSAT   = "unsat"
RESULT_UNKNOWN = "unknown"

_BV_LOGICS  = {"QF_BV", "QF_ABV", "QF_AUFBV", "BV"}
_LIA_LOGICS = {"QF_LIA", "QF_NIA", "QF_LRA", "LIA"}


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

    def _ensure_lia_gan_loaded(self) -> bool:
        if self._lia_gan_loaded:
            return self.lia_gen is not None
        self._lia_gan_loaded = True
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

    def _ensure_bv_gan_loaded(self) -> bool:
        if self._bv_gan_loaded:
            return self.bv_gen is not None
        self._bv_gan_loaded = True
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
        formula = parse_file(path)
        result, model, elapsed_ms = self._solve(formula)
        return result, model, elapsed_ms, formula

    def solve_string(
        self, smtlib_str: str
    ) -> Tuple[str, Optional[dict], float, NsFormula]:
        formula = parse_string(smtlib_str)
        result, model, elapsed_ms = self._solve(formula)
        return result, model, elapsed_ms, formula

    def solve_formula(self, formula: NsFormula) -> Tuple[str, Optional[dict], float]:
        """Solve an already-parsed formula. For callers that need to
        inspect the parsed formula before deciding whether to run
        NeuroSym's own pipeline at all (e.g. skipping straight to an
        external-solver fallback for array-heavy formulas -- see
        ns_fallback.formula_uses_arrays) rather than parsing twice."""
        return self._solve(formula)

    # ── Internal dispatch ─────────────────────────────────────────────────────

    def _solve(self, formula: NsFormula) -> Tuple[str, Optional[dict], float]:
        t0    = time.time()
        logic = formula.logic.upper()
        is_bv = logic in _BV_LOGICS

        deadline = t0 + self.timeout_ms / 1000.0

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
        if not uses_arrays and (
            (is_bv and self._use_bv_gan) or (not is_bv and self._use_lia_gan)
        ):
            try:
                if is_bv:
                    gan_ready = self._ensure_bv_gan_loaded()
                    r, m = self._bv_gan_path(formula) if gan_ready else (RESULT_UNKNOWN, None)
                elif logic in _LIA_LOGICS or formula.variables:
                    gan_ready = self._ensure_lia_gan_loaded()
                    r, m = self._lia_gan_path(formula) if gan_ready else (RESULT_UNKNOWN, None)
                else:
                    r, m = RESULT_UNKNOWN, None

                if r == RESULT_SAT:
                    return r, m, (time.time() - t0) * 1000
            except Exception:
                pass

        # ── Symbolic fallback — own solvers only ──────────────────────────────
        if is_bv:
            # The CNF search gets its own minimum window (minisat_timeout_ms,
            # default 20 min) regardless of the shorter --timeout that
            # governs the GAN path above -- never *shorter* than the general
            # deadline (a larger --timeout still wins), only ever extended.
            cnf_deadline = max(deadline, t0 + self.minisat_timeout_ms / 1000.0)
            if cnf_deadline - time.time() <= 0:
                return RESULT_UNKNOWN, None, (time.time() - t0) * 1000
            result, model = self._bv_solve(formula, cnf_deadline)
        else:
            remaining = deadline - time.time()
            if remaining <= 0:
                return RESULT_UNKNOWN, None, (time.time() - t0) * 1000
            result, model = self._lia_solve(formula, deadline)

        return result, model, (time.time() - t0) * 1000

    # ── GAN paths ─────────────────────────────────────────────────────────────

    def _lia_gan_path(self, formula: NsFormula) -> Tuple[str, Optional[dict]]:
        enc   = encode(formula)
        enc_t = torch.tensor(enc, dtype=torch.float32,
                             device=self.device).unsqueeze(0)
        with torch.no_grad():
            candidates = self.lia_gen.sample(enc_t, n_samples=self.n_candidates)

        for i in range(self.n_candidates):
            vec        = candidates[0, i].cpu().numpy()
            assignment = decode_assignment(vec, formula)
            if evaluate(formula, assignment):
                return RESULT_SAT, assignment
        return RESULT_UNKNOWN, None

    def _bv_gan_path(self, formula: NsFormula) -> Tuple[str, Optional[dict]]:
        enc   = bv_encode(formula)
        enc_t = torch.tensor(enc, dtype=torch.float32,
                             device=self.device).unsqueeze(0)
        with torch.no_grad():
            candidates = self.bv_gen.sample(enc_t, n_samples=self.n_candidates)

        for i in range(self.n_candidates):
            vec        = candidates[0, i].cpu().numpy()
            assignment = bv_decode_assignment(vec, formula)
            if evaluate(formula, assignment):
                return RESULT_SAT, assignment
        return RESULT_UNKNOWN, None

    # ── Symbolic solvers ──────────────────────────────────────────────────────

    def _bv_solve(self, formula: NsFormula,
                  deadline: float) -> Tuple[str, Optional[dict]]:
        try:
            # Previously unbounded: for a large enough formula, bit-blasting
            # itself (not the CNF search that follows it) could run past
            # the deadline with no way to detect that and hand off to the
            # external fallback -- measured directly, a 160,671-assignment
            # formula was still bit-blasting past the 20-minute MiniSat
            # floor, MiniSat never even reached. Passing the same deadline
            # through here lets blast() raise BlastTimeout (caught below,
            # same as any other blast failure) instead of running forever.
            clauses, n_vars, var_map = blast(formula, deadline=deadline)
        except Exception:
            return RESULT_UNKNOWN, None

        if not clauses and not var_map:
            return RESULT_SAT, {}

        sat_assign = solve_cnf(clauses, n_vars, deadline=deadline)
        if sat_assign is None:
            # DPLL returned None — could be UNSAT or timeout
            remaining = deadline - time.time()
            if remaining <= 0:
                return RESULT_UNKNOWN, None
            return RESULT_UNSAT, None

        bv_assign = reconstruct(sat_assign, var_map)
        # Verify with our own evaluator
        if evaluate(formula, bv_assign):
            return RESULT_SAT, bv_assign
        # Assignment found by DPLL but evaluator disagrees — encoding mismatch
        return RESULT_UNKNOWN, None

    def _lia_solve(self, formula: NsFormula,
                   deadline: float) -> Tuple[str, Optional[dict]]:
        try:
            result, assignment = solve_lia(formula, deadline)
        except Exception:
            return RESULT_UNKNOWN, None

        if result == RESULT_SAT and assignment is not None:
            if evaluate(formula, assignment):
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
    side. Without sort info (or for non-BV variables) it falls back to Int."""
    lines = [result]
    if result == RESULT_SAT and model:
        lines.append("(model")
        for name, val in sorted(model.items()):
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
