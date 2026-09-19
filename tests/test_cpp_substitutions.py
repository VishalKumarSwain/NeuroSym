"""Differential checks for the C++ substitution pass; Z3 is the oracle."""
import os, pathlib, random, subprocess, sys, tempfile
import z3

root = pathlib.Path(__file__).resolve().parent
solver = os.environ.get('NEUROSYM_CPP_SOLVER', str(root.parent / 'neurosym_cpp' / 'bitblast_solver'))
workspace = tempfile.TemporaryDirectory(prefix='neurosym_cpp_check_')
work = pathlib.Path(workspace.name)
rng = random.Random(20260919)
cases = []
for case in range(300):
    width = rng.choice([4, 8, 16])
    xs = [z3.BitVec(f'x{i}', width) for i in range(rng.randrange(3, 18))]
    constraints = []
    for i, x in enumerate(xs):
        choices = xs if case % 3 == 0 else xs[:i]
        a = rng.choice(choices) if choices else z3.BitVecVal(rng.randrange(16), width)
        b = rng.choice(choices) if choices and rng.random() < .4 else z3.BitVecVal(rng.randrange(16), width)
        expr = rng.choice([a + b, a - b, a ^ b, a & b, a | b, z3.If(a < b, a, b)])
        if rng.random() < .85:
            constraints.append(x == expr)
    constraints.append(xs[-1] == rng.randrange(16))
    if case % 5 == 0:
        constraints.extend([xs[0] == 1, xs[0] == 2])
    cases.append((f'random_{case}', constraints, xs))

# Long shared DAG, cyclic definitions, Boolean substitutions and arrays.
xs = [z3.BitVec(f'v{i}', 8) for i in range(600)]
chain = [xs[0] == 1] + [xs[i] == xs[i-1] + 1 for i in range(1, len(xs))]
cases += [('long_sat', chain + [xs[-1] == 600 % 256], xs),
          ('long_unsat', chain + [xs[-1] != 600 % 256], xs)]
a,b,c = z3.BitVecs('a b c', 8)
cases += [('cycle_sat', [a == b+1, b == c+1, c == a-2], [a,b,c]),
          ('cycle_unsat', [a == b+1, b == c+1, c == a+1], [a,b,c])]
p,q,r = z3.Bools('p q r')
cases += [('bool_cycle_unsat', [p == z3.Not(q), q == r, r == p], [p,q,r]),
          ('bool_cycle_sat', [p == z3.Not(q), q == z3.Not(r), r == p], [p,q,r])]
arr = z3.Array('arr', z3.BitVecSort(8), z3.BitVecSort(8))
arr2 = z3.Array('arr2', z3.BitVecSort(8), z3.BitVecSort(8))
cases += [('array_store_sat', [z3.Select(z3.Store(arr, a, b), a) == b], [a,b]),
          ('array_store_unsat', [z3.Select(z3.Store(arr, a, b), a) != b], [a,b])]
failures = []
for name, assertions, variables in cases:
    oracle = z3.Solver(); oracle.add(assertions)
    expected = str(oracle.check())
    path = work / 'check_input.smt2'; path.write_text(oracle.to_smt2())
    run = subprocess.run([solver, str(path)], capture_output=True, text=True, timeout=20)
    lines = run.stdout.splitlines()
    actual = lines[0] if lines else 'error'
    issue = None
    if actual != expected:
        issue = f'verdict {actual}, expected {expected}: {run.stderr}'
    elif actual == 'sat':
        vals = dict(line.split(' = ', 1) for line in lines[1:] if ' = ' in line)
        for var in variables:
            val = vals.get(str(var), '<unreferenced>')
            if val == '<unreferenced>': continue
            oracle.add(var == (val == 'true' if z3.is_bool(var) else int(val)))
        if oracle.check() != z3.sat:
            issue = 'reported model cannot satisfy original assertions'
    if issue:
        failures.append((name, issue)); (work / (name+'.smt2')).write_text(oracle.to_smt2())
        print('FAIL',name,issue,flush=True)
print(f'{len(cases)-len(failures)}/{len(cases)} passed; {len(failures)} failures',flush=True)
sys.exit(bool(failures))
