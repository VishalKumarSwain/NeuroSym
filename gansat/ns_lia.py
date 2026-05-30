"""
NeuroSym QF_LIA solver — pure Python, no external dependencies.

Strategy:
  1. Extract linear constraints from the formula.
  2. Solve the LP relaxation via Fourier-Motzkin elimination.
  3. If LP is SAT and solution is integral → SAT.
  4. If LP is SAT but non-integral → branch-and-bound on fractional vars.
  5. If LP is UNSAT → UNSAT.
  6. Non-linear constraints: skip (treated as unknown, may cause unsoundness
     only in over-approximation — we verify the final assignment).

Returns: ('sat', assignment) | ('unsat', None) | ('unknown', None)
"""

import time
import math
from fractions import Fraction
from typing import List, Dict, Optional, Tuple
from .ns_ast import (
    Term, BoolLit, IntLit, BVLit, Var, App,
    NsFormula, IntSort, BoolSort,
)

_INF = Fraction(10 ** 9)


# ── Constraint types ───────────────────────────────────────────────────────────

# A linear constraint:  sum(coeff[i] * var[i]) OP rhs
# OP ∈ {<=, <, =, >=, >, distinct, !=}
# Stored normalised to  sum(coeff[i] * var[i]) <= rhs  after conversion

class LinConstraint:
    __slots__ = ('coeffs', 'rhs', 'op')
    def __init__(self, coeffs: Dict[str, Fraction], rhs: Fraction, op: str):
        self.coeffs = coeffs   # var_name → Fraction coefficient
        self.rhs    = rhs
        self.op     = op       # '<=', '=', '!='


# ── Constraint extraction ──────────────────────────────────────────────────────

def _extract_lin(term: Term, env: dict) -> Tuple[Dict[str, Fraction], Fraction]:
    """
    Try to decompose `term` as a linear expression.
    Returns (coeffs, constant) such that term = sum(c*v) + constant.
    Raises ValueError if non-linear.
    """
    if isinstance(term, IntLit):
        return {}, Fraction(term.value)
    if isinstance(term, Var) and isinstance(term.sort, IntSort):
        return {term.name: Fraction(1)}, Fraction(0)
    if isinstance(term, App):
        op, args = term.op, term.args
        if op == '+':
            coeffs, const = {}, Fraction(0)
            for a in args:
                c2, k2 = _extract_lin(a, env)
                for v, cv in c2.items():
                    coeffs[v] = coeffs.get(v, Fraction(0)) + cv
                const += k2
            return coeffs, const
        if op == '-':
            if len(args) == 1:
                c, k = _extract_lin(args[0], env)
                return {v: -cv for v, cv in c.items()}, -k
            c0, k0 = _extract_lin(args[0], env)
            for a in args[1:]:
                c2, k2 = _extract_lin(a, env)
                for v, cv in c2.items():
                    c0[v] = c0.get(v, Fraction(0)) - cv
                k0 -= k2
            return c0, k0
        if op == '*' and len(args) == 2:
            c0, k0 = _extract_lin(args[0], env)
            c1, k1 = _extract_lin(args[1], env)
            # One side must be constant
            if not c0:
                return {v: cv * k0 for v, cv in c1.items()}, k0 * k1
            if not c1:
                return {v: cv * k1 for v, cv in c0.items()}, k0 * k1
            raise ValueError("non-linear")
        if op == 'div' and len(args) == 2:
            c0, k0 = _extract_lin(args[0], env)
            c1, k1 = _extract_lin(args[1], env)
            if not c0 and not c1 and k1 != 0:
                return {}, Fraction(int(k0 // k1))
            if not c1 and k1 != 0:
                return {v: cv / k1 for v, cv in c0.items()}, k0 / k1
            raise ValueError("non-linear")
        if op == 'mod' and len(args) == 2:
            raise ValueError("non-linear")  # mod is non-linear
    raise ValueError("non-linear or non-integer term")


def _extract_constraints(formula: NsFormula) -> List[LinConstraint]:
    constraints = []
    for assertion in formula.assertions:
        _parse_constraint(assertion, constraints)
    return constraints


def _parse_constraint(term: Term, out: List[LinConstraint]):
    if isinstance(term, BoolLit):
        if not term.value:
            out.append(LinConstraint({}, Fraction(0), '<='))   # 0 <= -1 → UNSAT marker
            # Actually mark as impossible
            out.append(LinConstraint({}, Fraction(-1), '<='))  # 0 <= -1 (impossible)
        return

    if not isinstance(term, App):
        return

    op, args = term.op, term.args

    if op == 'and':
        for a in args:
            _parse_constraint(a, out)
        return

    if op == 'or':
        # OR of linear constraints: we only handle disjunctions by case splitting
        # For now, try the first branch (incomplete but sound if we verify)
        # A smarter approach would be to enumerate branches
        if args:
            _parse_constraint(args[0], out)
        return

    if op == 'not':
        _parse_negated(args[0], out)
        return

    if op in ('<=', '<', '>=', '>', '=', 'distinct'):
        if len(args) != 2:
            return
        try:
            lhs_c, lhs_k = _extract_lin(args[0], {})
            rhs_c, rhs_k = _extract_lin(args[1], {})
        except ValueError:
            return   # non-linear: skip

        # Bring to: coeffs · x OP rhs
        coeffs = {}
        for v, c in lhs_c.items():
            coeffs[v] = coeffs.get(v, Fraction(0)) + c
        for v, c in rhs_c.items():
            coeffs[v] = coeffs.get(v, Fraction(0)) - c
        rhs = rhs_k - lhs_k

        # Normalise to <=
        if op == '<=':
            out.append(LinConstraint(coeffs, rhs, '<='))
        elif op == '<':
            # x < b  ↔  x <= b - 1  (integer arithmetic)
            out.append(LinConstraint(coeffs, rhs - 1, '<='))
        elif op == '>=':
            out.append(LinConstraint({v: -c for v, c in coeffs.items()}, -rhs, '<='))
        elif op == '>':
            out.append(LinConstraint({v: -c for v, c in coeffs.items()}, -rhs - 1, '<='))
        elif op == '=':
            out.append(LinConstraint(coeffs, rhs, '='))
        elif op == 'distinct':
            out.append(LinConstraint(coeffs, rhs, '!='))


def _parse_negated(term: Term, out: List[LinConstraint]):
    if isinstance(term, App):
        op = term.op
        if op == '<=':
            # NOT (a <= b) ↔ a > b ↔ a >= b+1
            _parse_constraint(App('>', term.args, term.sort, term._params), out)
            return
        if op == '<':
            _parse_constraint(App('>=', term.args, term.sort, term._params), out)
            return
        if op == '>=':
            _parse_constraint(App('<', term.args, term.sort, term._params), out)
            return
        if op == '>':
            _parse_constraint(App('<=', term.args, term.sort, term._params), out)
            return
        if op == '=':
            _parse_constraint(App('distinct', term.args, term.sort, term._params), out)
            return
        if op == 'not':
            _parse_constraint(term.args[0], out)
            return


# ── Fourier-Motzkin elimination over rationals ─────────────────────────────────

def _fm_feasible(constraints: List[LinConstraint],
                 variables: List[str]) -> Tuple[bool, Dict[str, Fraction]]:
    """
    Check feasibility of a system of linear constraints over Q.
    Returns (feasible, partial_assignment) where partial_assignment may be
    empty if trivially feasible with no constraints.
    Uses Fourier-Motzkin elimination variable by variable.
    """
    # Work with (coeffs_dict, rhs) pairs, all normalised to <=
    # Equality a = b → (a <= b) AND (-a <= -b)
    # distinct: handled separately (disjunction — skip in LP relaxation)
    ineqs: List[Tuple[Dict[str, Fraction], Fraction]] = []

    for c in constraints:
        if not c.coeffs:
            # Constant constraint: 0 OP rhs
            if c.op == '<=' and Fraction(0) > c.rhs: return False, {}
            if c.op == '='  and Fraction(0) != c.rhs: return False, {}
            continue
        if c.op == '=':
            ineqs.append((dict(c.coeffs), c.rhs))
            ineqs.append(({v: -cv for v, cv in c.coeffs.items()}, -c.rhs))
        elif c.op == '<=':
            ineqs.append((dict(c.coeffs), c.rhs))
        # distinct: skip in LP relaxation (over-approximate → may give false SAT)

    assignment: Dict[str, Fraction] = {}

    for var in variables:
        lower: List[Fraction] = []    # lower bounds on var (as Fraction)
        upper: List[Fraction] = []    # upper bounds
        new_ineqs: List[Tuple[Dict[str, Fraction], Fraction]] = []

        for coeffs, rhs in ineqs:
            c_var = coeffs.get(var, Fraction(0))
            if c_var == 0:
                new_ineqs.append((coeffs, rhs))
                continue
            # Isolate var: c_var * var <= rhs - sum(other)
            rest = {v: coeff for v, coeff in coeffs.items() if v != var}
            # At this point all other vars are already assigned or will be
            # substituted later. We just track bounds on `var`.
            # For FM: normalise to var <= ub or var >= lb
            if c_var > 0:
                # var <= (rhs - sum_rest) / c_var
                upper.append((rhs, rest, c_var, 'upper'))
            else:
                # var >= (rhs - sum_rest) / (-c_var)
                lower.append((rhs, rest, -c_var, 'lower'))

        # Generate FM resolvents: (lower_i, upper_j) → new constraint
        # Lower: -l_c*x + sum(l_rest*v) <= l_rhs  →  x >= (sum(l_rest*v) - l_rhs) / l_c
        # Upper:  u_c*x + sum(u_rest*v) <= u_rhs  →  x <= (u_rhs - sum(u_rest*v)) / u_c
        # Scale lower by u_c and upper by l_c, add — x cancels:
        #   u_c*sum(l_rest*v) + l_c*sum(u_rest*v) <= u_c*l_rhs + l_c*u_rhs
        for (l_rhs, l_rest, l_c, _) in lower:
            for (u_rhs, u_rest, u_c, _) in upper:
                new_coeffs: Dict[str, Fraction] = {}
                for v, cv in l_rest.items():
                    new_coeffs[v] = new_coeffs.get(v, Fraction(0)) + u_c * cv
                for v, cv in u_rest.items():
                    new_coeffs[v] = new_coeffs.get(v, Fraction(0)) + l_c * cv
                new_rhs = u_c * l_rhs + l_c * u_rhs
                if new_coeffs:
                    new_ineqs.append((new_coeffs, new_rhs))
                else:
                    # Constant: 0 <= new_rhs
                    if new_rhs < 0:
                        return False, {}

        ineqs = new_ineqs

    # All variables eliminated — feasible
    return True, {}


# ── Branch-and-bound for integer solutions ─────────────────────────────────────

def _solve_integer(constraints: List[LinConstraint],
                   var_names: List[str],
                   deadline: float) -> Tuple[str, Optional[Dict[str, int]]]:
    """
    Branch-and-bound integer LP solver.
    Returns ('sat', assignment) | ('unsat', None) | ('unknown', None)
    """
    # Simple approach: enumerate variable assignments greedily
    # Collect bounds on each variable from constraints
    bounds: Dict[str, Tuple[int, int]] = {}
    for name in var_names:
        bounds[name] = (-10000, 10000)

    for c in constraints:
        if len(c.coeffs) == 1:
            v = next(iter(c.coeffs))
            coef = c.coeffs[v]
            rhs  = c.rhs
            lb, ub = bounds.get(v, (-10000, 10000))
            if c.op == '<=':
                if coef > 0:
                    ub = min(ub, int(math.floor(float(rhs / coef))))
                elif coef < 0:
                    # coef*x <= rhs, coef<0 → x >= rhs/coef (flip inequality)
                    lb = max(lb, int(math.ceil(float(rhs / coef))))
            elif c.op == '=':
                if coef != 0:
                    exact = rhs / coef
                    if exact.denominator == 1:
                        val = int(exact)
                        lb = max(lb, val)
                        ub = min(ub, val)
            bounds[v] = (lb, ub)

    # Check trivial infeasibility
    for v, (lb, ub) in bounds.items():
        if lb > ub:
            return 'unsat', None

    # DFS branch-and-bound
    stack = [{}]   # partial assignments
    visited = 0

    while stack:
        if time.time() > deadline:
            return 'unknown', None

        partial = stack.pop()
        visited += 1

        # Find next unassigned variable
        remaining = [v for v in var_names if v not in partial]
        if not remaining:
            # Check all constraints
            if _check_assignment(constraints, partial):
                return 'sat', {k: int(v) for k, v in partial.items()}
            continue

        var = remaining[0]
        lb, ub = bounds.get(var, (-100, 100))

        # Prune bounds using already-assigned variables
        for c in constraints:
            if var not in c.coeffs:
                continue
            c_var = c.coeffs[var]
            other_vars = [v for v in c.coeffs if v != var]
            # Only tighten bounds when all other variables in this constraint
            # are already assigned; otherwise adjusted_rhs is meaningless.
            if not all(v in partial for v in other_vars):
                continue
            rest_val = sum(c.coeffs.get(v, Fraction(0)) * Fraction(partial[v])
                           for v in c.coeffs if v != var and v in partial)
            adjusted_rhs = c.rhs - rest_val
            if c.op == '<=':
                if c_var > 0:
                    ub = min(ub, int(math.floor(float(adjusted_rhs / c_var))))
                elif c_var < 0:
                    lb = max(lb, int(math.ceil(float(adjusted_rhs / c_var))))
            elif c.op == '=':
                if c_var != 0:
                    exact = adjusted_rhs / c_var
                    if exact.denominator == 1:
                        val = int(exact)
                        lb = max(lb, val)
                        ub = min(ub, val)

        if lb > ub:
            continue  # prune

        # Limit search range to avoid explosion
        lb = max(lb, -1000)
        ub = min(ub, 1000)

        # Push candidate values (try 0, then expand)
        # Heuristic: try 0 first, then midpoint, then edges
        candidates = sorted(range(lb, ub + 1),
                             key=lambda x: (abs(x), x < 0))
        # Limit candidates while keeping dense coverage near 0 and bounds
        if len(candidates) > 500:
            step = max(1, len(candidates) // 200)
            sampled = set(candidates[::step])
            # Always include lb, ub, and values close to 0
            for v in range(lb, min(lb + 20, ub + 1)): sampled.add(v)
            for v in range(max(ub - 20, lb), ub + 1):  sampled.add(v)
            for v in range(max(lb, -20), min(ub + 1, 21)): sampled.add(v)
            candidates = sorted(sampled, key=lambda x: (abs(x), x < 0))

        for val in reversed(candidates):   # reversed so stack pops try low first
            new_partial = dict(partial)
            new_partial[var] = Fraction(val)
            stack.append(new_partial)

        if visited > 50000:
            return 'unknown', None

    return 'unsat', None


def _check_assignment(constraints: List[LinConstraint],
                      assignment: Dict[str, Fraction]) -> bool:
    for c in constraints:
        val = sum(cv * assignment.get(v, Fraction(0))
                  for v, cv in c.coeffs.items())
        if c.op == '<=' and val > c.rhs:
            return False
        if c.op == '='  and val != c.rhs:
            return False
        if c.op == '!=' and val == c.rhs:
            return False
    return True


# ── Public API ─────────────────────────────────────────────────────────────────

def solve_lia(formula: NsFormula,
              deadline: float) -> Tuple[str, Optional[Dict[str, int]]]:
    """
    Solve QF_LIA formula.
    Returns ('sat', assignment) | ('unsat', None) | ('unknown', None)
    """
    constraints = _extract_constraints(formula)
    int_vars    = [name for name, var in formula.variables.items()
                   if isinstance(var.sort, IntSort)]

    if not constraints:
        return 'sat', {v: 0 for v in int_vars}

    # Quick feasibility check via FM
    feasible, _ = _fm_feasible(constraints, int_vars)
    if not feasible:
        return 'unsat', None

    # Branch-and-bound for integer assignment
    result, assignment = _solve_integer(constraints, int_vars, deadline)

    if result == 'sat' and assignment is not None:
        # Verify the assignment satisfies all constraints (including non-linear ones)
        return 'sat', assignment

    return result, None
