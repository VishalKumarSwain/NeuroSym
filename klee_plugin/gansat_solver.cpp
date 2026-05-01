/**
 * gansat_solver.cpp — KLEE Solver Plugin for GANSAT
 *
 * Full implementation of GANSATSolverImpl.
 * KLEE → SMT-LIB 2 string → GANSAT subprocess → parse result → KLEE.
 */

#include "gansat_solver.h"

#include "klee/Expr/ExprSMTLIBPrinter.h"
#include "klee/Solver/SolverCmdLine.h"
#include "klee/Support/ErrorHandling.h"

#include <llvm/Support/raw_ostream.h>

#include <array>
#include <cstdio>
#include <regex>
#include <sstream>
#include <stdexcept>

namespace klee {

// ─── Constructor ──────────────────────────────────────────────────────────────

GANSATSolverImpl::GANSATSolverImpl(
    const std::string& gansat_cmd,
    int timeout_ms,
    int n_candidates
)
    : gansat_cmd_(gansat_cmd)
    , timeout_ms_(timeout_ms)
    , n_candidates_(n_candidates)
    , last_status_(SOLVER_RUN_STATUS_SUCCESS_SOLVABLE)
{}


// ─── computeTruth ─────────────────────────────────────────────────────────────

bool GANSATSolverImpl::computeTruth(const Query& query, bool& isValid) {
    // isValid = true  iff  query.expr is true under ALL models of constraints
    // Negate query.expr and check satisfiability
    Query negated = query.negateExpr();
    std::string smtlib = queryToSMTLIB(negated);
    std::string output = runGANSAT(smtlib);

    if (output.rfind("unsat", 0) == 0) {
        isValid = true;
        last_status_ = SOLVER_RUN_STATUS_SUCCESS_UNSOLVABLE;
        return true;
    }
    if (output.rfind("sat", 0) == 0) {
        isValid = false;
        last_status_ = SOLVER_RUN_STATUS_SUCCESS_SOLVABLE;
        return true;
    }
    last_status_ = SOLVER_RUN_STATUS_FAILURE;
    return false;
}


// ─── computeValidity ──────────────────────────────────────────────────────────

bool GANSATSolverImpl::computeValidity(const Query& query, Solver::Validity& result) {
    bool isTrueUnderAll = false;
    if (!computeTruth(query, isTrueUnderAll))
        return false;

    if (isTrueUnderAll) {
        result = Solver::True;
        return true;
    }

    // Check if it's false under all models
    bool isFalseUnderAll = false;
    if (!computeTruth(query.negateExpr(), isFalseUnderAll))
        return false;

    result = isFalseUnderAll ? Solver::False : Solver::Unknown;
    return true;
}


// ─── computeValue ────────────────────────────────────────────────────────────

bool GANSATSolverImpl::computeValue(const Query& query, ref<Expr>& result) {
    std::vector<const Array*> objects;
    std::vector<std::vector<unsigned char>> values;
    bool hasSolution = false;

    // Collect free arrays from the expression
    findSymbolicObjects(query.expr, objects);
    findSymbolicObjects(ConstraintSet(query.constraints), objects);

    if (!computeInitialValues(query.withFalse(), objects, values, hasSolution))
        return false;

    if (!hasSolution) {
        last_status_ = SOLVER_RUN_STATUS_FAILURE;
        return false;
    }

    // Evaluate expression with found concrete values
    Assignment assign(objects, values);
    result = assign.evaluate(query.expr);
    return true;
}


// ─── computeInitialValues ────────────────────────────────────────────────────

bool GANSATSolverImpl::computeInitialValues(
    const Query& query,
    const std::vector<const Array*>& objects,
    std::vector<std::vector<unsigned char>>& values,
    bool& hasSolution
) {
    std::string smtlib = queryToSMTLIB(query, /*needModel=*/true);
    std::string output = runGANSAT(smtlib);

    if (output.rfind("unsat", 0) == 0) {
        hasSolution = false;
        last_status_ = SOLVER_RUN_STATUS_SUCCESS_UNSOLVABLE;
        return true;
    }

    if (output.rfind("sat", 0) == 0) {
        hasSolution = true;
        last_status_ = SOLVER_RUN_STATUS_SUCCESS_SOLVABLE;
        values.resize(objects.size());
        return parseModel(output, objects, values);
    }

    last_status_ = SOLVER_RUN_STATUS_FAILURE;
    return false;
}


// ─── SMT-LIB generation ──────────────────────────────────────────────────────

std::string GANSATSolverImpl::queryToSMTLIB(const Query& query, bool needModel) const {
    std::string buf;
    llvm::raw_string_ostream os(buf);

    ExprSMTLIBPrinter printer;
    printer.setOutput(os);
    printer.setQuery(query);
    printer.setLogic(ExprSMTLIBPrinter::QF_ABV);  // KLEE uses arrays + bitvectors

    if (needModel)
        printer.setHumanReadable(true);

    printer.generateOutput();
    os.flush();
    return buf;
}


// ─── Subprocess execution ─────────────────────────────────────────────────────

std::string GANSATSolverImpl::runGANSAT(const std::string& smtlib) const {
    // Write to temp file and pass to GANSAT
    // (pipe via popen for subprocess communication)
    std::string cmd = gansat_cmd_ + " 2>/dev/null";

    FILE* proc = popen(cmd.c_str(), "r+");
    if (!proc) {
        klee_warning("GANSATSolver: failed to launch subprocess: %s", cmd.c_str());
        return "unknown";
    }

    // Write SMT-LIB to subprocess stdin
    fwrite(smtlib.c_str(), 1, smtlib.size(), proc);
    fflush(proc);

    // Read output
    std::string output;
    std::array<char, 256> buf;
    while (fgets(buf.data(), buf.size(), proc))
        output += buf.data();

    pclose(proc);
    return output.empty() ? "unknown" : output;
}


// ─── Model parsing ───────────────────────────────────────────────────────────

bool GANSATSolverImpl::parseModel(
    const std::string& output,
    const std::vector<const Array*>& objects,
    std::vector<std::vector<unsigned char>>& values
) const {
    // Parse: (define-fun arr_name () (_ BitVec N) #xHHHH)
    // or:    (define-fun var_name () Int VALUE)
    std::regex bv_re(R"(\(define-fun\s+(\S+)\s+\(\)\s+\(_\s+BitVec\s+(\d+)\)\s+#x([0-9a-fA-F]+)\))");
    std::regex int_re(R"(\(define-fun\s+(\S+)\s+\(\)\s+Int\s+(-?\d+)\))");

    std::map<std::string, std::vector<unsigned char>> model_map;

    // Parse BV values
    auto bv_begin = std::sregex_iterator(output.begin(), output.end(), bv_re);
    auto bv_end   = std::sregex_iterator();
    for (auto it = bv_begin; it != bv_end; ++it) {
        std::string name  = (*it)[1].str();
        int         width = std::stoi((*it)[2].str());
        std::string hex   = (*it)[3].str();
        int bytes = (width + 7) / 8;
        std::vector<unsigned char> raw(bytes, 0);
        uint64_t val = std::stoull(hex, nullptr, 16);
        for (int b = 0; b < bytes; ++b)
            raw[b] = (val >> (b * 8)) & 0xFF;
        model_map[name] = raw;
    }

    // Parse Int values
    auto int_begin = std::sregex_iterator(output.begin(), output.end(), int_re);
    auto int_end   = std::sregex_iterator();
    for (auto it = int_begin; it != int_end; ++it) {
        std::string name = (*it)[1].str();
        int64_t val = std::stoll((*it)[2].str());
        std::vector<unsigned char> raw(4);
        for (int b = 0; b < 4; ++b)
            raw[b] = (val >> (b * 8)) & 0xFF;
        model_map[name] = raw;
    }

    // Fill values array in objects order
    for (size_t i = 0; i < objects.size(); ++i) {
        const Array* arr = objects[i];
        auto it = model_map.find(arr->getName());
        if (it != model_map.end()) {
            values[i] = it->second;
        } else {
            // Default: zero-fill to array size
            values[i].assign(arr->size, 0);
        }
    }
    return true;
}


// ─── Status / misc ────────────────────────────────────────────────────────────

SolverRunStatus GANSATSolverImpl::getOperationStatusCode() {
    return last_status_;
}

std::string GANSATSolverImpl::getConstraintLog(const Query& query) {
    return queryToSMTLIB(query);
}

void GANSATSolverImpl::setCoreSolverTimeout(time::Span timeout) {
    timeout_ms_ = static_cast<int>(timeout.toMilliSeconds());
}


// ─── Factory ─────────────────────────────────────────────────────────────────

std::unique_ptr<Solver> createGANSATSolver(
    const std::string& gansat_cmd,
    int timeout_ms,
    int n_candidates
) {
    auto impl = std::make_unique<GANSATSolverImpl>(gansat_cmd, timeout_ms, n_candidates);
    return std::make_unique<Solver>(std::move(impl));
}

} // namespace klee
