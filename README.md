# NeuroSym — SMT Solver

> A standalone C++ QF_BV SMT solver, integrated with ESBMC as an alternate solver backend.

NeuroSym is a native C++ SMT-LIB2 solver: its own parser, Tseitin bit-blasting to CNF, and native MiniSat (SimpSolver) solving. It integrates into ESBMC via a `--neurosym-prog` backend, giving ESBMC an additional solver option alongside Z3, Boolector, CVC5, and Bitwuzla.

---

## Build

```bash
cd neurosym_cpp
g++ -O2 -std=c++17 -o bitblast_solver bitblast_solver.cpp -lminisat
```

Requires MiniSat's headers and shared library (`libminisat.so.2` or equivalent) available on the build system. The binary must be built at `neurosym_cpp/bitblast_solver`, right next to `bitblast_solver.cpp` — the wrapper scripts below locate it relative to their own location, not a hardcoded path.

---

## Usage

### Standalone (SMT-LIB2 file)

```bash
./neurosym_cpp/bitblast_solver formula.smt2
```

### As an ESBMC backend

Symlink the wrapper scripts onto your `$PATH`:

```bash
mkdir -p ~/bin
ln -s "$(pwd)/neurosym_cpp/bin/neurosym-cpp-solve" ~/bin/neurosym-cpp-solve
ln -s "$(pwd)/neurosym_cpp/bin/esbmc-neurosym-cpp" ~/bin/esbmc-neurosym-cpp
export PATH="$HOME/bin:$PATH"   # add to ~/.bashrc to persist
```

Use symlinks, not copies — a plain copy loses the link back to this checkout and the scripts won't find `bitblast_solver` anymore.

Then run:

```bash
esbmc --neurosym program.c
```

If your ESBMC build doesn't already default `--neurosym-prog` to `neurosym-cpp-solve`, pass it explicitly:

```bash
esbmc --neurosym --neurosym-prog "$HOME/bin/neurosym-cpp-solve %f" program.c
```

For just the SAT/UNSAT verdict without building a full counterexample trace:

```bash
esbmc --neurosym --neurosym-prog "$HOME/bin/neurosym-cpp-solve %f" --result-only program.c
```

Full counterexample-trace building for formulas involving sign/zero-extend still requires a live SMT-LIB2 solver for model completion (a gap in ESBMC's own local model evaluator, not this solver):

```bash
esbmc --neurosym --neurosym-prog "$HOME/bin/neurosym-cpp-solve %f" --neurosym-model-prog "z3 -in" program.c
```

---

## Supported theory

QF_BV (bit-vectors), including array theory (`select`/`store`) via read-over-write encoding. No LIA (linear integer arithmetic) support. See [neurosym_cpp/README.md](neurosym_cpp/README.md) for full details, known limitations, and validation status.

---

## Tests

```bash
python3 tests/test_cpp_substitutions.py   # differential tests against Z3 (substitution/cycle-detection soundness)
python3 tests/test_ssa_substitution.py
python3 tests/test_word_rewrites.py
python3 tests/test_bvdiv_const.py
python3 tests/test_bvmul_sparse_const.py
python3 tests/test_array_select_dedup.py
```

---

## Mathematical Details

See [GANSAT_Mathematical_Details.md](GANSAT_Mathematical_Details.md) and [NeuroSym_System_Description.tex](NeuroSym_System_Description.tex) for background on the solver's design.

---

## Authors

* **Vishal Kumar Swain** (NIT Warangal)
* **Sangharatna Godboley** (NIT Warangal)
* **P. Radha Krishna** (NIT Warangal)
* **Avijit Das** (DRDO, LRDE Bengaluru)
* **Lucas Cordeiro** (The University of Manchester)

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
