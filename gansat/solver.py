"""
GANSAT solver — unified entry point for QF_LIA and QF_BV.

Auto-dispatches based on formula logic:
  QF_LIA → IterativeGenerator   (integer linear arithmetic)
  QF_BV  → BVIterativeGenerator (bit-vector theory)
  Other  → Z3 fallback only

Strategy (both theories):
  1. Encode formula → feature vector
  2. GAN generates N candidates (fast, ~1ms)
  3. Z3 verifies each candidate (~0.1ms)
  4. First verified candidate → return sat immediately
  5. No candidate passes → Z3 full solve (complete fallback)
"""

import time
import torch
import numpy as np
import z3

from .parser    import parse_string, parse_file, ParsedFormula
from .encoder   import encode,    decode_assignment,    feature_dim
from .bv_encoder import bv_encode, bv_decode_assignment, bv_feature_dim
from .gan       import IterativeGenerator,   MAX_VARS,    NOISE_DIM
from .bv_gan    import BVIterativeGenerator, BV_NOISE_DIM

RESULT_SAT     = "sat"
RESULT_UNSAT   = "unsat"
RESULT_UNKNOWN = "unknown"

_BV_LOGICS  = {"QF_BV", "QF_ABV", "QF_AUFBV", "BV"}
_LIA_LOGICS = {"QF_LIA", "QF_NIA", "QF_LRA", "LIA"}


class GANSATSolver:
    def __init__(
        self,
        model_path:    str = None,
        bv_model_path: str = None,
        n_candidates:  int = 16,
        timeout_ms:    int = 20_000,
        device:        str = "cpu",
    ):
        self.n_candidates = n_candidates
        self.timeout_ms   = timeout_ms
        self.device       = torch.device(device)

        # QF_LIA generator
        self.lia_gen = IterativeGenerator().to(self.device)
        self.lia_gen.eval()
        if model_path:
            state = torch.load(model_path, map_location=self.device)
            self.lia_gen.load_state_dict(state)

        # QF_BV generator
        self.bv_gen = BVIterativeGenerator().to(self.device)
        self.bv_gen.eval()
        if bv_model_path:
            state = torch.load(bv_model_path, map_location=self.device)
            self.bv_gen.load_state_dict(state)

    # ── Public API ────────────────────────────────────────────────────────────

    def solve_file(self, path: str) -> tuple:
        return self._solve(parse_file(path))

    def solve_string(self, smtlib_str: str) -> tuple:
        return self._solve(parse_string(smtlib_str))

    # ── Internal dispatch ─────────────────────────────────────────────────────

    def _solve(self, formula: ParsedFormula) -> tuple:
        t0 = time.time()
        logic = formula.logic.upper()

        if logic in _BV_LOGICS:
            result, model = self._bv_fast_path(formula)
        elif logic in _LIA_LOGICS or formula.variables:
            result, model = self._lia_fast_path(formula)
        else:
            result, model = RESULT_UNKNOWN, None

        if result == RESULT_SAT:
            return result, model, (time.time() - t0) * 1000

        remaining_ms = self.timeout_ms - int((time.time() - t0) * 1000)
        result, model = self._z3_solve(formula, logic, max(remaining_ms, 1000))
        return result, model, (time.time() - t0) * 1000

    # ── QF_LIA fast path ──────────────────────────────────────────────────────

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

    # ── QF_BV fast path ───────────────────────────────────────────────────────

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

    # ── Z3 fallback ───────────────────────────────────────────────────────────

    def _z3_solve(self, formula: ParsedFormula, logic: str, timeout_ms: int) -> tuple:
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


# ── Assignment verification ───────────────────────────────────────────────────

def _verify_assignment(formula: ParsedFormula, assignment: dict, theory: str) -> bool:
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
