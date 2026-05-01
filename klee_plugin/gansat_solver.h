/**
 * gansat_solver.h — KLEE Solver Plugin for GANSAT
 *
 * Implements klee::Solver interface and delegates to GANSAT
 * via subprocess (SMT-LIB 2 stdin/stdout protocol).
 *
 * Integration steps:
 *   1. Copy gansat_solver.h + gansat_solver.cpp into klee/lib/Solver/
 *   2. Add to klee/lib/Solver/CMakeLists.txt
 *   3. Register in klee/lib/Solver/SolverManager.cpp
 *   4. Build KLEE: cmake + make
 *   5. Use: klee --solver-backend=gansat program.bc
 *
 * Protocol (SMT-LIB 2 subprocess):
 *   stdin  → SMT-LIB 2 formula
 *   stdout ← "sat" / "unsat" / "unknown"
 *             + "(model ...)" block if sat
 */

#pragma once

#include "klee/Solver/Solver.h"
#include "klee/Expr/Constraints.h"
#include "klee/Expr/ExprSMTLIBPrinter.h"

#include <string>
#include <memory>

namespace klee {

/**
 * GANSATSolverImpl
 *
 * Wraps GANSAT as a KLEE solver backend.
 * Supports QF_BV (KLEE's primary logic) and QF_LIA.
 *
 * Solver chain position (recommended):
 *   KLEE → CachingSolver → GANSATSolver → Z3Solver (internal fallback)
 *
 * GANSAT handles the fast path; Z3 handles anything GANSAT can't solve
 * within its n_candidates attempts. The internal Z3 fallback in
 * gansat/solver.py ensures completeness.
 */
class GANSATSolverImpl : public SolverImpl {
public:
    explicit GANSATSolverImpl(
        const std::string& gansat_cmd    = "python /gansat/main.py --stdin",
        int                timeout_ms    = 20000,
        int                n_candidates  = 16
    );

    ~GANSATSolverImpl() override = default;

    bool computeTruth(const Query& query, bool& isValid) override;
    bool computeValidity(const Query& query, Solver::Validity& result) override;
    bool computeValue(const Query& query, ref<Expr>& result) override;
    bool computeInitialValues(
        const Query& query,
        const std::vector<const Array*>& objects,
        std::vector<std::vector<unsigned char>>& values,
        bool& hasSolution
    ) override;

    SolverRunStatus getOperationStatusCode() override;
    std::string getConstraintLog(const Query& query) override;
    void setCoreSolverTimeout(time::Span timeout) override;

private:
    std::string  gansat_cmd_;
    int          timeout_ms_;
    int          n_candidates_;
    SolverRunStatus last_status_;

    /** Convert KLEE query to SMT-LIB 2 string */
    std::string queryToSMTLIB(const Query& query, bool needModel = false) const;

    /** Run GANSAT subprocess, return raw output */
    std::string runGANSAT(const std::string& smtlib) const;

    /** Parse "sat\n(model\n  (define-fun x () (_ BitVec 32) #x0000001a)..." */
    bool parseModel(
        const std::string& output,
        const std::vector<const Array*>& objects,
        std::vector<std::vector<unsigned char>>& values
    ) const;
};

/** Factory: create GANSAT solver and wrap in CachingSolver */
std::unique_ptr<Solver> createGANSATSolver(
    const std::string& gansat_cmd   = "python /gansat/main.py --stdin",
    int                timeout_ms   = 20000,
    int                n_candidates = 16
);

} // namespace klee
