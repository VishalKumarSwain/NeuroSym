import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gansat.ns_bitblaster import blast, reconstruct, _Alloc, _int_to_bits, _CONST_TRUE, _CONST_FALSE
from gansat.ns_parser import parse_string
from gansat.ns_evaluator import evaluate

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f'[PASS] {name}')
    else:
        failed += 1
        print(f'[FAIL] {name}')

# ── Part 9: unit tests on _int_to_bits/_CONST_TRUE/_CONST_FALSE directly ──

alloc = _Alloc()
out = []
t1 = _CONST_TRUE(out, alloc)
t2 = _CONST_TRUE(out, alloc)
check('CONST_TRUE returns same literal on repeat', t1 == t2)
check('CONST_TRUE allocates exactly once', alloc.true_allocations == 1)
check('CONST_TRUE request count is 2', alloc.true_requests == 2)

f1 = _CONST_FALSE(out, alloc)
f2 = _CONST_FALSE(out, alloc)
check('CONST_FALSE returns same literal on repeat', f1 == f2)
check('CONST_FALSE allocates exactly once', alloc.false_allocations == 1)
check('TRUE and FALSE literals differ', t1 != f1)

# repeated identical BV constants
alloc2 = _Alloc()
out2 = []
b1 = _int_to_bits(1, 32, out2, alloc2)
b2 = _int_to_bits(1, 32, out2, alloc2)
check('repeated (bv1 32) -> identical bit literals', b1 == b2)
check('repeated (bv1 32) -> distinct list objects (no aliasing)', b1 is not b2)
check('after 2x(bv1,32): only TRUE/FALSE allocated once each',
      alloc2.true_allocations <= 1 and alloc2.false_allocations <= 1)

# width separation: same value, different width must NOT collapse to same vector
b_w8  = _int_to_bits(1, 8, out2, alloc2)
b_w16 = _int_to_bits(1, 16, out2, alloc2)
b_w32 = _int_to_bits(1, 32, out2, alloc2)
check('width 8 vector has 8 bits', len(b_w8) == 8)
check('width 16 vector has 16 bits', len(b_w16) == 16)
check('width 32 vector has 32 bits', len(b_w32) == 32)
check('bv1@8 != bv1@32 as vectors (different lengths)', b_w8 != b_w32)

# normalization: value mod 2**width
alloc3 = _Alloc()
out3 = []
b_257_8  = _int_to_bits(257, 8, out3, alloc3)   # 257 mod 256 = 1
b_1_8    = _int_to_bits(1, 8, out3, alloc3)
check('257 mod 2^8 == 1 -> same bit pattern', b_257_8 == b_1_8)

# bit pattern sanity: (_ bv5 4) = 0101 MSB-first -> [T,F,T,F]... verify count of true-bits
alloc4 = _Alloc()
out4 = []
b5_4 = _int_to_bits(5, 4, out4, alloc4)
true_count = sum(1 for lit in b5_4 if lit == alloc4.true_lit)
check('(bv5 4) has exactly two 1-bits (0101)', true_count == 2)


# ── Part 10: SAT/UNSAT correctness through the real pipeline ──

def solve(smt):
    f = parse_string(smt)
    clauses, n_vars, var_map = blast(f)
    from gansat.ns_dpll import solve_cnf
    assign = solve_cnf(clauses, n_vars)
    if assign is None:
        return 'unsat', None
    bv = reconstruct(assign, var_map)
    ok = evaluate(f, bv)
    return ('sat' if ok else 'eval_mismatch'), bv

r, m = solve("""
(set-logic QF_BV)
(declare-fun x () (_ BitVec 32))
(assert (= x (_ bv1 32)))
(check-sat)
""")
check('SAT: x = bv1 32 -> sat', r == 'sat')
check('SAT: x = bv1 32 -> model has x=1', m and m.get('x') == 1)

r, m = solve("""
(set-logic QF_BV)
(declare-fun x () (_ BitVec 32))
(assert (= x (_ bv1 32)))
(assert (= x (_ bv2 32)))
(check-sat)
""")
check('UNSAT: x=1 and x=2 -> unsat', r == 'unsat')

r, m = solve("""
(set-logic QF_BV)
(declare-fun x () (_ BitVec 8))
(assert (bvult x (_ bv10 8)))
(assert (bvugt x (_ bv5 8)))
(assert (= (bvand x (_ bv1 8)) (_ bv1 8)))
(check-sat)
""")
check('SAT: constant comparisons + bitwise + ite-adjacent', r == 'sat')

r, m = solve("""
(set-logic QF_BV)
(declare-fun x () (_ BitVec 16))
(assert (= (concat (_ bv0 8) (_ bv1 8)) x))
(check-sat)
""")
check('SAT: concat involving constants', r == 'sat' and m.get('x') == 1)

r, m = solve("""
(set-logic QF_BV)
(declare-fun x () (_ BitVec 32))
(assert (= ((_ extract 7 0) (_ bv257 32)) ((_ extract 7 0) x)))
(assert (= x (_ bv1 32)))
(check-sat)
""")
check('SAT: extract from constant matches normalized value', r == 'sat')

r, m = solve("""
(set-logic QF_BV)
(declare-fun c () Bool)
(declare-fun x () (_ BitVec 32))
(assert (= x (ite c (_ bv1 32) (_ bv2 32))))
(assert c)
(check-sat)
""")
check('SAT: ite branch with constants, c=true -> x=1', r == 'sat' and m.get('x') == 1)

print(f'\\n{passed} passed, {failed} failed')
sys.exit(1 if failed else 0)
