# ESBMC integration patches

These 3 patches add and update NeuroSym support in ESBMC
(`esbmc/esbmc` upstream). They were developed and tested against a local
checkout of `esbmc/esbmc` on `master`, but that checkout has no push
access to upstream, so the patches are shared here instead.

## What each patch does

1. **`0001-...serve-counterexamples-from-NeuroSym-s-own-model-output.patch`**
   ESBMC's neurosym backend parses a `(model (define-fun ...))` block
   directly from NeuroSym's own stdout on a SAT verdict, instead of
   always paying for a second, independent solve through
   `--neurosym-model-prog` just to answer `(get-value)` queries.

2. **`0002-...don-t-fall-through-to-a-dead-model-solver-in-get_bv-l_get.patch`**
   Small correctness fix in `get_bv()`/`l_get()`'s handling of the local
   model / model-solver fallback path.

3. **`0003-...extend-local-model-evaluator-default-to-the-C-solver.patch`**
   - Extends the local model evaluator (`local_eval_bv`/`local_eval_bool`/
     `local_eval_array_at`) to resolve bit-vector arithmetic, comparisons,
     and array select/store chains directly from NeuroSym's model output,
     reducing (not eliminating) how often a full counterexample trace
     needs the `--neurosym-model-prog` fallback.
   - Changes the default `--neurosym-prog` from `"python main.py %f"` to
     `"$HOME/bin/neurosym-cpp-solve %f"` — NeuroSym has a native C++
     solver now (see `../neurosym_cpp/`, no Python involved at solve
     time), so the bare `--neurosym` flag uses that by default. The
     original Python solver remains fully available via an explicit
     `--neurosym-prog` override.

All three were validated: correct SAT/UNSAT verdicts and counterexamples
on real test programs, clean rebuild, no regressions in the existing
Python fallback path.

## How to apply

From a local `esbmc/esbmc` checkout, on the commit these were based on
(check each patch's header for the exact base if `master` has since
moved):

```bash
cd /path/to/esbmc
git am esbmc_integration/patches/0001-*.patch
git am esbmc_integration/patches/0002-*.patch
git am esbmc_integration/patches/0003-*.patch
```

`git am` preserves the original commit messages and authorship. If any
patch fails to apply cleanly (likely if `master` has moved on since these
were written), `git am --show-current-patch` shows the conflict, or fall
back to `git apply --reject` and resolve manually.

After applying, rebuild ESBMC normally (`cmake --build build --target
esbmc`) and the new default takes effect immediately for anyone building
from that point on.
