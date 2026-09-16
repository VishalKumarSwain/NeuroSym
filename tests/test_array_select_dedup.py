"""Differential test for the literal-index-identity fast path added to
blastSelect() in neurosym_cpp/bitblast_solver.cpp: when two array reads
happen to blast to bit-for-bit identical index literals, the second one
aliases the first's result instead of redoing the store-chain walk and
consistency loop. Exercises: repeated reads at the same literal index,
repeated reads at the same SYMBOLIC index (via a shared variable),
reads at genuinely different indices (make sure dedup never wrongly
merges distinct indices), and store-chain interaction (dedup across a
store boundary must still see the correct post-store value). Run
through the REAL native SMT-LIB2 parser + bitblast_solver binary.
"""
import subprocess, sys, os, re

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
    path = "/tmp/_arrdedup_test.smt2"
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

# 1. Same literal-constant index read twice from an unconstrained array:
# both reads must agree (sat), and forcing them to differ must be unsat.
smt = """
(declare-const a (Array (_ BitVec 8) (_ BitVec 8)))
(declare-const r1 (_ BitVec 8))
(declare-const r2 (_ BitVec 8))
(assert (= r1 (select a #x05)))
(assert (= r2 (select a #x05)))
(assert (not (= r1 r2)))
(check-sat)
"""
v, m = run(smt)
check("same_const_index_must_agree_unsat", v == "unsat", v)

smt2 = """
(declare-const a (Array (_ BitVec 8) (_ BitVec 8)))
(declare-const r1 (_ BitVec 8))
(declare-const r2 (_ BitVec 8))
(assert (= r1 (select a #x05)))
(assert (= r2 (select a #x05)))
(assert (= r1 #x2A))
(check-sat)
"""
v, m = run(smt2)
check("same_const_index_agrees_sat", v == "sat" and m.get("r2") == 42, m)

# 2. Same SYMBOLIC index (a shared variable) read via two different select
# node instances -- these will generally have DIFFERENT idxNode ids in the
# IR (two separate `select` terms) but blast to the SAME idxBits (since
# both reference the same variable node), so this exercises the actual
# literal-identity dedup path, not just the outer bvCache.
smt3 = """
(declare-const a (Array (_ BitVec 8) (_ BitVec 8)))
(declare-const i (_ BitVec 8))
(declare-const r1 (_ BitVec 8))
(declare-const r2 (_ BitVec 8))
(assert (= r1 (select a i)))
(assert (= r2 (select a i)))
(assert (not (= r1 r2)))
(check-sat)
"""
v, m = run(smt3)
check("same_symbolic_index_must_agree_unsat", v == "unsat", v)

# 3. Genuinely DIFFERENT indices must NOT be aliased -- forcing them equal
# and forcing the values to differ must be sat (array is otherwise free),
# and reading at i vs j with i != j enforced, values CAN differ.
smt4 = """
(declare-const a (Array (_ BitVec 8) (_ BitVec 8)))
(declare-const r1 (_ BitVec 8))
(declare-const r2 (_ BitVec 8))
(assert (= r1 (select a #x03)))
(assert (= r2 (select a #x07)))
(assert (= r1 #x11))
(assert (= r2 #x22))
(check-sat)
"""
v, m = run(smt4)
check("different_const_indices_can_differ_sat", v == "sat" and m.get("r1") == 17 and m.get("r2") == 34, m)

# 4. Store-chain interaction: read at the SAME index both before and after
# a store to a DIFFERENT index -- must see the same (unaffected) value both
# times despite the dedup fast path, since the two selects are against
# array expressions separated by an intervening store.
smt5 = """
(declare-const a (Array (_ BitVec 8) (_ BitVec 8)))
(declare-const b (Array (_ BitVec 8) (_ BitVec 8)))
(declare-const r1 (_ BitVec 8))
(declare-const r2 (_ BitVec 8))
(assert (= b (store a #x09 #x55)))
(assert (= r1 (select a #x05)))
(assert (= r2 (select b #x05)))
(assert (not (= r1 r2)))
(check-sat)
"""
v, m = run(smt5)
check("read_same_index_across_unrelated_store_must_agree_unsat", v == "unsat", v)

# 5. Read at the index that WAS stored to, before and after -- must differ
# correctly (post-store value must be the stored constant).
smt6 = """
(declare-const a (Array (_ BitVec 8) (_ BitVec 8)))
(declare-const b (Array (_ BitVec 8) (_ BitVec 8)))
(declare-const r2 (_ BitVec 8))
(assert (= b (store a #x09 #x55)))
(assert (= r2 (select b #x09)))
(check-sat)
"""
v, m = run(smt6)
check("read_stored_index_gets_stored_value", v == "sat" and m.get("r2") == 0x55, m)

# 6. Many repeated reads at the same symbolic index in a loop-like pattern
# (stresses the dedup path being hit repeatedly, not just once).
smt7 = "(declare-const a (Array (_ BitVec 8) (_ BitVec 8)))\n(declare-const i (_ BitVec 8))\n"
for k in range(20):
    smt7 += f"(declare-const r{k} (_ BitVec 8))\n(assert (= r{k} (select a i)))\n"
smt7 += "(assert (= r0 #x07))\n(check-sat)\n"
v, m = run(smt7)
ok = v == "sat" and all(m.get(f"r{k}") == 7 for k in range(20))
check("many_repeated_symbolic_reads_all_agree", ok, m)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
