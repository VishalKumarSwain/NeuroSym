import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gansat.ns_bitblaster import blast, reconstruct
from gansat.ns_dpll import solve_cnf
from gansat.ns_parser import parse_string
from gansat.ns_evaluator import evaluate

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond:
        passed += 1; print(f'[PASS] {name}')
    else:
        failed += 1; print(f'[FAIL] {name}')

def solve(smt):
    f = parse_string(smt)
    clauses, n_vars, var_map = blast(f)
    assign = solve_cnf(clauses, n_vars)
    if assign is None:
        return 'unsat', None
    bv = reconstruct(assign, var_map)
    return ('sat' if evaluate(f, bv) else 'eval_mismatch'), bv

# Phase M: repeated equality expressions -- structural cache should hit
r, m = solve("""
(set-logic QF_BV)
(declare-fun x () (_ BitVec 32))
(declare-fun y () (_ BitVec 32))
(assert (= x y))
(assert (or (= x y) (= x y)))
(assert (ite (= x y) (= x y) (= x y)))
(check-sat)
""")
check('repeated (= x y) formula solves sat', r == 'sat')
check('repeated (= x y) formula: model has x==y', m and m['x'] == m['y'])

# structurally DIFFERENT equalities must not collide
r, m = solve("""
(set-logic QF_BV)
(declare-fun x () (_ BitVec 8))
(declare-fun y () (_ BitVec 8))
(declare-fun z () (_ BitVec 8))
(assert (= x y))
(assert (= x z))
(assert (not (= y z)))
(check-sat)
""")
check('(=x y) and (=x z) and not(=y z) -> unsat (would be sat if wrongly merged)', r == 'unsat')

r, m = solve("""
(set-logic QF_BV)
(declare-fun x () (_ BitVec 8))
(declare-fun y () (_ BitVec 8))
(assert (= x y))
(assert (= y x))
(assert (= x (_ bv7 8)))
(check-sat)
""")
check('(=x y) and (=y x) [reversed operands, separate keys] -> sat, x=y=7',
      r == 'sat' and m['x'] == 7 and m['y'] == 7)

# Boolean equality repeated
r, m = solve("""
(set-logic QF_BV)
(declare-fun a () Bool)
(declare-fun b () Bool)
(assert (= a b))
(assert (or (= a b) (= a b)))
(assert a)
(check-sat)
""")
check('repeated boolean (= a b), a=true -> sat', r == 'sat')

# Phase N: ITE differences must not collide (ITE is NOT memoized, but
# verify correctness of un-memoized encoding still holds under the new code)
r, m = solve("""
(set-logic QF_BV)
(declare-fun c () Bool)
(declare-fun x () (_ BitVec 8))
(declare-fun y () (_ BitVec 8))
(assert (= x (ite c (_ bv1 8) (_ bv2 8))))
(assert (= y (ite c (_ bv2 8) (_ bv1 8))))
(assert c)
(check-sat)
""")
check('ite(c,a,b) vs ite(c,b,a) not confused, c=true -> x=1,y=2',
      r == 'sat' and m['x'] == 1 and m['y'] == 2)

# Known SAT/UNSAT regression from the earlier phase, re-verified here
r, m = solve("""
(set-logic QF_BV)
(declare-fun x () (_ BitVec 32))
(assert (= x (_ bv1 32)))
(assert (= x (_ bv2 32)))
(check-sat)
""")
check('UNSAT: x=1 and x=2 -> unsat (regression)', r == 'unsat')

print(f'\\n{passed} passed, {failed} failed')
sys.exit(1 if failed else 0)
