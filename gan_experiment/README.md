# GAN experiment: running the original neural model natively in C++

**Status: experimental, not recommended for use.** This directory documents a real
attempt to run NeuroSym's original GAN (previously in `gansat/`, `main.py`,
`models/`, deleted from the main tree since the C++ solver doesn't use it) natively
in C++ via LibTorch, and the results of testing it against the same trained weights
in both Python and C++.

## What's here

- `main.py`, `gansat/` — the original Python GAN implementation, restored from git
  history (commit `0714aaa~1`) for comparison testing.
- `models/gansat_bv.pt` — the original trained weights (unchanged, not retrained).
- `export_gan_torchscript.py` — exports `gansat_bv.pt` to a TorchScript-traced
  format (`gansat_bv_traced.pt`) so it can be loaded directly in C++ via LibTorch,
  with no Python at all at runtime. Traced output is bit-identical to eager Python
  output (verified: max diff = 0.0).
- `bitblast_solver_gan.cpp` — a copy of `neurosym_cpp/bitblast_solver.cpp` with an
  added C++ port of `gansat/bv_encoder.py`'s formula encoder, plus code to load and
  run the traced GAN model via LibTorch, decode its guess, and check it against the
  real formula. Set `GAN_TEST=1` when running it to print the GAN's candidate
  assignment and how many of the real constraints it actually satisfies. This is a
  diagnostic side-channel only -- the GAN's guess is never used as the actual
  answer; the real decision still always comes from the exact bit-blast+MiniSat
  path, unchanged.
- `gan_cpp_wrapper.sh` -- wires `bitblast_solver_gan` into ESBMC as a
  `--neurosym-prog` backend, same protocol as the real `neurosym-cpp-solve`.
- `pure_bv_test.smt2` -- a small, hand-written, array-free QF_BV formula used for
  a fair test of the GAN (see below).

## Build

```
TORCH_LIB=$(python3 -c "import torch,os;print(os.path.dirname(torch.__file__))")/lib
TORCH_INC=$(python3 -c "import torch,os;print(os.path.dirname(torch.__file__))")/include
g++ -O2 -std=c++17 -D_GLIBCXX_USE_CXX11_ABI=1 \
  -I"$TORCH_INC" -I"$TORCH_INC/torch/csrc/api/include" \
  -o bitblast_solver_gan bitblast_solver_gan.cpp \
  -L"$TORCH_LIB" -ltorch -ltorch_cpu -lc10 -lminisat \
  -Wl,-rpath,"$TORCH_LIB"
```

Requires a `torch` install (CPU build is fine; `pip install torch` provides all the
LibTorch shared libraries this needs) and MiniSat headers/library, same as the
main solver.

## Known, deliberate deviation from a "correct" encoder

The C++ encoder in `bitblast_solver_gan.cpp` faithfully reproduces a real bug in
the original `gansat/bv_encoder.py`: its `_BV_OP_INDEX` table only recognizes
non-strict comparisons (`<=`, `>=`), never strict ones (`<`, `>`) -- Z3 (and this
project's own parser) represent those as distinct operator kinds, and the
original encoder silently drops any constraint using a strict inequality from the
feature vector entirely. This is ported bug-for-bug on purpose: the trained
weights were fit against that exact (buggy) distribution, so "fixing" the encoder
without retraining would feed the model out-of-distribution input and likely
perform *worse*, not better.

## What we actually found

Tested on real formulas, comparing the original Python GAN (`main.py`, unmodified)
against the new C++ port, using the *same* trained weights in both:

1. **On a real ESBMC-generated formula (from `test8.c`, includes array-bookkeeping
   symbols)**: the original Python GAN never even attempts it
   (`gan_skipped_reason: uses_arrays`), falling straight through to its own
   symbolic fallback. The C++ port doesn't implement that skip logic, so it did
   attempt it -- and produced a candidate that satisfied only 8 of 14 real
   assertions. Reproduced identically across multiple runs through the full ESBMC
   pipeline.

2. **On a small, clean, array-free QF_BV formula (`pure_bv_test.smt2`), squarely
   inside the GAN's intended scope**: cross-checked directly against the
   *original, unmodified Python GAN* on the exact same formula.
   - Python: `gan_attempted: True, gan_success: False`
   - C++: candidate satisfied only 1 of 5 real assertions
   - Reproduced 3 times independently (two different machines), each with a
     different random seed, each time producing a different but still poor guess
     (never above 1/5).

**Conclusion: this is not a bug in the C++ port.** The same trained weights, run
through the original Python code on a fair, in-scope test formula, fail the same
way. The weakness is in the original trained model itself, not the porting work.

## What would be needed to actually make this useful

At minimum: (1) fix the strict-inequality encoder gap, and (2) **retrain** the
model against the corrected encoding -- the current weights were fit against the
buggy distribution, so fixing the encoder alone without retraining would make
results worse, not better. This is a real ML research task, not a quick patch.

## Bottom line

The production path (`neurosym_cpp/bitblast_solver.cpp`, no GAN) remains the
recommended solver. This experiment is preserved as documentation of what was
tried and why it wasn't adopted, not as a working feature.
