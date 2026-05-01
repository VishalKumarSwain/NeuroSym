#!/bin/bash
# patch_klee.sh — Auto-patch a KLEE source tree to add GANSAT solver backend
#
# Usage:
#   chmod +x patch_klee.sh
#   ./patch_klee.sh /path/to/klee/source
#
# What it does:
#   1. Copies gansat_solver.h/.cpp into klee/lib/Solver/
#   2. Adds gansat_solver.cpp to klee/lib/Solver/CMakeLists.txt
#   3. Registers GANSATSolver in klee/lib/Solver/SolverManager.cpp
#   4. Adds --solver-backend=gansat CLI option
#   5. Prints build instructions

set -e

KLEE_SRC="${1:?Usage: $0 /path/to/klee/source}"
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$PLUGIN_DIR")"

echo "==================================================="
echo "  GANSAT KLEE Patch"
echo "  KLEE source : $KLEE_SRC"
echo "  GANSAT root : $PROJECT_ROOT"
echo "==================================================="

# ── 1. Copy plugin files ──────────────────────────────────────────────────────
echo "[1/5] Copying solver plugin files..."
cp "$PLUGIN_DIR/gansat_solver.h"   "$KLEE_SRC/lib/Solver/"
cp "$PLUGIN_DIR/gansat_solver.cpp" "$KLEE_SRC/lib/Solver/"
echo "      ✓ gansat_solver.h"
echo "      ✓ gansat_solver.cpp"

# ── 2. Patch CMakeLists.txt ───────────────────────────────────────────────────
echo "[2/5] Patching lib/Solver/CMakeLists.txt..."
CMAKE_FILE="$KLEE_SRC/lib/Solver/CMakeLists.txt"

if ! grep -q "gansat_solver.cpp" "$CMAKE_FILE"; then
    # Find the line with the last .cpp file in target_sources and add after it
    sed -i '/STPSolver\.cpp\|Z3Solver\.cpp\|MetaSMTSolver\.cpp/ a\  gansat_solver.cpp' "$CMAKE_FILE"
    echo "      ✓ Added gansat_solver.cpp to CMakeLists.txt"
else
    echo "      ✓ Already present in CMakeLists.txt"
fi

# ── 3. Register in SolverManager ─────────────────────────────────────────────
echo "[3/5] Patching lib/Solver/SolverManager.cpp..."
SM_FILE="$KLEE_SRC/lib/Solver/SolverManager.cpp"

if ! grep -q "GANSATSolver" "$SM_FILE"; then
    # Add include after last solver include
    sed -i '/#include.*Z3Solver\|#include.*STPSolver/ a\#include "gansat_solver.h"' "$SM_FILE"

    # Register in the solver creation switch/if-else block
    GANSAT_CASE=$(cat <<'GANSAT_EOF'

  if (solverName == "gansat" || solverName == "GANSAT") {
    std::string cmd = "python GANSAT_PROJECT_ROOT/klee_plugin/gansat_bridge.py";
    return createGANSATSolver(cmd, 20000, 16);
  }
GANSAT_EOF
)
    # Escape for sed
    GANSAT_ESC="${GANSAT_CASE//GANSAT_PROJECT_ROOT/$PROJECT_ROOT}"
    # Insert before the closing of solver creation
    python3 -c "
import re, sys
content = open('$SM_FILE').read()
insert = '''$GANSAT_ESC'''
# Insert before 'return createDefaultSolver' or before last solver option
pattern = r'(if \(solverName == .z3.)'
replacement = insert.strip() + r'\n  \1'
content = re.sub(pattern, replacement, content, count=1)
open('$SM_FILE', 'w').write(content)
print('      ✓ Registered GANSATSolver in SolverManager')
"
else
    echo "      ✓ GANSATSolver already registered"
fi

# ── 4. Add CLI option ─────────────────────────────────────────────────────────
echo "[4/5] Adding --solver-backend=gansat CLI option..."
CMD_FILE="$KLEE_SRC/lib/Solver/SolverCmdLine.cpp"

if [ -f "$CMD_FILE" ] && ! grep -q "gansat" "$CMD_FILE"; then
    sed -i '/clEnumValN.*z3.*"Use Z3"/ a\                       clEnumValN(SOLVER_GANSAT, "gansat", "Use GANSAT (GAN-guided SMT solver)"),' "$CMD_FILE"
    echo "      ✓ Added gansat to solver CLI options"
else
    echo "      ✓ CLI option already present or file not found"
fi

# ── 5. Print build instructions ───────────────────────────────────────────────
echo "[5/5] Done! Build instructions:"
echo ""
echo "  cd $KLEE_SRC/build"
echo "  cmake .. -DENABLE_SOLVER_Z3=ON  (keep Z3 for fallback)"
echo "  make -j\$(nproc)"
echo ""
echo "  Then run KLEE with GANSAT:"
echo "  klee --solver-backend=gansat \\  "
echo "       --gansat-model=$PROJECT_ROOT/models/gansat_bv.pt \\"
echo "       program.bc"
echo ""
echo "==================================================="
echo "  Patch complete!"
echo "==================================================="
