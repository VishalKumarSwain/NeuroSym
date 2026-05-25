#!/usr/bin/env bash
# Build the SMT-COMP 2026 submission archive for NeuroSym.
#
# Usage (Linux / WSL / Docker):
#   bash build_archive.sh
#
# What it does:
#   1. pip-installs z3-solver, bitwuzla, networkx, pysmt into lib/
#   2. Packs gansat/, main.py, lib/, models/ (if present), README.md into
#      NeuroSym-<VERSION>.tar.gz with the prefix NeuroSym-<VERSION>/
#   3. Prints the SHA256 to paste into NeuroSym.json
#
# Why bundle lib/?
#   The competition Ubuntu 24.04 image has python3-numpy and python3-tqdm
#   but does NOT have z3-solver, bitwuzla, networkx, or pysmt as pip packages.
#   torch is intentionally excluded (~1.5 GB); the GAN path degrades gracefully.
set -euo pipefail

VERSION="1.1"
ARCHIVE="NeuroSym-${VERSION}.tar.gz"
PREFIX="NeuroSym-${VERSION}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "==> Bundling Python dependencies into lib/ ..."
rm -rf lib/
mkdir -p lib/

pip install --target ./lib --no-compile \
    "z3-solver>=4.12.0" \
    "bitwuzla>=0.9.0" \
    "networkx>=3.1" \
    "pysmt>=0.9.5" \
    "numpy>=1.24.0"

# Strip runtime-irrelevant bulk to keep the archive small
find lib/ -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find lib/ -type d -name "tests"       -exec rm -rf {} + 2>/dev/null || true
find lib/ -type d -name "test"        -exec rm -rf {} + 2>/dev/null || true
find lib/ -name "*.dist-info" -type d -exec rm -rf {} + 2>/dev/null || true

echo "==> Creating ${ARCHIVE} ..."

ITEMS=(gansat/ main.py lib/ README.md)
[ -d models ] && ITEMS+=(models/)

tar -czf "${ARCHIVE}" \
    --exclude=".git" \
    --exclude="__pycache__" \
    --exclude="*.pyc" \
    --exclude="*.pyo" \
    --transform "s|^|${PREFIX}/|" \
    "${ITEMS[@]}"

SHA256=$(sha256sum "${ARCHIVE}" | awk '{print $1}')
SIZE=$(du -sh "${ARCHIVE}" | awk '{print $1}')

echo ""
echo "=== Build complete ==="
echo "Archive : ${ARCHIVE}  (${SIZE})"
echo "SHA256  : ${SHA256}"
echo ""
echo "Next steps:"
echo "  1. Upload ${ARCHIVE} to https://zenodo.org/uploads/20368663"
echo "  2. In NeuroSym.json set:"
echo "       \"url\": \"https://zenodo.org/records/20368663/files/${ARCHIVE}\","
echo "       \"sha256\": \"${SHA256}\""
