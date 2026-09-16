"""Exhaustive/randomized differential test for the constant-divisor
relational division/remainder encoding added to neurosym_cpp/bitblast_solver.cpp
(bv_reldiv_by_const, used by bvudiv/bvurem/bvsdiv/bvsrem when the divisor is
a compile-time constant). Run through the REAL native SMT-LIB2 parser +
bitblast_solver binary (--smtlib mode). Oracle: Python's own truncating/
floor semantics matching SMT-LIB2 bvudiv/bvurem/bvsdiv/bvsrem definitions.

Modeled on tests/test_bvmul_sparse_const.py's structure (real-binary vs.
independent-oracle, exhaustive small widths + randomized/edge wide widths).
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

def to_signed(v, w):
    if v & (1 << (w - 1)):
        return v - (1 << w)
    return v

def to_unsigned(v, w):
    return v & ((1 << w) - 1)

def _bvudiv(a, b, w):
    if b == 0:
        return (1 << w) - 1
    return a // b

def _bvurem(a, b, w):
    if b == 0:
        return a
    return a % b

def _bvsdiv(a, b, w):
    sa, sb = to_signed(a, w), to_signed(b, w)
    if sb == 0:
        return to_unsigned(-1, w) if sa >= 0 else to_unsigned(1, w)
    q = abs(sa) // abs(sb)
    if (sa < 0) != (sb < 0):
        q = -q
    return to_unsigned(q, w)

def _bvsrem(a, b, w):
    sa, sb = to_signed(a, w), to_signed(b, w)
    if sb == 0:
        return to_unsigned(sa, w)
    r = abs(sa) % abs(sb)
    if sa < 0:
        r = -r
    return to_unsigned(r, w)

OPS = {'bvudiv': _bvudiv, 'bvurem': _bvurem, 'bvsdiv': _bvsdiv, 'bvsrem': _bvsrem}

def make_case(op, w, a, b):
    expected = OPS[op](a, b, w)
    smt_pos = f"""(set-logic QF_BV)
(declare-fun x () (_ BitVec {w}))
(declare-fun r () (_ BitVec {w}))
(assert (= x (_ bv{a} {w})))
(assert (= r ({op} x (_ bv{b} {w}))))
(assert (= r (_ bv{expected} {w})))
(check-sat)
"""
    smt_neg = smt_pos.replace(f"(assert (= r (_ bv{expected} {w})))", f"(assert (not (= r (_ bv{expected} {w}))))")
    return (op, w, a, b, expected, smt_pos, smt_neg)

def eval_case(case):
    op, w, a, b, expected, smt_pos, smt_neg = case
    r1 = run(smt_pos)
    r2 = run(smt_neg)
    ok1 = r1.startswith('sat')
    ok2 = r2.startswith('unsat')
    return (op, w, a, b, expected, ok1, ok2)

jobs = []
# Exhaustive over small widths for every op, all a/b pairs (covers b==0,
# b==1, pow2 b, sparse/dense non-pow2 b, and every a magnitude/sign combo).
for op in OPS:
    for w in (2, 3, 4, 5):
        for a in range(1 << w):
            for b in range(1 << w):
                jobs.append(make_case(op, w, a, b))

# Randomized + edge cases at real ESBMC-relevant width (32), biased toward
# the small-popcount non-pow2 constants seen in the real RERS corpus
# (3, 5, 7, 9, 10, ...) plus dense/negative/edge divisors.
random.seed(20260916)
w = 32
mask = (1 << w) - 1
edge_consts = [1, mask, 3, 5, 7, 9, 10, 100, 1000, (mask - 4) & mask,
               1 << (w - 1), 0xAAAAAAAA & mask, 0x55555555 & mask]
for op in OPS:
    for c in edge_consts:
        for _ in range(6):
            a = random.randint(0, mask)
            jobs.append(make_case(op, w, a, c))
    for _ in range(30):
        a = random.randint(0, mask)
        b = random.randint(1, mask)  # nonzero, exercises the new code path
        jobs.append(make_case(op, w, a, b))

print(f"running {len(jobs)} cases...")
passed = failed = 0
with ThreadPoolExecutor(max_workers=16) as ex:
    futs = [ex.submit(eval_case, j) for j in jobs]
    for fut in as_completed(futs):
        op, w, a, b, expected, ok1, ok2 = fut.result()
        if ok1 and ok2:
            passed += 1
        else:
            failed += 1
            print(f"[FAIL] {op} w={w} a={a} b={b} expected={expected} pos_sat={ok1} neg_unsat={ok2}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
