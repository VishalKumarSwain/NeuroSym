import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gansat import ns_minisat, ns_minisat_native
from gansat.ns_bitblaster import blast, reconstruct
from gansat.ns_parser import parse_file, parse_string
from gansat.ns_evaluator import evaluate

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond:
        passed += 1; print(f'[PASS] {name}')
    else:
        failed += 1; print(f'[FAIL] {name}')

check('native shim available', ns_minisat_native.available())
check('subprocess minisat available', ns_minisat.available())

def cmp_backends(clauses, n_vars, label):
    r1 = ns_minisat.solve_cnf(clauses, n_vars)
    r2 = ns_minisat_native.solve_cnf(clauses, n_vars, use_simp=False)
    r3 = ns_minisat_native.solve_cnf(clauses, n_vars, use_simp=True)
    same_verdict = (r1 is None) == (r2 is None) == (r3 is None)
    check(f'{label}: subprocess/native-Solver/native-SimpSolver agree', same_verdict)
    return r1, r2, r3

# Elimination-triggering cases: chains of equivalent variables and a
# variable that appears in only two clauses in exactly the pattern
# variable elimination resolves away (x <-> y via (−x∨y)∧(x∨−y), then x
# used nowhere else -- SimpSolver should be able to eliminate x entirely).
cmp_backends([[-1, 2], [1, -2], [2, 3]], 3, 'equivalent variables (x<->y chain)')
cmp_backends([[-1, 2], [1, -2], [-2, 3], [2, -3]], 3, 'chained equivalences (sat)')
# pure literal: variable 2 only ever appears positively
cmp_backends([[1, 2], [3, 2], [-1, -3]], 3, 'pure literal (var 2 only positive)')

# unit clauses
cmp_backends([[1]], 1, 'unit clause (sat)')
cmp_backends([[1], [-1]], 1, 'unit clauses conflicting (unsat)')
# binary
cmp_backends([[1, 2], [-1, 2]], 2, 'binary clauses (sat)')
# empty clause -> unsat
cmp_backends([[]], 1, 'empty clause (unsat)')
# single var, no clauses referencing it beyond trivial
cmp_backends([[1, -1]], 1, 'tautology clause (sat)')
# negative literals only
cmp_backends([[-1, -2], [1, 2]], 2, 'negative literals mixed (sat)')
# many vars
big_clauses = [[i, -(i+1)] for i in range(1, 200, 2)]
cmp_backends(big_clauses, 201, 'many variables (sat)')
# unused variable (n_vars larger than referenced)
cmp_backends([[1, 2]], 10, 'unused variables beyond referenced (sat)')

# Real formulas: bitblast ONCE, feed identical CNF to all three backends
for path in ('/tmp/wtest25.smt2', '/tmp/prob1.smt2'):
    f = parse_file(path)
    clauses, n_vars, var_map = blast(f)
    r1 = ns_minisat.solve_cnf(clauses, n_vars)
    r2 = ns_minisat_native.solve_cnf(clauses, n_vars, use_simp=False)
    r3 = ns_minisat_native.solve_cnf(clauses, n_vars, use_simp=True)
    check(f'{path}: all three backends agree on SAT/UNSAT',
          (r1 is None) == (r2 is None) == (r3 is None))
    for name, r in (('subprocess', r1), ('native-Solver', r2), ('native-SimpSolver', r3)):
        if r is not None:
            check(f'{path}: {name} model verifies', evaluate(f, reconstruct(r, var_map)))

print(f'\\n{passed} passed, {failed} failed')
sys.exit(1 if failed else 0)
