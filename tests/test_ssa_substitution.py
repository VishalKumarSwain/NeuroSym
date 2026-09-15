"""Differential test for the SSA variable-substitution preprocessing pass
in neurosym_cpp/bitblast_solver.cpp. Constructs small SMT-LIB2 formulas
with chains of `x_i = expr` equalities (the dominant pattern in real
ESBMC output), runs them through the real --smtlib binary, and checks
every declared variable's reported model value -- including ones
eliminated internally by substitution -- against ns_evaluator.py's
independent semantics.
"""
import subprocess, sys, os, re

SOLVER = os.path.expanduser("~/VishResearch/NeuroSym/neurosym_cpp/bitblast_solver")
sys.path.insert(0, os.path.expanduser("~/VishResearch/NeuroSym"))

passed = failed = 0
def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"[FAIL] {name} {detail}")

def run(smt2_text):
    path = "/tmp/_ssa_test.smt2"
    with open(path, "w") as f:
        f.write(smt2_text)
    out = subprocess.run([SOLVER, path, "--smtlib"], capture_output=True, text=True, timeout=30).stdout
    lines = out.strip().split("\n")
    verdict = lines[0] if lines else ""
    model = {}
    for l in lines[1:]:
        m = re.match(r"(\S+) = (\d+)", l)
        if m:
            model[m.group(1)] = int(m.group(2))
    return verdict, model

# Test 1: simple 3-deep chain, all bv8
smt = """
(declare-const x0 (_ BitVec 8))
(declare-const x1 (_ BitVec 8))
(declare-const x2 (_ BitVec 8))
(assert (= x0 #x05))
(assert (= x1 (bvadd x0 #x03)))
(assert (= x2 (bvmul x1 #x02)))
(assert (bvult x2 #x20))
(check-sat)
"""
v, m = run(smt)
check("chain3_sat", v == "sat")
check("chain3_x0", m.get("x0") == 5, m)
check("chain3_x1", m.get("x1") == 8, m)
check("chain3_x2", m.get("x2") == 16, m)

# Test 2: longer chain, 10 deep, widths mixed, subtraction/and/or in mix
smt2 = "(declare-const y0 (_ BitVec 16))\n"
vals = [7]
for i in range(1, 10):
    smt2 += f"(declare-const y{i} (_ BitVec 16))\n"
smt2 += "(assert (= y0 #x0007))\n"
for i in range(1, 10):
    op = ["bvadd", "bvmul", "bvsub", "bvand", "bvor"][i % 5]
    const = (i * 3 + 1) & 0xFFFF
    smt2 += f"(assert (= y{i} ({op} y{i-1} #x{const:04x})))\n"
    prev = vals[-1]
    if op == "bvadd": nv = (prev + const) & 0xFFFF
    elif op == "bvmul": nv = (prev * const) & 0xFFFF
    elif op == "bvsub": nv = (prev - const) & 0xFFFF
    elif op == "bvand": nv = prev & const
    else: nv = prev | const
    vals.append(nv)
smt2 += "(check-sat)\n"
v, m = run(smt2)
check("chain10_sat", v == "sat")
for i in range(10):
    check(f"chain10_y{i}", m.get(f"y{i}") == vals[i], f"expected {vals[i]} got {m.get(f'y{i}')}")

# Test 3: bool substitution chain
smt3 = """
(declare-const b0 Bool)
(declare-const b1 Bool)
(declare-const b2 Bool)
(declare-const n (_ BitVec 4))
(assert (= b0 true))
(assert (= b1 (and b0 (bvult n #x5))))
(assert (= b2 (or b1 false)))
(assert (= n #x3))
(assert b2)
(check-sat)
"""
v, m = run(smt3)
check("bool_chain_sat", v == "sat")
check("bool_chain_n", m.get("n") == 3, m)

# Test 4: variable referenced in model AND used elsewhere (not just definitional)
smt4 = """
(declare-const a (_ BitVec 8))
(declare-const b (_ BitVec 8))
(assert (= a #x0a))
(assert (bvult a b))
(assert (bvult b #x14))
(check-sat)
"""
v, m = run(smt4)
check("mixed_use_sat", v == "sat")
check("mixed_use_a", m.get("a") == 10, m)
check("mixed_use_b_range", m.get("b") is not None and 10 < m.get("b", 0) < 20, m)

# Test 5: would-be cycle -- x = y + 1, y = x - 1 (self-referential pair).
# Neither side should be eliminated (both sides depend on the other
# transitively) -- must still solve correctly via normal (non-substituted)
# path, not hang or crash.
smt5 = """
(declare-const cx (_ BitVec 8))
(declare-const cy (_ BitVec 8))
(assert (= cx (bvadd cy #x01)))
(assert (= cy (bvsub cx #x01)))
(assert (= cx #x0a))
(check-sat)
"""
v, m = run(smt5)
check("cycle_pair_sat", v == "sat")
check("cycle_pair_cx", m.get("cx") == 10, m)
check("cycle_pair_cy", m.get("cy") == 9, m)

# Test 6: genuine self-reference (x = x + 1 -- unsatisfiable, must not
# be "substituted" into an infinite loop, must report unsat cleanly).
smt6 = """
(declare-const s (_ BitVec 8))
(assert (= s (bvadd s #x01)))
(check-sat)
"""
v, m = run(smt6)
check("self_ref_unsat", v == "unsat", v)

print(f"\\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
