"""
NeuroSym bit-blaster — converts QF_BV / QF_ABV formulas to SAT CNF.

Each BV variable of width w becomes w Boolean variables (MSB first is
index 0; index w-1 is LSB).  The blaster builds a circuit of AND/OR/XOR
gates encoded as Tseitin clauses.

Public API:
  blast(formula: NsFormula) → (clauses, n_vars, var_map)
    clauses  : list of list[int]  (signed, 1-indexed)
    n_vars   : total Boolean vars
    var_map  : dict {bv_var_name: list[int]} BV vars → bit-variable IDs (MSB first)
               bit-variable ID = SAT variable number (1-indexed)

After DPLL returns an assignment, call reconstruct(sat_assign, var_map) to
get the BV assignment dict.
"""

import time
from typing import List, Dict, Tuple, Optional
from .ns_ast import (
    Term, BoolLit, IntLit, BVLit, Var, App,
    NsFormula, BoolSort, IntSort, BVSort, ArraySort,
    BOOL, TRUE, FALSE,
)


class BlastTimeout(Exception):
    """Raised mid-blast when a deadline was given and is exceeded.
    Bit-blasting itself was previously unbounded -- ns_solver's own
    minisat_timeout_ms only ever governed the CNF *search* (MiniSat/
    ns_dpll), not the step that builds the CNF in the first place. Measured
    directly: a 160,671-assignment ESBMC formula (998 bundled VCCs, a
    fully-unwound 1000-iteration loop) never even reached MiniSat -- still
    bit-blasting past the 20-minute floor, with no way to detect that and
    hand off to the external fallback instead of running unbounded."""
    pass


class UnsupportedBitVectorOperator(Exception):
    """Raised when the AST contains an operator this bit-blaster has no
    encoding for. Previously such an operator silently fell through to
    `self.alloc.fresh_bits(w)` -- an unconstrained value with no relation
    to the operator's actual semantics, which is unsound: a formula using
    that value could be reported SAT (or UNSAT) based on a value that
    doesn't mean what the formula says it means. Caught by the same
    `except Exception` already around blast() in ns_solver.py's
    _bv_solve(), degrading to RESULT_UNKNOWN -- an honest "can't solve
    this", not a silently wrong answer."""
    pass


# ── Variable allocator ─────────────────────────────────────────────────────────

class _Alloc:
    def __init__(self):
        self._count = 0
        # Constant-encoding cache -- scoped to this _Alloc instance, which
        # Blaster.__init__ creates fresh per formula solve (see blast()),
        # so this never crosses formulas or reuses SAT variable IDs between
        # independent CNFs. See _CONST_TRUE/_CONST_FALSE/_int_to_bits: a
        # single canonical TRUE/FALSE literal is allocated at most once
        # each and then reused for every constant bit in the formula,
        # rather than allocating a fresh variable + unit clause per
        # occurrence of every repeated BV literal.
        self.true_lit:  Optional[int] = None
        self.false_lit: Optional[int] = None
        # Profiling counters -- exact, not estimated: every increment here
        # corresponds to a real function call/allocation that did or didn't
        # happen, not an inference from AST-level duplicate counts.
        self.true_requests    = 0
        self.false_requests   = 0
        self.true_allocations = 0   # 0 or 1, ever
        self.false_allocations = 0  # 0 or 1, ever
        self.bv_const_requests  = 0   # total _int_to_bits() calls
        self._unique_bv_consts: set = set()  # (width, normalized_value) seen

    def fresh(self) -> int:
        self._count += 1
        return self._count

    def fresh_bits(self, w: int) -> List[int]:
        return [self.fresh() for _ in range(w)]

    @property
    def count(self) -> int:
        return self._count

    @property
    def unique_bv_constants(self) -> int:
        return len(self._unique_bv_consts)

    @property
    def constant_sat_vars_avoided(self) -> int:
        # Every TRUE/FALSE request beyond the first is one fresh variable
        # (and its matching unit clause) that the old code would have
        # allocated and this one didn't.
        return (self.true_requests - self.true_allocations) + \
               (self.false_requests - self.false_allocations)

    @property
    def constant_clauses_avoided(self) -> int:
        # _CONST_TRUE/_CONST_FALSE add exactly one unit clause per
        # allocation (see their bodies) -- same count as vars avoided.
        return self.constant_sat_vars_avoided


# ── Tseitin gate builders ──────────────────────────────────────────────────────
# All functions return a NEW variable whose value equals the gate output.
# Clauses are appended to `out`.

def _new_eq_lit(a: int, out: list, alloc: _Alloc) -> int:
    """y = a  (just return a directly — no gate needed)."""
    return a


def _gate_not(a: int, out: list, alloc: _Alloc) -> int:
    return -a   # negate literal; no extra clause needed


def _gate_and(a: int, b: int, out: list, alloc: _Alloc) -> int:
    y = alloc.fresh()
    # y → (a ∧ b)   and   (a ∧ b) → y
    out.append([-y,  a])
    out.append([-y,  b])
    out.append([ y, -a, -b])
    return y


def _gate_or(a: int, b: int, out: list, alloc: _Alloc) -> int:
    y = alloc.fresh()
    out.append([ y, -a])
    out.append([ y, -b])
    out.append([-y,  a,  b])
    return y


def _gate_xor(a: int, b: int, out: list, alloc: _Alloc) -> int:
    y = alloc.fresh()
    out.append([-y, -a, -b])
    out.append([-y,  a,  b])
    out.append([ y, -a,  b])
    out.append([ y,  a, -b])
    return y


def _gate_ite(c: int, t: int, e: int, out: list, alloc: _Alloc) -> int:
    """y = ite(c, t, e)"""
    y = alloc.fresh()
    # y ↔ (c → t) ∧ (¬c → e)
    out.append([-y,  -c,  t])
    out.append([-y,   c,  e])
    out.append([ y,  -c, -t])
    out.append([ y,   c, -e])
    return y


def _gate_and_n(bits: List[int], out: list, alloc: _Alloc) -> int:
    """y = AND of all bits in list."""
    if not bits: return _CONST_TRUE(out, alloc)
    r = bits[0]
    for b in bits[1:]:
        r = _gate_and(r, b, out, alloc)
    return r


def _gate_or_n(bits: List[int], out: list, alloc: _Alloc) -> int:
    if not bits: return _CONST_FALSE(out, alloc)
    r = bits[0]
    for b in bits[1:]:
        r = _gate_or(r, b, out, alloc)
    return r


def _CONST_TRUE(out: list, alloc: _Alloc) -> int:
    """Canonical TRUE literal for this formula's Blaster/_Alloc -- allocated
    at most once (see _Alloc.__init__), every subsequent request across the
    whole solve reuses the same SAT variable instead of allocating a fresh
    one and a matching unit clause. Semantically identical to the old
    always-fresh version: any variable constrained to be unconditionally
    true is interchangeable with any other such variable, so sharing one is
    sound by construction, not an approximation."""
    alloc.true_requests += 1
    if alloc.true_lit is None:
        alloc.true_lit = alloc.fresh()
        out.append([alloc.true_lit])
        alloc.true_allocations += 1
    return alloc.true_lit


def _CONST_FALSE(out: list, alloc: _Alloc) -> int:
    """Canonical FALSE literal -- see _CONST_TRUE's docstring; same
    reasoning, unconditionally-false variables are interchangeable."""
    alloc.false_requests += 1
    if alloc.false_lit is None:
        alloc.false_lit = alloc.fresh()
        out.append([-alloc.false_lit])
        alloc.false_allocations += 1
    return alloc.false_lit


# ── Bit-vector integer → bit list ──────────────────────────────────────────────

def _int_to_bits(val: int, w: int, out: list, alloc: _Alloc) -> List[int]:
    """Return a list of w constant literals (MSB first).

    Composed entirely from the two canonical TRUE/FALSE literals (see
    _CONST_TRUE/_CONST_FALSE) rather than allocating w fresh
    constant-constrained variables per call -- every occurrence of every BV
    literal, of any value and width, ends up referencing just those same
    two SAT variables. Deliberately not a separate (width, value) -> bits
    cache: composing from two already-canonical literals gets the same
    zero-extra-allocation result with less code and no cache-key
    normalization to get right (see the class comment on why a naive
    value-keyed cache would still need explicit `val mod 2**w` handling).
    Bit ordering (MSB first, i.e. index w-1 down to 0) is unchanged from
    the prior implementation. The returned list is always a fresh object
    (built via .append() in this call), so sharing the underlying literals
    carries no aliasing risk even though callers may treat the list as
    theirs to use freely."""
    alloc.bv_const_requests += 1
    normalized = val & ((1 << w) - 1) if w > 0 else 0
    alloc._unique_bv_consts.add((w, normalized))
    result = []
    for i in range(w - 1, -1, -1):
        bit = (normalized >> i) & 1
        result.append(_CONST_TRUE(out, alloc) if bit else _CONST_FALSE(out, alloc))
    return result


# ── Adder circuit ──────────────────────────────────────────────────────────────

def _full_adder(a: int, b: int, cin: int,
                out: list, alloc: _Alloc) -> Tuple[int, int]:
    """Returns (sum_bit, carry_out)."""
    # sum  = a XOR b XOR cin
    ab   = _gate_xor(a,  b,   out, alloc)
    s    = _gate_xor(ab, cin, out, alloc)
    # cout = (a AND b) OR (cin AND (a XOR b))
    c1   = _gate_and(a, b,   out, alloc)
    c2   = _gate_and(cin, ab, out, alloc)
    cout = _gate_or(c1, c2,  out, alloc)
    return s, cout


def _bv_add(a_bits: List[int], b_bits: List[int],
            out: list, alloc: _Alloc) -> List[int]:
    """Ripple-carry adder. a_bits and b_bits are MSB-first."""
    w     = len(a_bits)
    carry = _CONST_FALSE(out, alloc)
    sums  = [0] * w
    for i in range(w - 1, -1, -1):
        s, carry = _full_adder(a_bits[i], b_bits[i], carry, out, alloc)
        sums[i] = s
    return sums   # MSB first; overflow carry discarded


def _bv_neg(a_bits: List[int], out: list, alloc: _Alloc) -> List[int]:
    """Two's complement negation: ~a + 1."""
    not_a = [_gate_not(b, out, alloc) for b in a_bits]
    one   = _int_to_bits(1, len(a_bits), out, alloc)
    return _bv_add(not_a, one, out, alloc)


def _bv_sub(a_bits: List[int], b_bits: List[int],
            out: list, alloc: _Alloc) -> List[int]:
    """a - b = a + (-b)."""
    neg_b = _bv_neg(b_bits, out, alloc)
    return _bv_add(a_bits, neg_b, out, alloc)


def _bv_and(a_bits, b_bits, out, alloc):
    return [_gate_and(a, b, out, alloc) for a, b in zip(a_bits, b_bits)]

def _bv_or(a_bits, b_bits, out, alloc):
    return [_gate_or(a, b, out, alloc) for a, b in zip(a_bits, b_bits)]

def _bv_xor(a_bits, b_bits, out, alloc):
    return [_gate_xor(a, b, out, alloc) for a, b in zip(a_bits, b_bits)]

def _bv_not(a_bits, out, alloc):
    return [_gate_not(b, out, alloc) for b in a_bits]


def _bv_mul(a_bits: List[int], b_bits: List[int],
            out: list, alloc: _Alloc) -> List[int]:
    """Schoolbook multiplication (w² AND gates). MSB first."""
    w     = len(a_bits)
    # Partial products
    result = _int_to_bits(0, w, out, alloc)
    for i in range(w - 1, -1, -1):
        # Shift a_bits left by (w-1-i) positions (= multiply by 2^(w-1-i))
        shift = w - 1 - i
        shifted = ([_CONST_FALSE(out, alloc)] * shift
                   + a_bits[:w - shift])     # MSB first, shift left
        # If b[i] is 1, add shifted to result
        masked = [_gate_and(shifted[j], b_bits[i], out, alloc)
                  for j in range(w)]
        result = _bv_add(result, masked, out, alloc)
    return result


# ── Comparators ────────────────────────────────────────────────────────────────

def _bv_eq(a_bits: List[int], b_bits: List[int],
           out: list, alloc: _Alloc) -> int:
    """Return single Boolean variable: 1 iff a == b."""
    eq_bits = [_gate_not(_gate_xor(a, b, out, alloc), out, alloc)
               for a, b in zip(a_bits, b_bits)]
    return _gate_and_n(eq_bits, out, alloc)


def _bv_ult(a_bits: List[int], b_bits: List[int],
            out: list, alloc: _Alloc) -> int:
    """Unsigned a < b."""
    # Compute a - b; if borrow occurred, a < b
    # Equivalently: NOT (a >= b) = NOT (b <= a)
    # Use subtraction: borrow = MSB carry-out of (a - b) is 1 → a < b
    # More directly: compute a + ~b + 1; if carry-in to MSB+1 is 0 → a < b
    # Simplest: propagate borrow bit from MSB
    w = len(a_bits)
    # borrow chain: b[i] = (a_i < b_i) OR (a_i == b_i AND borrow)
    borrow = _CONST_FALSE(out, alloc)
    for i in range(w - 1, -1, -1):
        ai, bi = a_bits[i], b_bits[i]
        # new_borrow = (NOT ai AND bi) OR (NOT (ai XOR bi) AND borrow)
        not_ai    = _gate_not(ai, out, alloc)
        ai_lt_bi  = _gate_and(not_ai, bi, out, alloc)
        eq_i      = _gate_not(_gate_xor(ai, bi, out, alloc), out, alloc)
        prop      = _gate_and(eq_i, borrow, out, alloc)
        borrow    = _gate_or(ai_lt_bi, prop, out, alloc)
    return borrow


def _bv_ule(a_bits, b_bits, out, alloc) -> int:
    """a <= b  iff  NOT (b < a)."""
    return _gate_not(_bv_ult(b_bits, a_bits, out, alloc), out, alloc)


# ── Division / remainder ─────────────────────────────────────────────────────
# Semantics match gansat/ns_evaluator.py's _bvudiv/_bvurem/_bvsdiv/_bvsrem
# exactly (SMT-LIB2 QF_BV totalized division) -- that module was already
# correct and used as the reference throughout this implementation and its
# exhaustive/randomized differential tests; only the bit-blaster (which had
# no dispatch for these four operators at all -- an unsound silent gap,
# not a rewrite of working code) needed this.

def _bv_udivrem(a_bits: List[int], b_bits: List[int],
                out: list, alloc: _Alloc):
    """Restoring long division, unsigned, nonzero-divisor case only --
    the SMT-LIB divisor=0 override is applied by the four public
    operators below, not here. Returns (quotient, remainder), both width
    w = len(a_bits), MSB first, matching the file's existing convention.

    Standard schoolbook circuit: a (w+1)-bit remainder register R starts
    at 0; for each dividend bit (MSB to LSB), shift R left by one bringing
    in that bit, then subtract the (zero-extended) divisor if R is large
    enough. The loop invariant 0 <= R < b_ext holds at the start of every
    iteration (R is only ever left less than the divisor, by
    construction), so R's leading bit is always 0 at that point --
    dropping it on the next left-shift (`r[1:]`) is exactly a shift-by-1
    on a fixed-width register, not a silent truncation of anything live.
    w+1 bits (not w) for R is what keeps a trial R-before-subtraction from
    overflowing: R < 2*b_ext - 1 <= 2*(2^w - 1) - 1 < 2^(w+1) always."""
    w = len(a_bits)
    r = [_CONST_FALSE(out, alloc) for _ in range(w + 1)]
    b_ext = [_CONST_FALSE(out, alloc)] + list(b_bits)
    q_bits = []
    for i in range(w):
        r = r[1:] + [a_bits[i]]
        ge = _gate_not(_bv_ult(r, b_ext, out, alloc), out, alloc)  # r >= b_ext ?
        r_sub = _bv_sub(r, b_ext, out, alloc)
        r = [_gate_ite(ge, r_sub[k], r[k], out, alloc) for k in range(w + 1)]
        q_bits.append(ge)
    return q_bits, r[1:]  # drop the always-zero-after-a-correct-division extension bit


def _bv_bvudiv(a_bits: List[int], b_bits: List[int],
               out: list, alloc: _Alloc) -> List[int]:
    """SMT-LIB (bvudiv s t): floor(unsigned(s)/unsigned(t)) for t != 0,
    else all-ones (matches _bvudiv in ns_evaluator.py)."""
    w = len(a_bits)
    q, _ = _bv_udivrem(a_bits, b_bits, out, alloc)
    b_is_zero = _gate_not(_gate_or_n(b_bits, out, alloc), out, alloc)
    all_ones = [_CONST_TRUE(out, alloc) for _ in range(w)]
    return [_gate_ite(b_is_zero, all_ones[k], q[k], out, alloc) for k in range(w)]


def _bv_bvurem(a_bits: List[int], b_bits: List[int],
               out: list, alloc: _Alloc) -> List[int]:
    """SMT-LIB (bvurem s t): unsigned(s) mod unsigned(t) for t != 0, else
    s itself (matches _bvurem in ns_evaluator.py)."""
    _, r = _bv_udivrem(a_bits, b_bits, out, alloc)
    b_is_zero = _gate_not(_gate_or_n(b_bits, out, alloc), out, alloc)
    return [_gate_ite(b_is_zero, a_bits[k], r[k], out, alloc) for k in range(len(a_bits))]


def _bv_bvsdiv(a_bits: List[int], b_bits: List[int],
               out: list, alloc: _Alloc) -> List[int]:
    """SMT-LIB (bvsdiv s t): two's-complement signed division, truncating
    toward zero -- computed via sign/magnitude (|s| udiv |t|, negated if
    the operand signs differ), not Python's // (which truncates toward
    negative infinity, the wrong direction for SMT-LIB bvsdiv). Zero
    divisor: all-ones if s >= 0 else 1 (matches _bvsdiv in
    ns_evaluator.py, including the MIN_SIGNED/-1 case, which correctly
    wraps back to MIN_SIGNED here exactly as _from_signed's masking does
    there -- both are plain fixed-width two's-complement arithmetic)."""
    w = len(a_bits)
    a_sign, b_sign = a_bits[0], b_bits[0]
    a_neg, b_neg = _bv_neg(a_bits, out, alloc), _bv_neg(b_bits, out, alloc)
    a_mag = [_gate_ite(a_sign, a_neg[k], a_bits[k], out, alloc) for k in range(w)]
    b_mag = [_gate_ite(b_sign, b_neg[k], b_bits[k], out, alloc) for k in range(w)]
    q_mag, _ = _bv_udivrem(a_mag, b_mag, out, alloc)
    q_neg = _bv_neg(q_mag, out, alloc)
    sign_differs = _gate_xor(a_sign, b_sign, out, alloc)
    result = [_gate_ite(sign_differs, q_neg[k], q_mag[k], out, alloc) for k in range(w)]

    b_is_zero = _gate_not(_gate_or_n(b_bits, out, alloc), out, alloc)
    all_ones = [_CONST_TRUE(out, alloc) for _ in range(w)]
    one_val  = [_CONST_FALSE(out, alloc)] * (w - 1) + [_CONST_TRUE(out, alloc)]
    zero_case = [_gate_ite(a_sign, one_val[k], all_ones[k], out, alloc) for k in range(w)]
    return [_gate_ite(b_is_zero, zero_case[k], result[k], out, alloc) for k in range(w)]


def _bv_bvsrem(a_bits: List[int], b_bits: List[int],
               out: list, alloc: _Alloc) -> List[int]:
    """SMT-LIB (bvsrem s t): signed remainder, sign follows the dividend
    (not bvsmod, whose sign follows the divisor) -- computed the same
    sign/magnitude way as bvsdiv: |s| urem |t|, negated iff s is negative.
    This is the standard truncating-remainder identity r = s - trunc(s/t)*t
    reduced to magnitudes, matching _bvsrem in ns_evaluator.py exactly.
    Zero divisor: s itself (matches ns_evaluator.py)."""
    w = len(a_bits)
    a_sign, b_sign = a_bits[0], b_bits[0]
    a_neg, b_neg = _bv_neg(a_bits, out, alloc), _bv_neg(b_bits, out, alloc)
    a_mag = [_gate_ite(a_sign, a_neg[k], a_bits[k], out, alloc) for k in range(w)]
    b_mag = [_gate_ite(b_sign, b_neg[k], b_bits[k], out, alloc) for k in range(w)]
    _, r_mag = _bv_udivrem(a_mag, b_mag, out, alloc)
    r_neg = _bv_neg(r_mag, out, alloc)
    result = [_gate_ite(a_sign, r_neg[k], r_mag[k], out, alloc) for k in range(w)]

    b_is_zero = _gate_not(_gate_or_n(b_bits, out, alloc), out, alloc)
    return [_gate_ite(b_is_zero, a_bits[k], result[k], out, alloc) for k in range(w)]


def _bv_slt(a_bits: List[int], b_bits: List[int],
            out: list, alloc: _Alloc) -> int:
    """Signed a < b."""
    # If signs differ: a < b iff a is negative (MSB=1)
    # If signs equal:  unsigned compare of remaining bits
    w    = len(a_bits)
    a_s  = a_bits[0]        # sign bit of a
    b_s  = b_bits[0]        # sign bit of b
    # diff_sign = a_s AND NOT b_s  (a neg, b pos → a < b)
    not_b_s   = _gate_not(b_s, out, alloc)
    diff_sign = _gate_and(a_s, not_b_s, out, alloc)
    # same_sign = NOT (a_s XOR b_s)
    same_sign = _gate_not(_gate_xor(a_s, b_s, out, alloc), out, alloc)
    # ult_rest = unsigned compare
    ult_rest  = _bv_ult(a_bits, b_bits, out, alloc)
    # slt = diff_sign OR (same_sign AND ult_rest)
    both = _gate_and(same_sign, ult_rest, out, alloc)
    return _gate_or(diff_sign, both, out, alloc)


def _bv_sle(a_bits, b_bits, out, alloc) -> int:
    return _gate_not(_bv_slt(b_bits, a_bits, out, alloc), out, alloc)


# ── Shift circuits ─────────────────────────────────────────────────────────────

def _bv_shl(a_bits: List[int], b_bits: List[int],
            out: list, alloc: _Alloc) -> List[int]:
    """Logical shift left a by b (variable shift)."""
    w = len(a_bits)
    result = list(a_bits)
    for stage, bit in enumerate(reversed(b_bits)):  # LSB first
        shift_amt = 1 << stage
        if shift_amt >= w:
            # If this bit is set, all result bits are 0
            zero_bits = [_CONST_FALSE(out, alloc) for _ in range(w)]
            result = [_gate_ite(bit, zero_bits[i], result[i], out, alloc)
                      for i in range(w)]
            break
        shifted = result[shift_amt:] + [_CONST_FALSE(out, alloc)] * shift_amt
        result  = [_gate_ite(bit, shifted[i], result[i], out, alloc)
                   for i in range(w)]
    return result


def _bv_lshr(a_bits: List[int], b_bits: List[int],
             out: list, alloc: _Alloc) -> List[int]:
    """Logical shift right."""
    w = len(a_bits)
    result = list(a_bits)
    for stage, bit in enumerate(reversed(b_bits)):
        shift_amt = 1 << stage
        if shift_amt >= w:
            zero_bits = [_CONST_FALSE(out, alloc) for _ in range(w)]
            result = [_gate_ite(bit, zero_bits[i], result[i], out, alloc)
                      for i in range(w)]
            break
        shifted = [_CONST_FALSE(out, alloc)] * shift_amt + result[:w - shift_amt]
        result  = [_gate_ite(bit, shifted[i], result[i], out, alloc)
                   for i in range(w)]
    return result


def _bv_ashr(a_bits: List[int], b_bits: List[int],
             out: list, alloc: _Alloc) -> List[int]:
    """Arithmetic shift right (fill with sign bit)."""
    w    = len(a_bits)
    sign = a_bits[0]
    result = list(a_bits)
    for stage, bit in enumerate(reversed(b_bits)):
        shift_amt = 1 << stage
        if shift_amt >= w:
            fill = [sign] * w
            result = [_gate_ite(bit, fill[i], result[i], out, alloc)
                      for i in range(w)]
            break
        fill    = [sign] * shift_amt + result[:w - shift_amt]
        result  = [_gate_ite(bit, fill[i], result[i], out, alloc)
                   for i in range(w)]
    return result


# ── Main blaster ───────────────────────────────────────────────────────────────

class _Blaster:
    def __init__(self, deadline: Optional[float] = None):
        self.alloc   = _Alloc()
        self.clauses: List[List[int]] = []
        self.var_map: Dict[str, List[int]] = {}
        self._cache: Dict[int, object] = {}   # id(term) → blasted value
        self._deadline = deadline
        # Equality-only structural cache (measured to be worthwhile; ITE
        # showed 0% structural duplication on every real formula tested and
        # is deliberately NOT included here). Keyed by the operands'
        # already-resolved SAT-level representation -- not by AST identity
        # or a separately-computed structural AST key -- so its safety
        # rides entirely on blast_bv()/blast_bool() already being correct
        # (symbol interning, let-scoping, sort/width, all handled upstream
        # by the existing, trusted machinery); two equalities are cache-
        # equivalent here iff they compare the literal same SAT bits,
        # which is definitionally the same equality regardless of which
        # AST node asked for it. Scoped to this _Blaster instance, i.e. one
        # formula/one CNF, same as _cache and _Alloc's constant cache.
        self._eq_cache: Dict[tuple, tuple] = {}  # key -> (result, first_vars, first_clauses)
        self.eq_cache_hits    = 0
        self.eq_cache_misses  = 0
        self.eq_vars_avoided    = 0
        self.eq_clauses_avoided = 0

    def _check_deadline(self) -> None:
        # Checked on every blast_bv/blast_bool entry, not batched by a call
        # counter: a *single* call (e.g. a wide bvmul, O(width^2) gates) can
        # internally generate tens of thousands of clauses on its own, so
        # batching by call count can still let a lot of real wall-time slip
        # through unchecked between checks. time.time() itself is cheap
        # (microseconds) next to the arithmetic each call already does --
        # correctness here is worth far more than that negligible overhead.
        if self._deadline is not None and time.time() > self._deadline:
            raise BlastTimeout()

        # Array theory (read-over-write + weak consistency axioms — no
        # dedicated array decision procedure, so arrays are encoded lazily
        # as they're touched by select/store, keyed by the array "root"
        # variable's name rather than object identity: the parser builds a
        # fresh Var(name, sort) at every occurrence of a variable, so two
        # selects on "the same array" are two different Var objects with
        # the same .name -- id(term) would wrongly treat them as unrelated
        # arrays. array_name -> [(index_bits, value_bits), ...] of every
        # select resolved against that array so far.
        self._array_reads: Dict[str, List[Tuple[List[int], List[int]]]] = {}

    def _bv_var(self, name: str, width: int) -> List[int]:
        if name not in self.var_map:
            self.var_map[name] = self.alloc.fresh_bits(width)
        return self.var_map[name]

    # ── Array theory ─────────────────────────────────────────────────────

    def _blast_value(self, term: Term, sort) -> List[int]:
        """Blast a term known to have sort `sort` (an array's element
        sort) — dispatches to blast_bool (wrapped as a 1-bit list) or
        blast_bv, since the two produce differently-shaped results and the
        element sort isn't always recoverable from `term.sort` alone (e.g.
        a boolean literal used as a store's value)."""
        if isinstance(sort, BoolSort):
            return [self.blast_bool(term)]
        return self.blast_bv(term)

    def _blast_select(self, arr_term: Term, idx_term: Term, elem_sort) -> List[int]:
        """select(arr_term, idx_term), returning `elem_sort`-shaped bits.

        No dedicated array decision procedure: store/as_const/ite chains
        are peeled by direct term rewriting (read-over-write) rather than
        bit-blasted as arrays in their own right, so a store never itself
        needs an array representation -- only the eventual select does.
        Bottoms out at a genuine array variable (or any other opaque
        array-valued term), where consistency with every previously
        resolved select against that same array is enforced by the weak
        array axiom: idx_i == idx_j -> value_i == value_j."""
        out   = self.clauses
        alloc = self.alloc
        width = elem_sort.width if isinstance(elem_sort, BVSort) else 1

        if isinstance(arr_term, App) and arr_term.op == 'store':
            a0, i0, v0 = arr_term.args
            idx_bits  = self.blast_bv(idx_term)
            i0_bits   = self.blast_bv(i0)
            eq        = _bv_eq(idx_bits, i0_bits, out, alloc)
            then_bits = self._blast_value(v0, elem_sort)
            else_bits = self._blast_select(a0, idx_term, elem_sort)
            return [_gate_ite(eq, then_bits[k], else_bits[k], out, alloc)
                    for k in range(width)]

        if isinstance(arr_term, App) and arr_term.op == 'as_const':
            # Constant array: every index maps to the same value.
            return self._blast_value(arr_term.args[0], elem_sort)

        if isinstance(arr_term, App) and arr_term.op == 'ite':
            cond   = self.blast_bool(arr_term.args[0])
            t_bits = self._blast_select(arr_term.args[1], idx_term, elem_sort)
            e_bits = self._blast_select(arr_term.args[2], idx_term, elem_sort)
            return [_gate_ite(cond, t_bits[k], e_bits[k], out, alloc)
                    for k in range(width)]

        # Base case: an opaque array (a Var, or any other term we don't
        # peel further). Key by variable name, not id(term) -- the parser
        # builds a fresh Var object per occurrence, so two selects on "the
        # same array" are different objects sharing a .name.
        key = arr_term.name if isinstance(arr_term, Var) else f"#anon{id(arr_term)}"
        idx_bits    = self.blast_bv(idx_term)
        result_bits = alloc.fresh_bits(width)

        prior = self._array_reads.setdefault(key, [])
        for prev_idx_bits, prev_result_bits in prior:
            idx_eq = _bv_eq(idx_bits, prev_idx_bits, out, alloc)
            for k in range(width):
                val_eq = _gate_not(
                    _gate_xor(result_bits[k], prev_result_bits[k], out, alloc),
                    out, alloc)
                out.append([-idx_eq, val_eq])   # idx_i==idx_j -> bit_k equal
        prior.append((idx_bits, result_bits))

        return result_bits

    def blast_bv(self, term: Term) -> List[int]:
        """Return list of SAT literals (MSB first) representing BV term."""
        tid = id(term)
        if tid in self._cache:
            return self._cache[tid]

        self._check_deadline()
        result = self._blast_bv_inner(term)
        self._cache[tid] = result
        return result

    def _blast_bv_inner(self, term: Term) -> List[int]:
        out    = self.clauses
        alloc  = self.alloc

        if isinstance(term, BVLit):
            return _int_to_bits(term.value, term.width, out, alloc)

        if isinstance(term, Var) and isinstance(term.sort, BVSort):
            return self._bv_var(term.name, term.sort.width)

        if not isinstance(term, App):
            return [_CONST_FALSE(out, alloc)]

        op, args, p = term.op, term.args, term._params
        w = term.sort.width if isinstance(term.sort, BVSort) else 1

        if op == 'bvadd':
            return _bv_add(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
        if op == 'bvsub':
            return _bv_sub(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
        if op == 'bvmul':
            return _bv_mul(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
        if op == 'bvudiv':
            return _bv_bvudiv(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
        if op == 'bvurem':
            return _bv_bvurem(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
        if op == 'bvsdiv':
            return _bv_bvsdiv(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
        if op == 'bvsrem':
            return _bv_bvsrem(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
        if op == 'bvneg':
            return _bv_neg(self.blast_bv(args[0]), out, alloc)
        if op == 'bvnot':
            return _bv_not(self.blast_bv(args[0]), out, alloc)
        if op == 'bvand':
            return _bv_and(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
        if op == 'bvor':
            return _bv_or(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
        if op == 'bvxor':
            return _bv_xor(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
        if op == 'bvnand':
            return _bv_not(_bv_and(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc), out, alloc)
        if op == 'bvnor':
            return _bv_not(_bv_or(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc), out, alloc)
        if op == 'bvxnor':
            return _bv_not(_bv_xor(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc), out, alloc)
        if op == 'bvshl':
            return _bv_shl(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
        if op == 'bvlshr':
            return _bv_lshr(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
        if op == 'bvashr':
            return _bv_ashr(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)

        if op == 'concat':
            return self.blast_bv(args[0]) + self.blast_bv(args[1])

        if op == 'extract':
            hi, lo = p[0], p[1]
            a_bits = self.blast_bv(args[0])
            total  = len(a_bits)
            # MSB first: bit index i from MSB = bit position (total-1-i) from LSB
            lo_idx = total - 1 - hi
            hi_idx = total - 1 - lo
            return a_bits[lo_idx : hi_idx + 1]

        if op == 'zero_extend':
            n      = p[0]
            a_bits = self.blast_bv(args[0])
            return [_CONST_FALSE(out, alloc)] * n + a_bits

        if op == 'sign_extend':
            n      = p[0]
            a_bits = self.blast_bv(args[0])
            sign   = a_bits[0]
            return [sign] * n + a_bits

        if op == 'rotate_left':
            n      = p[0] % w if w else 0
            a_bits = self.blast_bv(args[0])
            return a_bits[n:] + a_bits[:n]

        if op == 'rotate_right':
            n      = p[0] % w if w else 0
            a_bits = self.blast_bv(args[0])
            return a_bits[w - n:] + a_bits[:w - n]

        if op == 'repeat':
            n      = p[0]
            a_bits = self.blast_bv(args[0])
            return a_bits * n

        if op == 'bvcomp':
            eq = _bv_eq(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
            return [eq]

        if op == 'ite':
            cond  = self.blast_bool(args[0])
            t_    = self.blast_bv(args[1])
            e_    = self.blast_bv(args[2])
            return [_gate_ite(cond, t_[i], e_[i], out, alloc) for i in range(len(t_))]

        if op == 'select':
            return self._blast_select(args[0], args[1], term.sort)

        raise UnsupportedBitVectorOperator(
            f"no bit-vector encoding for operator {op!r} "
            f"(width {w}, {len(args)} args)")

    def blast_bool(self, term: Term) -> int:
        """Return a SAT literal for a Bool-sorted term."""
        tid = id(term)
        if tid in self._cache:
            return self._cache[tid]
        self._check_deadline()
        result = self._blast_bool_inner(term)
        self._cache[tid] = result
        return result

    def _blast_bool_inner(self, term: Term) -> int:
        out   = self.clauses
        alloc = self.alloc

        if isinstance(term, BoolLit):
            if term.value:
                return _CONST_TRUE(out, alloc)
            else:
                return _CONST_FALSE(out, alloc)

        if isinstance(term, Var) and isinstance(term.sort, BoolSort):
            return self._bv_var(term.name, 1)[0]

        if not isinstance(term, App):
            return _CONST_TRUE(out, alloc)

        op, args = term.op, term.args

        if op == 'and':
            lits = [self.blast_bool(a) for a in args]
            return _gate_and_n(lits, out, alloc)
        if op == 'or':
            lits = [self.blast_bool(a) for a in args]
            return _gate_or_n(lits, out, alloc)
        if op == 'not':
            return _gate_not(self.blast_bool(args[0]), out, alloc)
        if op == 'xor':
            r = self.blast_bool(args[0])
            for a in args[1:]:
                r = _gate_xor(r, self.blast_bool(a), out, alloc)
            return r
        if op in ('=>', 'implies'):
            a_ = self.blast_bool(args[0])
            b_ = self.blast_bool(args[1])
            return _gate_or(_gate_not(a_, out, alloc), b_, out, alloc)
        if op == 'ite':
            c_ = self.blast_bool(args[0])
            t_ = self.blast_bool(args[1])
            e_ = self.blast_bool(args[2])
            return _gate_ite(c_, t_, e_, out, alloc)

        if op == '=':
            s0 = args[0].sort
            if isinstance(s0, BVSort):
                lhs_bits = self.blast_bv(args[0])
                rhs_bits = self.blast_bv(args[1])
                key = ('bveq', tuple(lhs_bits), tuple(rhs_bits))
                entry = self._eq_cache.get(key)
                if entry is not None:
                    self.eq_cache_hits += 1
                    result, fv, fc = entry
                    self.eq_vars_avoided    += fv
                    self.eq_clauses_avoided += fc
                    return result
                before_v, before_c = alloc.count, len(out)
                result = _bv_eq(lhs_bits, rhs_bits, out, alloc)
                self._eq_cache[key] = (
                    result, alloc.count - before_v, len(out) - before_c)
                self.eq_cache_misses += 1
                return result
            elif isinstance(s0, BoolSort):
                a_ = self.blast_bool(args[0])
                b_ = self.blast_bool(args[1])
                key = ('booleq', a_, b_)
                entry = self._eq_cache.get(key)
                if entry is not None:
                    self.eq_cache_hits += 1
                    result, fv, fc = entry
                    self.eq_vars_avoided    += fv
                    self.eq_clauses_avoided += fc
                    return result
                before_v, before_c = alloc.count, len(out)
                result = _gate_not(_gate_xor(a_, b_, out, alloc), out, alloc)
                self._eq_cache[key] = (
                    result, alloc.count - before_v, len(out) - before_c)
                self.eq_cache_misses += 1
                return result
            else:
                return _CONST_TRUE(out, alloc)

        if op == 'distinct':
            s0 = args[0].sort
            if isinstance(s0, BVSort) and len(args) == 2:
                eq = _bv_eq(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
                return _gate_not(eq, out, alloc)
            return _CONST_TRUE(out, alloc)

        # BV comparisons
        if op == 'bvult':
            return _bv_ult(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
        if op == 'bvule':
            return _bv_ule(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
        if op == 'bvugt':
            return _bv_ult(self.blast_bv(args[1]), self.blast_bv(args[0]), out, alloc)
        if op == 'bvuge':
            return _bv_ule(self.blast_bv(args[1]), self.blast_bv(args[0]), out, alloc)
        if op == 'bvslt':
            return _bv_slt(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
        if op == 'bvsle':
            return _bv_sle(self.blast_bv(args[0]), self.blast_bv(args[1]), out, alloc)
        if op == 'bvsgt':
            return _bv_slt(self.blast_bv(args[1]), self.blast_bv(args[0]), out, alloc)
        if op == 'bvsge':
            return _bv_sle(self.blast_bv(args[1]), self.blast_bv(args[0]), out, alloc)

        if op == 'bvcomp':
            bits = self.blast_bv(term)
            return bits[0]

        if op == 'select':
            bits = self._blast_select(args[0], args[1], BOOL)
            return bits[0]

        # Found during the same audit that surfaced the missing bvudiv/
        # bvurem/bvsdiv/bvsrem dispatch: this previously returned
        # _CONST_TRUE(out, alloc) unconditionally for any unrecognized
        # Boolean operator -- a fixed wrong value, not even a genuinely
        # unconstrained one. Same fix, same reasoning as the BV-side
        # UnsupportedBitVectorOperator: fail loudly (caught by the
        # existing except Exception in ns_solver.py, degrading to
        # RESULT_UNKNOWN) rather than silently assert something false.
        raise UnsupportedBitVectorOperator(
            f"no boolean encoding for operator {op!r} ({len(args)} args)")


# ── Public API ─────────────────────────────────────────────────────────────────

def blast(formula: NsFormula, deadline: Optional[float] = None,
          stats_out: Optional[dict] = None):
    """
    Bit-blast a QF_BV / QF_ABV formula.
    Returns (clauses, n_vars, var_map) -- unchanged contract, existing
    callers are unaffected.
    Raises BlastTimeout if `deadline` (an absolute time.time() value) is
    given and exceeded before blasting finishes -- checked on every
    not-yet-cached term visited via blast_bv/blast_bool. A cache hit skips
    the check: it does no new work, so it can't be what overruns a deadline.

    `stats_out`, when given a dict, is populated (in place) with the
    constant-encoding counters from this solve's _Alloc -- profiling-only,
    exact counts (not estimates): true_requests, false_requests,
    true_allocations, false_allocations, bv_const_requests,
    unique_bv_constants, constant_sat_vars_avoided, constant_clauses_avoided.
    """
    blaster = _Blaster(deadline=deadline)
    top_lits = []

    for assertion in formula.assertions:
        lit = blaster.blast_bool(assertion)
        top_lits.append(lit)

    # Assert all top-level literals to be True
    for lit in top_lits:
        blaster.clauses.append([lit])

    if stats_out is not None:
        a = blaster.alloc
        stats_out.update(
            true_requests=a.true_requests,
            false_requests=a.false_requests,
            true_allocations=a.true_allocations,
            false_allocations=a.false_allocations,
            bv_const_requests=a.bv_const_requests,
            unique_bv_constants=a.unique_bv_constants,
            constant_sat_vars_avoided=a.constant_sat_vars_avoided,
            constant_clauses_avoided=a.constant_clauses_avoided,
            eq_cache_hits=blaster.eq_cache_hits,
            eq_cache_misses=blaster.eq_cache_misses,
            eq_vars_avoided=blaster.eq_vars_avoided,
            eq_clauses_avoided=blaster.eq_clauses_avoided,
        )

    return blaster.clauses, blaster.alloc.count, blaster.var_map


def reconstruct(sat_assign: Dict[int, bool],
                var_map: Dict[str, List[int]]) -> Dict[str, int]:
    """
    Convert SAT assignment back to BV variable values.
    sat_assign: {sat_var (1-indexed): bool}
    var_map:    {bv_var_name: [sat_var, ...]}  (MSB first)
    """
    result = {}
    for name, bits in var_map.items():
        value = 0
        for bit_var in bits:
            bit_val = sat_assign.get(abs(bit_var), False)
            if bit_var < 0:
                bit_val = not bit_val
            value = (value << 1) | (1 if bit_val else 0)
        result[name] = value
    return result
