"""
GANSAT solver — Neural-Symbolic Portfolio Solver.

Strategy:
  1. GAN fast path  (~5ms)  — predicts satisfying assignment directly
  2. If GAN misses  → race Z3 vs Bitwuzla in parallel threads
  3. Whoever answers first wins; other thread is cancelled via timeout

Theories:
  QF_LIA → GAN (LIA) + Z3 parallel
  QF_BV  → GAN (BV)  + Z3 + Bitwuzla parallel   ← portfolio
  Other  → Z3 only
"""

import time
import threading
import queue
import torch
import numpy as np
import z3

from .parser     import parse_string, parse_file, ParsedFormula
from .encoder    import encode,    decode_assignment,    feature_dim
from .bv_encoder import bv_encode, bv_decode_assignment, bv_feature_dim
from .gan        import IterativeGenerator,   MAX_VARS,    NOISE_DIM
from .bv_gan     import BVIterativeGenerator, BV_NOISE_DIM

RESULT_SAT     = "sat"
RESULT_UNSAT   = "unsat"
RESULT_UNKNOWN = "unknown"

_BV_LOGICS  = {"QF_BV", "QF_ABV", "QF_AUFBV", "BV"}
_LIA_LOGICS = {"QF_LIA", "QF_NIA", "QF_LRA", "LIA"}

# Try importing Bitwuzla — optional, degrades gracefully if missing
try:
    import bitwuzla as _bwz
    _BITWUZLA_AVAILABLE = True
except ImportError:
    _BITWUZLA_AVAILABLE = False


class GANSATSolver:
    def __init__(
        self,
        model_path:     str  = None,
        bv_model_path:  str  = None,
        lia_model_path: str  = None,
        n_candidates:   int  = 8,
        timeout_ms:     int  = 20_000,
        device:         str  = "cpu",
        portfolio:      bool = True,
    ):
        self.n_candidates = n_candidates
        self.timeout_ms   = timeout_ms
        self.device       = torch.device(device)
        self.portfolio    = portfolio and _BITWUZLA_AVAILABLE

        # QF_LIA generator
        self.lia_gen = IterativeGenerator().to(self.device)
        self.lia_gen.eval()
        lia_path = lia_model_path or model_path
        if lia_path:
            state = torch.load(lia_path, map_location=self.device, weights_only=True)
            self.lia_gen.load_state_dict(state)

        # QF_BV generator
        self.bv_gen = BVIterativeGenerator().to(self.device)
        self.bv_gen.eval()
        if bv_model_path:
            state = torch.load(bv_model_path, map_location=self.device, weights_only=True)
            self.bv_gen.load_state_dict(state)

    # ── Public API ────────────────────────────────────────────────────────────

    def solve_file(self, path: str) -> tuple:
        formula = parse_file(path)
        return self._solve(formula)

    def solve_string(self, smtlib_str: str) -> tuple:
        formula = parse_string(smtlib_str)
        return self._solve(formula)

    # ── Internal dispatch ─────────────────────────────────────────────────────

    def _solve(self, formula: ParsedFormula) -> tuple:
        t0    = time.time()
        logic = formula.logic.upper()

        # ── Strategy ──────────────────────────────────────────────────────────
        # Z3 is NOT thread-safe across threads. So:
        #   • Bitwuzla (C++ library, own memory) → background thread from t=0
        #   • GAN verification (uses Z3) + Z3 fallback → main thread only
        # This gives Bitwuzla a head-start while the GAN runs, eliminating
        # the sequential penalty without any Z3 thread-safety issues.

        bwz_q = queue.Queue()

        def _run_bitwuzla():
            try:
                r, m = self._bitwuzla_solve(formula.source, self.timeout_ms)
                bwz_q.put((r, m))
            except Exception:
                bwz_q.put((RESULT_UNKNOWN, None))

        # Start Bitwuzla immediately in background (BV only)
        if self.portfolio and logic in _BV_LOGICS:
            threading.Thread(target=_run_bitwuzla, daemon=True).start()

        # ── GAN fast path (main thread — Z3 verification here is safe) ───────
        gan_result, gan_model = RESULT_UNKNOWN, None
        try:
            if logic in _BV_LOGICS:
                gan_result, gan_model = self._bv_fast_path(formula)
            elif logic in _LIA_LOGICS or formula.variables:
                gan_result, gan_model = self._lia_fast_path(formula)
        except Exception:
            pass

        if gan_result == RESULT_SAT:
            return gan_result, gan_model, (time.time() - t0) * 1000

        # ── GAN missed: Z3 on main thread + check if Bitwuzla already done ───
        elapsed_ms   = int((time.time() - t0) * 1000)
        remaining_ms = max(self.timeout_ms - elapsed_ms, 1000)

        # Check if Bitwuzla already finished while GAN was running
        if self.portfolio and logic in _BV_LOGICS:
            try:
                r, m = bwz_q.get_nowait()
                if r in (RESULT_SAT, RESULT_UNSAT):
                    return r, m, (time.time() - t0) * 1000
            except queue.Empty:
                pass

        # Bitwuzla intermediate wait: give Bitwuzla up to 1.5s before committing
        # Z3 to the full remaining timeout. This catches the common case where
        # Bitwuzla answers in ~500-1500ms while Z3 would waste 4-5s and miss it.
        if self.portfolio and logic in _BV_LOGICS:
            bwz_pre = min(1.5, remaining_ms / 1000.0 * 0.3)
            try:
                r, m = bwz_q.get(timeout=bwz_pre)
                if r in (RESULT_SAT, RESULT_UNSAT):
                    return r, m, (time.time() - t0) * 1000
            except queue.Empty:
                pass
            elapsed_ms   = int((time.time() - t0) * 1000)
            remaining_ms = max(self.timeout_ms - elapsed_ms, 500)

        # Run Z3 on main thread
        z3_result, z3_model = self._z3_solve(formula, logic, remaining_ms)
        elapsed_ms = int((time.time() - t0) * 1000)

        if z3_result in (RESULT_SAT, RESULT_UNSAT):
            return z3_result, z3_model, elapsed_ms

        # Both GAN and Z3 failed — wait for Bitwuzla if BV
        if self.portfolio and logic in _BV_LOGICS:
            bwz_wait = max(self.timeout_ms / 1000.0 - (time.time() - t0) + 1.0, 1.0)
            try:
                r, m = bwz_q.get(timeout=bwz_wait)
                return r, m, (time.time() - t0) * 1000
            except queue.Empty:
                pass

        return RESULT_UNKNOWN, None, (time.time() - t0) * 1000

    # ── GAN fast paths ────────────────────────────────────────────────────────

    def _lia_fast_path(self, formula: ParsedFormula) -> tuple:
        enc   = encode(formula)
        enc_t = torch.tensor(enc, dtype=torch.float32, device=self.device).unsqueeze(0)
        with torch.no_grad():
            candidates = self.lia_gen.sample(enc_t, n_samples=self.n_candidates)
        for i in range(self.n_candidates):
            vec        = candidates[0, i].cpu().numpy()
            assignment = decode_assignment(vec, formula)
            if _verify_assignment(formula, assignment, theory="lia"):
                return RESULT_SAT, assignment
        return RESULT_UNKNOWN, None

    def _bv_fast_path(self, formula: ParsedFormula) -> tuple:
        enc   = bv_encode(formula)
        enc_t = torch.tensor(enc, dtype=torch.float32, device=self.device).unsqueeze(0)
        with torch.no_grad():
            candidates = self.bv_gen.sample(enc_t, n_samples=self.n_candidates)
        for i in range(self.n_candidates):
            vec        = candidates[0, i].cpu().numpy()
            assignment = bv_decode_assignment(vec, formula)
            if _verify_assignment(formula, assignment, theory="bv"):
                return RESULT_SAT, assignment
        return RESULT_UNKNOWN, None

    # ── Portfolio: Z3 vs Bitwuzla race ────────────────────────────────────────

    def _portfolio_solve(self, formula: ParsedFormula, logic: str,
                         timeout_ms: int) -> tuple:
        """Race Z3 and Bitwuzla — return whichever answers first."""
        result_q  = queue.Queue()
        timeout_s = timeout_ms / 1000.0

        def _run_z3():
            try:
                r, m = self._z3_solve(formula, logic, timeout_ms)
                result_q.put(("z3", r, m))
            except Exception:
                result_q.put(("z3", RESULT_UNKNOWN, None))

        def _run_bitwuzla():
            try:
                r, m = self._bitwuzla_solve(formula.source, timeout_ms)
                result_q.put(("bitwuzla", r, m))
            except Exception:
                result_q.put(("bitwuzla", RESULT_UNKNOWN, None))

        t_z3  = threading.Thread(target=_run_z3,       daemon=True)
        t_bwz = threading.Thread(target=_run_bitwuzla, daemon=True)
        t_z3.start()
        t_bwz.start()

        deadline = time.time() + timeout_s + 2.0
        best = (RESULT_UNKNOWN, None)
        answered = 0

        while answered < 2 and time.time() < deadline:
            try:
                who, r, m = result_q.get(timeout=0.1)
                answered += 1
                if r in (RESULT_SAT, RESULT_UNSAT):
                    return r, m
                if r != RESULT_UNKNOWN:
                    best = (r, m)
            except queue.Empty:
                continue

        return best

    # ── Z3 solver ─────────────────────────────────────────────────────────────

    def _z3_solve(self, formula: ParsedFormula, logic: str,
                  timeout_ms: int) -> tuple:
        solver = z3.Solver()
        solver.set("timeout", timeout_ms)
        for assertion in formula.assertions:
            solver.add(assertion)
        result = solver.check()
        if result == z3.sat:
            model      = solver.model()
            assignment = _extract_model(model, logic)
            return RESULT_SAT, assignment
        elif result == z3.unsat:
            return RESULT_UNSAT, None
        return RESULT_UNKNOWN, None

    # ── Bitwuzla solver ───────────────────────────────────────────────────────

    def _bitwuzla_solve(self, smtlib_str: str, timeout_ms: int) -> tuple:
        """Solve using Bitwuzla via its SMT-LIB 2 parser."""
        tm   = _bwz.TermManager()
        opts = _bwz.Options()
        opts.set(_bwz.Option.PRODUCE_MODELS, True)
        opts.set(_bwz.Option.TIME_LIMIT_PER, timeout_ms)
        opts.set(_bwz.Option.VERBOSITY,      0)
        try:
            parser = _bwz.Parser(tm, opts)
            parser.parse(smtlib_str, parse_only=True, parse_file=False)
            bwz    = parser.bitwuzla()
            result = bwz.check_sat()
            s = str(result)
            if s == "sat":   return RESULT_SAT,   {}
            if s == "unsat": return RESULT_UNSAT, None
            return RESULT_UNKNOWN, None
        except Exception:
            return RESULT_UNKNOWN, None


# ── Assignment verification ───────────────────────────────────────────────────

def _verify_assignment(formula: ParsedFormula, assignment: dict,
                        theory: str) -> bool:
    if not assignment:
        return False
    if theory == "bv":
        subs = [
            (formula.variables[name],
             z3.BitVecVal(val, formula.variables[name].sort().size()))
            for name, val in assignment.items()
            if name in formula.variables and z3.is_bv_sort(formula.variables[name].sort())
        ]
    else:
        subs = [
            (formula.variables[name], z3.IntVal(val))
            for name, val in assignment.items()
            if name in formula.variables
        ]
    if not subs:
        return False
    for assertion in formula.assertions:
        simplified = z3.simplify(z3.substitute(assertion, subs))
        if z3.is_false(simplified):
            return False
        if not z3.is_true(simplified):
            return False
    return True


def _extract_model(model: z3.ModelRef, logic: str) -> dict:
    assignment = {}
    for d in model.decls():
        val = model[d]
        if val is None:
            continue
        if z3.is_int_value(val):
            assignment[str(d)] = val.as_long()
        elif z3.is_bv_value(val):
            assignment[str(d)] = val.as_long()
    return assignment


# ── Output formatting ────────────────────────────────────────────────────────

def format_output(result: str, model: dict = None) -> str:
    lines = [result]
    if result == RESULT_SAT and model:
        lines.append("(model")
        for name, val in sorted(model.items()):
            lines.append(f"  (define-fun {name} () Int {val})")
        lines.append(")")
    return "\n".join(lines)
