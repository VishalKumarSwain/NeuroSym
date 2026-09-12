"""Exhaustive/randomized differential tests for bvudiv/bvurem/bvsdiv/bvsrem
in gansat/ns_bitblaster.py, against gansat/ns_evaluator.py (the pre-existing,
already-correct SMT-LIB reference semantics) as the source of truth.
"""
import sys, os, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gansat.ns_bitblaster import blast, reconstruct
from gansat.ns_dpll import solve_cnf
from gansat.ns_parser import parse_string
from gansat.ns_evaluator import evaluate, _bvudiv, _bvurem, _bvsdiv, _bvsrem

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f'[FAIL] {name}')

def solve_get(smt):
    """Parse+bitblast+solve; return (result_str, model_dict)."""
    f = parse_string(smt)
    clauses, n_vars, var_map = blast(f)
    assign = solve_cnf(clauses, n_vars)
    if assign is None:
        return 'unsat', None
    m = reconstruct(assign, var_map)
    ok = evaluate(f, m)
    return ('sat' if ok else 'eval_mismatch'), m

def check_op(op, w, a, b):
    """Assert (op x y) == expected(a,b) with x,y fixed to a,b via equality
    constraints, and separately assert (op x y) != expected as UNSAT --
    both directions catch different classes of encoding bug."""
    expected = {
        'bvudiv': _bvudiv, 'bvurem': _bvurem,
        'bvsdiv': _bvsdiv, 'bvsrem': _bvsrem,
    }[op](a, b, w)

    smt_pos = f"""
(set-logic QF_BV)
(declare-fun x () (_ BitVec {w}))
(declare-fun y () (_ BitVec {w}))
(declare-fun r () (_ BitVec {w}))
(assert (= x (_ bv{a} {w})))
(assert (= y (_ bv{b} {w})))
(assert (= r ({op} x y)))
(assert (= r (_ bv{expected} {w})))
(check-sat)
"""
    r, m = solve_get(smt_pos)
    check(f'{op} w={w} {a}/{b} -> expect sat with r={expected}', r == 'sat')

    smt_neg = f"""
(set-logic QF_BV)
(declare-fun x () (_ BitVec {w}))
(declare-fun y () (_ BitVec {w}))
(declare-fun r () (_ BitVec {w}))
(assert (= x (_ bv{a} {w})))
(assert (= y (_ bv{b} {w})))
(assert (= r ({op} x y)))
(assert (not (= r (_ bv{expected} {w}))))
(check-sat)
"""
    r2, _ = solve_get(smt_neg)
    check(f'{op} w={w} {a}/{b} -> expect UNSAT for r != {expected}', r2 == 'unsat')

# ── Phase 6/7: exhaustive bvudiv/bvurem, widths 1-4, every pair including 0 ──
for w in (1, 2, 3, 4):
    for a in range(1 << w):
        for b in range(1 << w):
            check_op('bvudiv', w, a, b)
            check_op('bvurem', w, a, b)

print(f'unsigned exhaustive (w1-4): {passed} passed, {failed} failed so far')

# ── Phase 8/9: exhaustive bvsdiv/bvsrem, widths 2-4, every pair ──
for w in (2, 3, 4):
    for a in range(1 << w):
        for b in range(1 << w):
            check_op('bvsdiv', w, a, b)
            check_op('bvsrem', w, a, b)

print(f'after signed exhaustive (w2-4): {passed} passed, {failed} failed so far')

# ── Phase 14/15: explicit division-by-zero and signed-overflow matrix ──
for w in (1, 2, 3, 4, 8, 32):
    check_op('bvudiv', w, 0, 0)
    check_op('bvurem', w, 0, 0)
    check_op('bvudiv', w, (1 << w) - 1, 0)
    check_op('bvurem', w, (1 << w) - 1, 0)
    if w >= 2:
        min_signed = 1 << (w - 1)
        max_signed = (1 << (w - 1)) - 1
        neg_one = (1 << w) - 1  # -1 in two's complement
        check_op('bvsdiv', w, 0, 0)
        check_op('bvsdiv', w, 1, 0)
        check_op('bvsdiv', w, neg_one, 0)
        check_op('bvsdiv', w, min_signed, 0)
        check_op('bvsrem', w, min_signed, 0)
        # the classic overflow trap: MIN_SIGNED / -1
        check_op('bvsdiv', w, min_signed, neg_one)
        check_op('bvsrem', w, min_signed, neg_one)
        check_op('bvsdiv', w, min_signed, 1)
        check_op('bvsdiv', w, max_signed, neg_one)

print(f'after div-by-zero/overflow matrix: {passed} passed, {failed} failed so far')

# ── Phase 10 (partial -- no external solver used per instructions;
#     random testing against ns_evaluator, the accepted reference) ──
random.seed(1234)
for _ in range(1000):
    w = random.choice([1, 2, 3, 4, 8, 16, 32])
    a = random.randrange(1 << w)
    b = random.randrange(1 << w)
    op = random.choice(['bvudiv', 'bvurem', 'bvsdiv', 'bvsrem'])
    check_op(op, w, a, b)

print(f'after 1000 randomized (w up to 32): {passed} passed, {failed} failed so far')

# ── Phase 11: compositional / symbolic formulas ──
r, m = solve_get("""
(set-logic QF_BV)
(declare-fun x () (_ BitVec 8))
(declare-fun y () (_ BitVec 8))
(assert (bvugt y (_ bv0 8)))
(assert (= (bvudiv x y) (_ bv5 8)))
(assert (bvult x (_ bv50 8)))
""")
check('symbolic bvudiv with side constraints -> sat', r == 'sat')
if m:
    check('symbolic bvudiv model matches evaluator', m['x'] // m['y'] == 5 if m['y'] else True)

r, m = solve_get("""
(set-logic QF_BV)
(declare-fun x () (_ BitVec 8))
(declare-fun y () (_ BitVec 8))
(declare-fun c () Bool)
(assert (= x (ite c (_ bv20 8) (_ bv7 8))))
(assert (= y (_ bv3 8)))
(assert (= (bvurem x y) (_ bv2 8)))
""")
check('bvurem composed with ite -> sat (20 rem 3 == 2)', r == 'sat')

r, _ = solve_get("""
(set-logic QF_BV)
(declare-fun x () (_ BitVec 8))
(assert (= (bvudiv (_ bv10 8) (_ bv2 8)) (_ bv5 8)))
""")
check('constant-folded bvudiv: 10/2==5 -> sat', r == 'sat')

r, _ = solve_get("""
(set-logic QF_BV)
(assert (not (= (bvudiv (_ bv10 8) (_ bv2 8)) (_ bv5 8))))
""")
check('constant-folded bvudiv: 10/2!=5 -> unsat', r == 'unsat')

r, _ = solve_get("""
(set-logic QF_BV)
(assert (not (= (bvurem (_ bv10 8) (_ bv3 8)) (_ bv1 8))))
""")
check('constant-folded bvurem: 10%3!=1 -> unsat', r == 'unsat')

print(f'\\n{passed} passed, {failed} failed')
sys.exit(1 if failed else 0)
