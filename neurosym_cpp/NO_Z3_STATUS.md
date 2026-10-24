# Native NeuroSym model completion, 2026-09-21

Implemented direct ESBMC model output (`--esbmc-model`) and false defaults
for declared, unblasted Boolean variables. Constrained and substituted
Boolean values are preserved. Bit-vector variables without a recovered
assignment remain omitted. No SMT-solving algorithm was changed.

Command: `esbmc-neurosym-no-z3 Vp1-B40.c` (BOUND 40 at validation).
Observed wall time: 10.285 seconds, assertion failure at line 454.
Native replay of ten reported inputs reproduced that assertion.
Process tracing found no Z3 executable. Z3 was used separately as a test
oracle to validate 53,930 model bindings against the original SMT formula.
308 existing differential tests and four new model-output tests passed.

This is a tested no-Z3 execution path, not a complete replacement for every
external model query: arrays, omitted BV assignments, and unsupported trace
expressions may still need further implementation. Existing ESBMC missing-
model error handling is unchanged. The previous esbmc-neurosym-cpp executable
was left untouched; use the new command above for this path.

Backups and evidence: /home/user2/VishResearch/neurosym_noz3_20260921_104407

Final installed command: 9.726 s. Same-file single-run comparison: previous
NeuroSym plus Z3 model fallback 14.449 s; Boolector 8.578 s. All detected
the assertion at line 454. This is a 32.7% wall-time reduction versus the
previous NeuroSym path; Boolector remains faster on this case.
