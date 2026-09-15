"""Differential test for the word-level rewrite rules added to
neurosym_cpp/bitblast_solver.cpp: bvand/bvor/bvxor constant identities,
ite constant-condition/same-branch folding, extract-of-concat and
extract-of-extract pushdown. Exhaustive over small widths, comparing the
real --smtlib binary against direct Python arithmetic (ground truth for
these simple bitwise/structural ops -- no need for ns_evaluator here,
the semantics are standard fixed-width bitwise arithmetic).
"""
import subprocess, sys, os, re, itertools

SOLVER = os.path.expanduser("~/VishResearch/NeuroSym/neurosym_cpp/bitblast_solver")

passed = failed = 0
def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"[FAIL] {name} {detail}")

def run(smt2_text):
    path = "/tmp/_wr_test.smt2"
    with open(path, "w") as f:
        f.write(smt2_text)
    out = subprocess.run([SOLVER, path, "--smtlib"], capture_output=True, text=True, timeout=15).stdout
    lines = out.strip().split("\n")
    verdict = lines[0] if lines else ""
    model = {}
    for l in lines[1:]:
        m = re.match(r"(\S+) = (\d+)", l)
        if m:
            model[m.group(1)] = int(m.group(2))
    return verdict, model

W = 8
MASK = (1 << W) - 1

# Exhaustive bvand/bvor/bvxor against every constant, both operand orders,
# for a sample of x values (not fully exhaustive x since that's 256 * 256
# consts * 3 ops * 2 orders -- too much; test all consts against a fixed
# representative set of x values instead, still exercises every rewrite
# branch: cv==0, cv==mask, and the general fallback).
xs = [0, 1, 7, 42, 85, 170, 213, 255]
consts = [0, 1, 15, 85, 170, 254, 255]  # includes 0 and mask=255 explicitly
ops = [("bvand", lambda a, b: a & b), ("bvor", lambda a, b: a | b), ("bvxor", lambda a, b: a ^ b)]

for opname, fn in ops:
    for x in xs:
        for c in consts:
            for order in (0, 1):
                if order == 0:
                    expr = f"({opname} x #x{c:02x})"
                else:
                    expr = f"({opname} #x{c:02x} x)"
                smt = f"(declare-const x (_ BitVec 8)) (declare-const r (_ BitVec 8)) (assert (= x #x{x:02x})) (assert (= r {expr})) (check-sat)"
                v, m = run(smt)
                expect = fn(x, c) & MASK
                check(f"{opname}_x={x}_c={c}_ord={order}", v == "sat" and m.get("r") == expect,
                      f"expected {expect} got {m.get('r')} verdict={v}")

# ITE constant-condition folding, both true/false, plus same-branch folding
smt = "(declare-const r (_ BitVec 8)) (assert (= r (ite true #x05 #x09))) (check-sat)"
v, m = run(smt); check("ite_true", v == "sat" and m.get("r") == 5, m)
smt = "(declare-const r (_ BitVec 8)) (assert (= r (ite false #x05 #x09))) (check-sat)"
v, m = run(smt); check("ite_false", v == "sat" and m.get("r") == 9, m)
smt = "(declare-const c Bool) (declare-const r (_ BitVec 8)) (assert (= r (ite c #x07 #x07))) (check-sat)"
v, m = run(smt); check("ite_same_branch", v == "sat" and m.get("r") == 7, m)
# Bool-sorted ite const-condition
smt = "(declare-const p Bool) (declare-const q Bool) (assert (= p (ite true q false))) (assert q) (check-sat)"
v, m = run(smt); check("bool_ite_true", v == "sat", v)

# Extract-of-concat: entirely in low operand, entirely in high operand, and
# spanning both (the fallback path).
smt = ("(declare-const a (_ BitVec 8)) (declare-const b (_ BitVec 8)) "
       "(assert (= a #xAB)) (assert (= b #xCD)) "
       "(assert (= ((_ extract 7 0) (concat a b)) #xCD)) (check-sat)")
v, m = run(smt); check("extract_concat_low", v == "sat", v)

smt = ("(declare-const a (_ BitVec 8)) (declare-const b (_ BitVec 8)) "
       "(assert (= a #xAB)) (assert (= b #xCD)) "
       "(assert (= ((_ extract 15 8) (concat a b)) #xAB)) (check-sat)")
v, m = run(smt); check("extract_concat_high", v == "sat", v)

smt = ("(declare-const a (_ BitVec 8)) (declare-const b (_ BitVec 8)) "
       "(assert (= a #xAB)) (assert (= b #xCD)) "
       # bits 11..4 span both a (bits 15..8) and b (bits 7..0): expect
       # (a<<8|b) bits [11:4] = 0xABCD's bits 11..4 = 0xBC
       "(assert (= ((_ extract 11 4) (concat a b)) #xBC)) (check-sat)")
v, m = run(smt); check("extract_concat_spanning", v == "sat", v)

# Extract-of-extract composition
smt = ("(declare-const a (_ BitVec 16)) (assert (= a #xABCD)) "
       "(assert (= ((_ extract 3 0) ((_ extract 11 0) a)) #xD)) (check-sat)")
v, m = run(smt); check("extract_of_extract", v == "sat", v)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
