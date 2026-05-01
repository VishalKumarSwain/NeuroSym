"""
run_gcov.py — Compile Vp2-B2.c with gcov instrumentation,
              run GANSAT-generated test cases, parse gcov output,
              and report Branch Coverage (BC) and Line Coverage (LC).

Usage (from project root in WSL):
    python scripts/run_gcov.py

Requirements:
    sudo apt install gcc gcov  (already in Ubuntu 22.04)
"""

import subprocess
import sys
import re
import os
from pathlib import Path

PROJECT  = Path(__file__).resolve().parent.parent
TESTS    = PROJECT / "tests"
C_FILE   = TESTS / "Vp2-B2.c"
DRIVER   = TESTS / "test_driver.c"
BUILD    = PROJECT / "build_gcov"
BINARY   = BUILD / "test_driver"


def run(cmd: list, cwd=None, check=True):
    result = subprocess.run(
        cmd, cwd=str(cwd or BUILD),
        capture_output=True, text=True
    )
    if check and result.returncode != 0:
        print(f"[ERROR] {' '.join(cmd)}")
        print(result.stderr[-2000:])
        sys.exit(1)
    return result


def compile_with_gcov():
    BUILD.mkdir(exist_ok=True)

    # Copy source files into build dir
    import shutil
    shutil.copy(C_FILE,  BUILD / "Vp2-B2.c")
    shutil.copy(DRIVER,  BUILD / "test_driver.c")
    shutil.copy(TESTS / "klee_stub.h", BUILD / "klee_stub.h")

    # Write a minimal llbmc.h stub (file needs #include <llbmc.h> on LLBMC path)
    llbmc_h = BUILD / "llbmc.h"
    if not llbmc_h.exists():
        llbmc_h.write_text(
            "/* llbmc stub */\n"
            "static inline void __llbmc_assume(int c){(void)c;}\n"
            "#define __llbmc_assert(c) ((void)(c))\n"
        )

    print("[compile] gcc -DLLBMC -fprofile-arcs -ftest-coverage ...")
    run([
        "gcc",
        "-DLLBMC",                  # use llbmc stub path (not klee)
        "-I.", "-I" + str(BUILD),   # find llbmc.h in build dir
        "-fprofile-arcs",
        "-ftest-coverage",
        "-O0",                      # no optimisation — accurate coverage
        "-g",
        "test_driver.c",
        "Vp2-B2.c",
        "-o", "test_driver",
        "-lm",
    ], cwd=BUILD)
    print("[compile] OK\n")


def run_tests():
    print("[run] Executing GANSAT test cases...")
    result = run([str(BINARY)], cwd=BUILD, check=False)
    print(result.stdout)
    if result.returncode != 0 and "PASS" not in result.stdout:
        print(f"[warn] Binary exited {result.returncode}")
    return result.stdout


def run_gcov():
    print("[gcov] Generating coverage data...")
    result = run(
        ["gcov", "-b", "-c", "Vp2-B2.c"],
        cwd=BUILD, check=False
    )
    return result.stdout + result.stderr


def parse_gcov_output(gcov_text: str, c_source: str) -> dict:
    """
    Parse gcov -b -c output and Vp2-B2.c.gcov file for BC and LC.
    """
    stats = {
        "lines_executed_pct": 0.0,
        "lines_executed":     0,
        "lines_total":        0,
        "branches_taken_pct": 0.0,
        "branches_taken":     0,
        "branches_total":     0,
    }

    # --- Summary from gcov stdout ---
    # e.g. "Lines executed:72.34% of 94"
    m = re.search(r"Lines executed:([\d.]+)% of (\d+)", gcov_text)
    if m:
        stats["lines_executed_pct"] = float(m.group(1))
        stats["lines_total"]        = int(m.group(2))
        stats["lines_executed"]     = int(stats["lines_total"] * stats["lines_executed_pct"] / 100)

    # --- Branch stats from .gcov file ---
    gcov_file = BUILD / "Vp2-B2.c.gcov"
    if gcov_file.exists():
        taken    = 0
        not_taken = 0
        for line in gcov_file.read_text().splitlines():
            if "branch" in line.lower():
                if "taken 0" in line.lower() or "never executed" in line.lower():
                    not_taken += 1
                else:
                    t = re.search(r"taken\s+(\d+)", line, re.IGNORECASE)
                    if t and int(t.group(1)) > 0:
                        taken += 1
                    elif t:
                        not_taken += 1
        stats["branches_taken"]   = taken
        stats["branches_total"]   = taken + not_taken
        if stats["branches_total"] > 0:
            stats["branches_taken_pct"] = taken / stats["branches_total"] * 100

    return stats


def print_report(stats: dict, n_tests: int):
    lc = stats["lines_executed_pct"]
    bc = stats["branches_taken_pct"]
    lt = stats["lines_total"]
    le = stats["lines_executed"]
    bt = stats["branches_total"]
    bk = stats["branches_taken"]

    print("=" * 52)
    print("  GANSAT Coverage Report — Vp2-B2.c")
    print("=" * 52)
    print(f"  Test cases executed       : {n_tests}")
    print(f"  Input domain              : {{1, 2, 3, 4, 5, 6}}")
    print(f"  Sequence length (BOUND)   : 2")
    print()
    print(f"  Line Coverage  (LC)       : {lc:.1f}%  ({le}/{lt} lines)")
    print(f"  Branch Coverage (BC)      : {bc:.1f}%  ({bk}/{bt} branches)")
    print()
    print(f"  Total if-statements       : 287")
    print(f"  Total branches (×2)       : ~574")
    print("=" * 52)

    # Verdict
    if lc >= 80 and bc >= 50:
        verdict = "GOOD — strong coverage for this complexity"
    elif lc >= 60:
        verdict = "MODERATE — increase test sequences to improve BC"
    else:
        verdict = "LOW — need longer input sequences"
    print(f"  Verdict: {verdict}")
    print("=" * 52)


def main():
    print("=" * 52)
    print("  GANSAT gcov Pipeline — Vp2-B2.c")
    print("=" * 52 + "\n")

    compile_with_gcov()
    test_output = run_tests()
    gcov_raw    = run_gcov()

    n_tests = test_output.count("TC")

    stats = parse_gcov_output(gcov_raw, str(BUILD / "Vp2-B2.c.gcov"))

    # Fallback: read raw gcov output numbers directly
    if stats["lines_total"] == 0:
        m = re.search(r"Lines executed:([\d.]+)% of (\d+)", gcov_raw)
        if m:
            stats["lines_executed_pct"] = float(m.group(1))
            stats["lines_total"] = int(m.group(2))
            stats["lines_executed"] = int(stats["lines_total"] * stats["lines_executed_pct"] / 100)
        m2 = re.search(r"Branches executed:([\d.]+)% of (\d+)", gcov_raw)
        if m2:
            stats["branches_total"] = int(m2.group(2))
            stats["branches_taken"] = int(stats["branches_total"] * float(m2.group(1)) / 100)
            stats["branches_taken_pct"] = float(m2.group(1))
        m3 = re.search(r"Taken at least once:([\d.]+)%", gcov_raw)
        if m3:
            stats["branches_taken_pct"] = float(m3.group(1))

    print("\nRaw gcov output:")
    print(gcov_raw[:1500])
    print()

    print_report(stats, n_tests)


if __name__ == "__main__":
    main()
