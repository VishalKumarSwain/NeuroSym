"""Exhaustive/randomized differential test for the relational (CBMC-style)
bv_udivrem encoding in neurosym_cpp/bitblast_solver.cpp, run through the
REAL native SMT-LIB2 parser + bitblast_solver binary (--smtlib mode), not
a mocked/Python path. Oracle: gansat/ns_evaluator.py's evaluate().
Parallelized with a thread pool (subprocess spawns are I/O-bound) since
this machine has 128 cores.
"""
import sys, os, random, subprocess, tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO = os.path.expanduser("~/VishResearch/NeuroSym")
sys.path.insert(0, REPO)
from gansat.ns_evaluator import _bvudiv, _bvurem, _bvsdiv, _bvsrem

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

def make_case(op, w, a, b):
    expected = {'bvudiv': _bvudiv, 'bvurem': _bvurem, 'bvsdiv': _bvsdiv, 'bvsrem': _bvsrem}[op](a, b, w)
    smt_pos = f"""(set-logic QF_BV)
(declare-fun x () (_ BitVec {w}))
(declare-fun y () (_ BitVec {w}))
(declare-fun r () (_ BitVec {w}))
(assert (= x (_ bv{a} {w})))
(assert (= y (_ bv{b} {w})))
(assert (= r ({op} x y)))
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
for w in (2, 3, 4, 5, 6):
    for a in range(1 << w):
        for b in range(1 << w):
            for op in ('bvudiv', 'bvurem', 'bvsdiv', 'bvsrem'):
                jobs.append(make_case(op, w, a, b))

def is_pow2(v):
    return v != 0 and (v & (v - 1)) == 0

random.seed(12345)
wide_jobs = []
for w in (16, 32, 64):
    for op in ('bvudiv', 'bvurem', 'bvsdiv', 'bvsrem'):
        n = 0
        attempts = 0
        while n < 1000 and attempts < 20000:
            attempts += 1
            a = random.randrange(0, 1 << w)
            b = random.randrange(0, 1 << w)
            if b != 0 and is_pow2(b):
                continue
            wide_jobs.append(make_case(op, w, a, b))
            n += 1

print(f'exhaustive jobs: {len(jobs)}  wide random jobs: {len(wide_jobs)}  total: {len(jobs)+len(wide_jobs)}')
sys.stdout.flush()

passed = failed = 0
fails = []

def run_batch(batch, label):
    global passed, failed
    with ThreadPoolExecutor(max_workers=64) as ex:
        futs = [ex.submit(eval_case, c) for c in batch]
        done = 0
        for fut in as_completed(futs):
            op, w, a, b, expected, ok1, ok2 = fut.result()
            done += 1
            if ok1:
                passed += 1
            else:
                failed += 1
                fails.append(f'{op} w={w} {a}/{b} pos-case FAIL expected r={expected}')
            if ok2:
                passed += 1
            else:
                failed += 1
                fails.append(f'{op} w={w} {a}/{b} neg-case FAIL expected r!={expected}')
            if done % 2000 == 0:
                print(f'  [{label}] {done}/{len(batch)} done, running totals {passed}p/{failed}f')
                sys.stdout.flush()

run_batch(jobs, 'exhaustive')
print(f'exhaustive (w2-6) done: {passed} passed, {failed} failed')
sys.stdout.flush()
run_batch(wide_jobs, 'wide-random')
print(f'TOTAL: {passed} passed, {failed} failed')
if failed:
    print("FAILURES (first 50):")
    for x in fails[:50]:
        print(' ', x)
    sys.exit(1)
else:
    print("ALL PASSED")
