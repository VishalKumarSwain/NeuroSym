"""Differential/regression test for deep store chains against a single
array, targeting the recursion-depth fix in gansat/ns_bitblaster.py's
_blast_select (store/ite chain walk made iterative instead of recursive).

For each depth D, builds:
    arr0 : (Array (BitVec 32) (BitVec 8))
    arrD = store(store(...store(arr0, 0, v0)..., D-2, vD-2), D-1, vD-1)
    assert (select arrD (D-1)) == vD-1     # last store -- shallowest peel
    assert (select arrD (D//2)) == v(D//2) # a mid-chain read -- must walk
                                            # partway down the whole chain
and confirms the solver: (a) does not crash, (b) returns sat, (c) the
reconstructed model satisfies the formula per ns_evaluator.evaluate()
(the pre-existing, independent semantic reference), same rigor as
tests/test_bv_division.py.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Match main.py's own setup: the still-recursive parts of the pipeline that
# this task did not target (e.g. ns_evaluator._eval, used here only as the
# independent verification oracle) rely on the same generous limit
# production runs already carry.
sys.setrecursionlimit(100000)
from gansat.ns_bitblaster import blast, reconstruct
from gansat.ns_dpll import solve_cnf
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

def gen_smt(depth):
    # Each store gets its own let binding (?s0, ?s1, ...) -- this mirrors
    # how real ESBMC/boolector-style encoders structure-share array terms
    # in practice (as seen in the captured Prob13 formula), and it is what
    # exercises the *parser's* iterative let-chain walk. Parsing still
    # builds a genuinely deep nested App tree underneath (each ?sI resolves
    # to the previously-built store App object) -- so this equally stresses
    # the bit-blaster's now-iterative _blast_select walk over that tree.
    decls = "(declare-const arr0 (Array (_ BitVec 32) (_ BitVec 8)))"
    opens = []
    prev = "arr0"
    for i in range(depth):
        val = (i * 7 + 3) % 256
        name = f"?s{i}"
        opens.append(f"(let (({name} (store {prev} (_ bv{i} 32) (_ bv{val} 8))))")
        prev = name
    last_val = ((depth - 1) * 7 + 3) % 256
    mid = depth // 2
    mid_val = (mid * 7 + 3) % 256
    body = (
        f"(and (= (select {prev} (_ bv{depth-1} 32)) (_ bv{last_val} 8))"
        f" (= (select {prev} (_ bv{mid} 32)) (_ bv{mid_val} 8)))"
    )
    closes = ")" * len(opens)
    assertion = f"(assert {''.join(opens)}{body}{closes})"
    return "\n".join([decls, assertion, "(check-sat)"])

def run_depth(depth):
    smt = gen_smt(depth)
    t0 = time.time()
    f = parse_string(smt)
    clauses, n_vars, var_map = blast(f)
    assign = solve_cnf(clauses, n_vars)
    dt = time.time() - t0
    if assign is None:
        check(f'depth={depth} -> sat (got unsat)', False)
        return
    m = reconstruct(assign, var_map)
    ok = evaluate(f, m)
    check(f'depth={depth} -> sat and model verifies via ns_evaluator ({dt:.2f}s)', ok)

for depth in [10, 100, 1000, 10000]:
    run_depth(depth)

print(f'\\n{passed} passed, {failed} failed')
sys.exit(1 if failed else 0)
