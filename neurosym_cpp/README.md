# neurosym_cpp — standalone C++ QF_BV solver (experimental)

A Python-free reimplementation of NeuroSym's core solving path: native
SMT-LIB2 parsing, Tseitin bit-blasting, and native MiniSat (SimpSolver)
solving, all in C++. Built as a performance experiment alongside the
production Python solver (`../main.py`), which remains the default,
fully-supported path.

## Status

Validated against the three real benchmark formulas used throughout this
project's development (SAT/UNSAT agreement, full model verification, and
1344/1344 exhaustive division/remainder differential tests). Measured
~2x-2.5x faster end-to-end than the Python solver on real ESBMC-generated
formulas.

Known limitations:
- No array theory (`select`/`store`) support — untested, throws a clear
  error rather than silently mishandling it.
- No LIA (linear integer arithmetic) or DPLL support — QF_BV only.
- No GAN-guided candidate generation — this is a pure symbolic solver,
  matching what the Python path does when the GAN doesn't fire (which is
  the common case on real formulas per this project's own profiling).
- Full ESBMC counterexample *trace* building for formulas containing
  sign_extend/zero_extend still requires `--neurosym-model-prog "z3 -in"`
  as a fallback -- a gap in ESBMC's own local model evaluator, unrelated
  to this solver or to Python. The SAT/UNSAT decision itself is always
  made by this solver, not z3.

## Build

```
g++ -O2 -std=c++17 -o bitblast_solver bitblast_solver.cpp -lminisat
```

Requires `libminisat.so.2` (or equivalent MiniSat shared library) and its
headers available on the build system. The binary must be built at
`neurosym_cpp/bitblast_solver` (i.e. right next to `bitblast_solver.cpp`,
not moved elsewhere) -- the wrapper scripts below locate it relative to
their own location, not a hardcoded path, so this is the only placement
that works out of the box.

## Usage

```
./bitblast_solver formula.smt2              # native SMT-LIB2 parsing
./bitblast_solver formula.json --json       # legacy JSON-IR path
```

## ESBMC integration

`bin/neurosym-cpp-solve` and `bin/esbmc-neurosym-cpp` wire this solver
into ESBMC as an alternate `--neurosym-prog` backend. Both scripts locate
`bitblast_solver` and each other *relative to their own real location*
(resolved through symlinks via `readlink -f`, not `$HOME`-hardcoded), so
they work from any checkout path on any machine -- including when you
symlink them onto your `$PATH` (recommended) rather than copying them:

```
mkdir -p ~/bin
ln -s "$(pwd)/neurosym_cpp/bin/neurosym-cpp-solve" ~/bin/neurosym-cpp-solve
ln -s "$(pwd)/neurosym_cpp/bin/esbmc-neurosym-cpp" ~/bin/esbmc-neurosym-cpp
export PATH="$HOME/bin:$PATH"   # add to ~/.bashrc to persist
```

Use symlinks, not copies -- a plain copy loses the link back to this
checkout and the scripts won't find `bitblast_solver` anymore.

`esbmc-neurosym-cpp` looks for `esbmc` on your `$PATH` first, then falls
back to `$HOME/VishResearch/esbmc` or `$HOME/bin/esbmc` if present. If
none of those apply to your setup, edit the `ESBMC=` resolution at the
top of the script.

```
esbmc-neurosym-cpp yourfile.c
```
