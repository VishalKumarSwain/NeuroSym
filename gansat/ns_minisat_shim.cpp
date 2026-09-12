// Minimal extern "C" shim around MiniSat -- exposes both the plain
// Minisat::Solver entry point (ns_ms_*, kept for diagnostics/comparison)
// and Minisat::SimpSolver (ns_mss_*, the preprocessing-enabled entry
// point the installed `minisat` CLI itself is built around). Two parallel
// sets of functions rather than one generic one: Solver::solve() is not
// virtual, so calling it through a Solver* that actually points at a
// SimpSolver object would silently call the *base* solve() (no
// preprocessing) instead of SimpSolver's override -- the exact pitfall
// this phase exists to avoid. Keeping the types concrete throughout
// sidesteps that entirely rather than relying on virtual dispatch that
// isn't there.
#include <minisat/core/Solver.h>
#include <minisat/simp/SimpSolver.h>

using namespace Minisat;

extern "C" {

// ── Plain Solver (no preprocessing) -- kept for diagnostics ────────────────

void *ns_ms_create() { return new Solver(); }
void ns_ms_destroy(void *solver) { delete static_cast<Solver *>(solver); }

void ns_ms_ensure_vars(void *solver, int n) {
    Solver *s = static_cast<Solver *>(solver);
    while (s->nVars() < n)
        s->newVar();
}

int ns_ms_add_clauses_bulk(void *solver, const int *literals,
                            const int *clause_lens, int num_clauses) {
    Solver *s = static_cast<Solver *>(solver);
    vec<Lit> ps;
    int pos = 0;
    bool ok = true;
    for (int c = 0; c < num_clauses; c++) {
        int len = clause_lens[c];
        ps.clear();
        for (int i = 0; i < len; i++) {
            int lit = literals[pos++];
            int var = (lit > 0 ? lit : -lit) - 1;
            ps.push(mkLit(var, lit < 0));
        }
        if (!s->addClause(ps))
            ok = false;
    }
    return ok ? 1 : 0;
}

int ns_ms_solve(void *solver) {
    return static_cast<Solver *>(solver)->solve() ? 1 : 0;
}

int ns_ms_model_value(void *solver, int dimacs_var) {
    Solver *s = static_cast<Solver *>(solver);
    int idx = dimacs_var - 1;
    if (idx < 0 || idx >= s->model.size())
        return -1;
    lbool v = s->model[idx];
    if (v == l_True) return 1;
    if (v == l_False) return 0;
    return -1;
}

int ns_ms_n_vars(void *solver) { return static_cast<Solver *>(solver)->nVars(); }
long long ns_ms_conflicts(void *solver)    { return (long long)static_cast<Solver *>(solver)->conflicts; }
long long ns_ms_decisions(void *solver)    { return (long long)static_cast<Solver *>(solver)->decisions; }
long long ns_ms_propagations(void *solver) { return (long long)static_cast<Solver *>(solver)->propagations; }
long long ns_ms_restarts(void *solver)     { return (long long)static_cast<Solver *>(solver)->starts; }


// ── SimpSolver (preprocessing enabled, matches the CLI's own entry point) ──
// SimpSolver publicly inherits Solver, so nVars()/model/conflicts/etc. are
// the exact same fields Solver uses -- only solve() and construction need
// their own concretely-typed versions to actually invoke SimpSolver's
// simplification-aware code path instead of the (non-virtual) base one.

void *ns_mss_create() { return new SimpSolver(); }
void ns_mss_destroy(void *solver) { delete static_cast<SimpSolver *>(solver); }

void ns_mss_ensure_vars(void *solver, int n) {
    SimpSolver *s = static_cast<SimpSolver *>(solver);
    while (s->nVars() < n)
        s->newVar();
}

int ns_mss_add_clauses_bulk(void *solver, const int *literals,
                             const int *clause_lens, int num_clauses) {
    SimpSolver *s = static_cast<SimpSolver *>(solver);
    vec<Lit> ps;
    int pos = 0;
    bool ok = true;
    for (int c = 0; c < num_clauses; c++) {
        int len = clause_lens[c];
        ps.clear();
        for (int i = 0; i < len; i++) {
            int lit = literals[pos++];
            int var = (lit > 0 ? lit : -lit) - 1;
            ps.push(mkLit(var, lit < 0));
        }
        if (!s->addClause(ps))
            ok = false;
    }
    return ok ? 1 : 0;
}

// SimpSolver::solve()'s do_simp defaults to true (see SimpSolver.h) --
// this is exactly the CLI's own default invocation, not a hand-picked
// variant: no separate explicit eliminate() call is made, matching what a
// plain `solver.solve()` call (the simplest, most standard usage pattern,
// and the one the header's own default parameters point to) already does.
int ns_mss_solve(void *solver) {
    return static_cast<SimpSolver *>(solver)->solve() ? 1 : 0;
}

// model[] is inherited from Solver and is fully extended (eliminated
// variables' values reconstructed) internally by SimpSolver before
// solve() returns -- see SimpSolver::extendModel(), called from within
// solve_(). No special-casing needed here versus the plain-Solver
// accessor; this is verified empirically by the differential tests
// accompanying this change, not assumed from the header comment alone.
int ns_mss_model_value(void *solver, int dimacs_var) {
    SimpSolver *s = static_cast<SimpSolver *>(solver);
    int idx = dimacs_var - 1;
    if (idx < 0 || idx >= s->model.size())
        return -1;
    lbool v = s->model[idx];
    if (v == l_True) return 1;
    if (v == l_False) return 0;
    return -1;
}

int ns_mss_n_vars(void *solver) { return static_cast<SimpSolver *>(solver)->nVars(); }
long long ns_mss_conflicts(void *solver)    { return (long long)static_cast<SimpSolver *>(solver)->conflicts; }
long long ns_mss_decisions(void *solver)    { return (long long)static_cast<SimpSolver *>(solver)->decisions; }
long long ns_mss_propagations(void *solver) { return (long long)static_cast<SimpSolver *>(solver)->propagations; }
long long ns_mss_restarts(void *solver)     { return (long long)static_cast<SimpSolver *>(solver)->starts; }
int ns_mss_eliminated_vars(void *solver)    { return static_cast<SimpSolver *>(solver)->eliminated_vars; }

} // extern "C"
