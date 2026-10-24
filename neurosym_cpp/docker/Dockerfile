# ESBMC-NeuroSym ICSE 2027 artifact, version 2: corrected NeuroSym (soundness fixes:
# wide literals, >64-bit constant folding, shift overflow, n-ary operands, bvsmod)
# and the overflow example of the paper. Base: the v1 image (ESBMC b39c8ce, solvers, benchmarks).
FROM esbmc-neurosym:soundness-final AS fixed
FROM esbmc-neurosym:icse27
COPY --from=fixed /opt/neurosym/neurosym_cpp/bitblast_solver /opt/neurosym/neurosym_cpp/bitblast_solver
COPY bitblast_solver.cpp smt2_parser.inc /opt/neurosym/neurosym_cpp/
COPY bug.c /examples/bug.c
LABEL org.opencontainers.image.version="2" org.opencontainers.image.title="ESBMC-NeuroSym ICSE 2027 artifact"
