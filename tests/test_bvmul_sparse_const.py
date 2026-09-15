"""Exhaustive/randomized differential test for the bv_mul_by_sparse_const
strength reduction in neurosym_cpp/bitblast_solver.cpp (multiply-by-known-
constant via shift-add decomposition, replacing the general O(w^2)
schoolbook bv_mul whenever one operand is a compile-time constant). Run
through the REAL native SMT-LIB2 parser + bitblast_solver binary
(--smtlib mode), not a mocked/Python path. Oracle: gansat/ns_evaluator.py's
evaluate() semantics for bvmul (== Python (a*b) mod 2**w).

Modeled directly on the now-reverted tests/test_relational_division.py's
structure (same real-binary/oracle-comparison approach), since the multiply
strength reduction lives in the same file and needs the same rigor.
"""
import sys, os, random, subprocess, tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO = os.path.expanduser("~/VishResearch/NeuroSym")
SOLVER = os.path.join(REPO, "neurosym_cpp", "bitblast_solver")

def run(smt):
    fd, path = tempfile.mkstemp(suffix='.smt2')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(smt)
        out = subprocess.run([SOLVER, path, "--smtlib"], capture_output=True, text=True, timeout=30)
        return out.stdout.strip()
    finally:
        os.unlink(path)

def expected_mul(a, b, w):
    return (a * b) % (1 << w)

def make_case(w, a, b, const_on_left):
    expected = expected_mul(a, b, w)
    if const_on_left:
        expr = f"(bvmul (_ bv{a} {w}) y)"
        bind = f"(assert (= y (_ bv{b} {w})))"
    else:
        expr = f"(bvmul x (_ bv{b} {w}))"
        bind = f"(assert (= x (_ bv{a} {w})))"
    smt_pos = f"""(set-logic QF_BV)
(declare-fun x () (_ BitVec {w}))
(declare-fun y () (_ BitVec {w}))
(declare-fun r () (_ BitVec {w}))
{bind}
(assert (= r {expr}))
(assert (= r (_ bv{expected} {w})))
(check-sat)
"""
    smt_neg = smt_pos.replace(f"(assert (= r (_ bv{expected} {w})))", f"(assert (not (= r (_ bv{expected} {w}))))")
    return (w, a, b, const_on_left, expected, smt_pos, smt_neg)

def eval_case(case):
    w, a, b, const_on_left, expected, smt_pos, smt_neg = case
    r1 = run(smt_pos)
    r2 = run(smt_neg)
    ok1 = r1.startswith('sat')
    ok2 = r2.startswith('unsat')
    return (w, a, b, const_on_left, expected, ok1, ok2)

jobs = []
# Exhaustive over small widths (both operand orders): covers every popcount
# shape (0, 1/pow2, sparse, dense, all-ones) for w=2..6.
for w in (2, 3, 4, 5, 6):
    for a in range(1 << w):
        for b in range(1 << w):
            jobs.append(make_case(w, a, b, True))
            jobs.append(make_case(w, a, b, False))

# Randomized at real ESBMC-relevant width (32), biased toward the sparse
# small-popcount constants actually seen in the RERS corpus (2,3,5,9,10,
# ...) plus fully random 32-bit constants for broader coverage, and a few
# adversarial dense/edge constants (0, 1, all-ones, MSB-only, alternating).
random.seed(20260915)
w = 32
mask = (1 << w) - 1
edge_consts = [0, 1, mask, 1 << (w - 1), 0xAAAAAAAA & mask, 0x55555555 & mask,
               5, 10, 3, 9, 6, 12, 100, 1000, (mask - 4) & mask]
for c in edge_consts:
    a = random.randint(0, mask)
    jobs.append(make_case(w, a, c, False))
    jobs.append(make_case(w, c, a, True))
for _ in range(300):
    a = random.randint(0, mask)
    b = random.randint(0, mask)
    jobs.append(make_case(w, a, b, random.random() < 0.5))

print(f"Running {len(jobs)} bvmul-by-constant differential cases against real bitblast_solver binary...")
passed = failed = 0
fail_examples = []
with ThreadPoolExecutor(max_workers=32) as ex:
    futs = [ex.submit(eval_case, j) for j in jobs]
    for fut in as_completed(futs):
        w, a, b, const_on_left, expected, ok1, ok2 = fut.result()
        if ok1 and ok2:
            passed += 1
        else:
            failed += 1
            fail_examples.append((w, a, b, const_on_left, expected, ok1, ok2))

print(f"[bvmul sparse-const] passed={passed} failed={failed}")
for ex_ in fail_examples[:20]:
    print(f"  [FAIL] {ex_}")

if failed:
    sys.exit(1)
