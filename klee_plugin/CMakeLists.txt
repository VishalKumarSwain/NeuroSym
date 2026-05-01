# CMakeLists.txt — GANSAT KLEE Solver Plugin
#
# Drop this file into klee/lib/Solver/ alongside gansat_solver.h/.cpp
# and add the following line to klee/lib/Solver/CMakeLists.txt:
#
#   include(${CMAKE_CURRENT_SOURCE_DIR}/gansat/CMakeLists.txt)
#
# OR simply add gansat_solver.cpp to the existing target_sources() call.

# ── Option: build as part of the main kleaverSolver library ──────────────────
# In klee/lib/Solver/CMakeLists.txt, add:
#
#   target_sources(kleaverSolver PRIVATE
#       gansat_solver.cpp
#   )
#   target_include_directories(kleaverSolver PRIVATE
#       ${CMAKE_CURRENT_SOURCE_DIR}
#   )
#
# ── Option: build as a separate shared library (for dynamic loading) ──────────

cmake_minimum_required(VERSION 3.14)

add_library(GANSATSolverPlugin SHARED
    gansat_solver.cpp
)

target_include_directories(GANSATSolverPlugin PRIVATE
    ${KLEE_INCLUDE_DIRS}
    ${LLVM_INCLUDE_DIRS}
)

target_link_libraries(GANSATSolverPlugin PRIVATE
    kleaverSolver
    kleaverExpr
    ${LLVM_LIBRARIES}
)

target_compile_features(GANSATSolverPlugin PRIVATE cxx_std_17)

install(TARGETS GANSATSolverPlugin DESTINATION lib)
