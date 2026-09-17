# ESBMC integration patches

These patches add and update NeuroSym support in ESBMC
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

4. **`0004-neurosym-recognize-textual-true-false-in-local_eval_.patch`**
   The local model evaluator's `SYMBOL` case in `local_eval_bool` didn't
   recognize NeuroSym's own `true`/`false` text output for Boolean
   model values, falling through to the `--neurosym-model-prog` solver
   fallback unnecessarily for every Boolean-typed counterexample
   variable. Fixed to parse them directly.

5. **`0005-neurosym-quiet-the-solver-invocation-log-lines.patch`**
   Cosmetic only, no behavior change. `neurosym_convt::solver_text()`
   and the shared `smtlib_convt::solver_text()` base (also used by
   Bitwuzllob) printed the *entire* `--neurosym-prog`/`--smtlib-solver-prog`
   command line — full path, every flag, the `%f` placeholder — via
   `log_progress` on every run, where every other solver just prints a
   short name (e.g. `Z3 v4.13.3`). Shortened to just `NeuroSym` (and to
   the program's basename for the generic smtlib case). Separately,
   `oneshot_process.cpp` logged the fully-substituted command (including
   the temp formula path) via `log_status` on every solve call — demoted
   to `log_debug`, so it's hidden at normal verbosity but still visible
   if you actually need to debug the subprocess invocation
   (`--verbosity` high enough to show debug-level messages).

   Net effect on a NeuroSym run: what used to print as two lines —
   ```
   [PROGRESS] Solving with solver NeuroSym '/path/to/neurosym-cpp-solve %f'
   Running neurosym: /path/to/neurosym-cpp-solve '/tmp/esbmc-neurosym-xxxx.smt2'
   ```
   is now one:
   ```
   Solving with solver NeuroSym
   ```

All patches were validated: correct SAT/UNSAT verdicts and counterexamples
on real test programs, clean rebuild, no regressions in the existing
fallback paths.

## How to apply

From a local `esbmc/esbmc` checkout, on the commit these were based on
(check each patch's header for the exact base if `master` has since
moved):

```bash
cd /path/to/esbmc
git am esbmc_integration/patches/0001-*.patch
git am esbmc_integration/patches/0002-*.patch
git am esbmc_integration/patches/0003-*.patch
git am esbmc_integration/patches/0004-*.patch
git am esbmc_integration/patches/0005-*.patch
```

`git am` preserves the original commit messages and authorship. If any
patch fails to apply cleanly (likely if `master` has moved on since these
were written), `git am --show-current-patch` shows the conflict, or fall
back to `git apply --reject` and resolve manually.

After applying, rebuild ESBMC normally (`cmake --build build --target
esbmc`) and the new defaults/behavior take effect immediately for anyone
building from that point on.
