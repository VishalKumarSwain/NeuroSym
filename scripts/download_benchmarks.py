"""
Download SMT-LIB QF_LIA benchmarks from Zenodo (official SMT-LIB archive).

Usage:
    python scripts/download_benchmarks.py --logic QF_LIA --max 5000 --out data/benchmarks
"""

import argparse
import os
import sys
import urllib.request
import zipfile
import tarfile
import hashlib
from pathlib import Path

# Official SMT-LIB benchmark releases on Zenodo
SOURCES = {
    "QF_LIA": {
        "url": "https://zenodo.org/record/11613764/files/QF_LIA.tar.zst",
        "note": "QF_LIA family (~300k benchmarks, large). Use --max to limit.",
    },
    "QF_NIA": {
        "url": "https://zenodo.org/record/11613764/files/QF_NIA.tar.zst",
        "note": "QF_NIA family.",
    },
}

SMTLIB_GIT = "https://clc-gitlab.cs.uiowa.edu:2443/SMT-LIB-benchmarks"


def download_via_git(logic: str, out_dir: Path, max_files: int):
    """Clone a subset using sparse checkout (no large download)."""
    import subprocess
    repo_url = f"{SMTLIB_GIT}/{logic}.git"
    dest = out_dir / logic
    if dest.exists():
        print(f"[skip] {dest} already exists.")
        return

    print(f"[git] Cloning {repo_url} (sparse, depth 1) ...")
    subprocess.run(
        ["git", "clone", "--depth", "1", "--filter=blob:none",
         "--sparse", repo_url, str(dest)],
        check=True,
    )
    subprocess.run(["git", "-C", str(dest), "sparse-checkout", "set", "--cone", "."],
                   check=True)
    print(f"[git] Done. Benchmarks at: {dest}")


def collect_smt2_files(root: Path, max_files: int) -> list:
    files = list(root.rglob("*.smt2"))
    if max_files and len(files) > max_files:
        files = files[:max_files]
    return files


def generate_synthetic_qf_lia(out_dir: Path, n: int = 1000):
    """
    Generate synthetic QF_LIA benchmarks for quick bootstrapping before
    the full SMT-LIB download completes.

    Each benchmark: random system of linear inequalities over integers.
    """
    import random
    import z3

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[synthetic] Generating {n} QF_LIA benchmarks → {out_dir}")

    for i in range(n):
        n_vars   = random.randint(2, 8)
        n_constr = random.randint(3, 16)
        var_names = [f"x{j}" for j in range(n_vars)]
        variables = {name: z3.Int(name) for name in var_names}

        solver = z3.Solver()
        lines  = ["(set-logic QF_LIA)"]
        for name in var_names:
            lines.append(f"(declare-fun {name} () Int)")

        for _ in range(n_constr):
            coeffs = [random.randint(-5, 5) for _ in range(n_vars)]
            rhs    = random.randint(-20, 20)
            op     = random.choice(["<=", ">=", "="])
            lhs_terms = " ".join(
                f"(* {c} {v})" if c != 1 else v
                for c, v in zip(coeffs, var_names) if c != 0
            )
            if not lhs_terms:
                lhs_terms = "0"
            lhs_expr = f"(+ {lhs_terms})" if " " in lhs_terms else lhs_terms
            lines.append(f"(assert ({op} {lhs_expr} {rhs}))")

        lines += ["(check-sat)", "(get-model)"]
        fname = out_dir / f"synthetic_{i:05d}.smt2"
        fname.write_text("\n".join(lines))

    print(f"[synthetic] Done.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--logic",     default="QF_LIA")
    parser.add_argument("--max",       type=int, default=5000)
    parser.add_argument("--out",       default="data/benchmarks")
    parser.add_argument("--synthetic", action="store_true",
                        help="Generate synthetic benchmarks (fast bootstrap, no internet)")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.synthetic:
        generate_synthetic_qf_lia(out_dir / "synthetic", n=args.max)
        return

    try:
        download_via_git(args.logic, out_dir, args.max)
    except Exception as e:
        print(f"[warn] Git clone failed: {e}")
        print("[fallback] Generating synthetic benchmarks instead.")
        generate_synthetic_qf_lia(out_dir / "synthetic", n=min(args.max, 2000))


if __name__ == "__main__":
    main()
