"""
Download real QF_BV benchmarks from:
  1. bitwuzla regression suite (749 BV files)
  2. STP regression suite (75 files)
Then generate high-quality synthetic benchmarks to reach target count.

Usage:
    python scripts/download_bv_benchmarks.py --out data/bv_benchmarks --synthetic 3000
"""

import argparse
import json
import random
import sys
import time
import urllib.request
from pathlib import Path

BITWUZLA_TREE_URL = (
    "https://api.github.com/repos/bitwuzla/bitwuzla/git/trees/HEAD?recursive=1"
)
BITWUZLA_RAW = "https://raw.githubusercontent.com/bitwuzla/bitwuzla/main/"

STP_TREE_URL = "https://api.github.com/repos/stp/stp/git/trees/HEAD?recursive=1"
STP_RAW = "https://raw.githubusercontent.com/stp/stp/master/"


def _fetch_json(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _download_file(raw_base, path, dest: Path, retries=3):
    url = raw_base + path
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                dest.write_bytes(r.read())
            return True
        except Exception:
            if attempt < retries - 1:
                time.sleep(1)
    return False


def download_bitwuzla(out_dir: Path, max_files: int = 750):
    print("[bitwuzla] Fetching file tree...")
    try:
        tree = _fetch_json(BITWUZLA_TREE_URL)
    except Exception as e:
        print(f"[bitwuzla] Failed to fetch tree: {e}")
        return 0

    bv_files = [
        x["path"] for x in tree.get("tree", [])
        if x["path"].endswith(".smt2") and "bv" in x["path"].lower()
    ]
    bv_files = bv_files[:max_files]
    print(f"[bitwuzla] Found {len(bv_files)} BV .smt2 files — downloading...")

    dest_dir = out_dir / "bitwuzla"
    dest_dir.mkdir(parents=True, exist_ok=True)
    ok = 0
    for i, path in enumerate(bv_files):
        fname = dest_dir / f"bwz_{i:04d}_{Path(path).name}"
        if fname.exists():
            ok += 1
            continue
        if _download_file(BITWUZLA_RAW, path, fname):
            ok += 1
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(bv_files)}  ({ok} ok)")
    print(f"[bitwuzla] Downloaded {ok}/{len(bv_files)}")
    return ok


def download_stp(out_dir: Path):
    print("[stp] Fetching file tree...")
    try:
        tree = _fetch_json(STP_TREE_URL)
    except Exception as e:
        print(f"[stp] Failed: {e}")
        return 0

    smt_files = [x["path"] for x in tree.get("tree", []) if x["path"].endswith(".smt2")]
    print(f"[stp] Found {len(smt_files)} .smt2 files — downloading...")

    dest_dir = out_dir / "stp"
    dest_dir.mkdir(parents=True, exist_ok=True)
    ok = 0
    for i, path in enumerate(smt_files):
        fname = dest_dir / f"stp_{i:04d}_{Path(path).name}"
        if fname.exists():
            ok += 1
            continue
        if _download_file(STP_RAW, path, fname):
            ok += 1
    print(f"[stp] Downloaded {ok}/{len(smt_files)}")
    return ok


# ── High-quality synthetic generator ──────────────────────────────────────────

def _bv_literal(width, maxval):
    return f"(_ bv{random.randint(0, maxval)} {width})"


def _bv_arith(v1, v2, width):
    op = random.choice(["bvadd", "bvsub", "bvmul"])
    return f"({op} {v1} {v2})"


def generate_klee_style(out_dir: Path, n: int):
    """Generate KLEE-like path constraint patterns."""
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for i in range(n):
        width  = random.choice([8, 16, 32, 32, 32])  # bias 32-bit like KLEE
        n_vars = random.randint(1, 4)
        maxval = (1 << width) - 1
        var_names = [f"symb_{j}" for j in range(n_vars)]

        lines = [
            "(set-logic QF_BV)",
            *[f"(declare-fun {v} () (_ BitVec {width}))" for v in var_names],
        ]

        # KLEE-style: range bounds on each variable
        for v in var_names:
            lo = random.randint(0, maxval // 4)
            hi = random.randint(maxval // 2, maxval - 1)
            lines.append(f"(assert (bvuge {v} (_ bv{lo} {width})))")
            lines.append(f"(assert (bvule {v} (_ bv{hi} {width})))")

        # Relationship constraints
        n_rel = random.randint(1, 4)
        for _ in range(n_rel):
            v1 = random.choice(var_names)
            v2 = random.choice(var_names)
            if random.random() < 0.5 and n_vars >= 2 and v1 != v2:
                # Arithmetic relation: expr op literal
                expr = _bv_arith(v1, v2, width)
                target = random.randint(1, maxval // 2)
                op = random.choice(["bvult", "bvule", "bvugt", "bvuge", "="])
                lines.append(f"(assert ({op} {expr} (_ bv{target} {width})))")
            else:
                # Inequality: v1 != v2 or v1 op v2
                op = random.choice(["distinct", "bvult", "bvugt"])
                lines.append(f"(assert ({op} {v1} {v2}))")

        # Negation constraint (exclude specific value)
        if random.random() < 0.5:
            v = random.choice(var_names)
            exc = random.randint(1, maxval - 1)
            lines.append(f"(assert (not (= {v} (_ bv{exc} {width}))))")

        lines += ["(check-sat)", "(get-model)"]
        (out_dir / f"klee_{i:05d}.smt2").write_text("\n".join(lines))
        count += 1
    return count


def generate_bitwise_patterns(out_dir: Path, n: int):
    """Benchmarks emphasizing bitwise ops — common in crypto/protocol code."""
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for i in range(n):
        width  = random.choice([8, 16, 32])
        n_vars = random.randint(2, 5)
        maxval = (1 << width) - 1
        var_names = [f"b{j}" for j in range(n_vars)]

        lines = [
            "(set-logic QF_BV)",
            *[f"(declare-fun {v} () (_ BitVec {width}))" for v in var_names],
        ]

        for _ in range(random.randint(2, 8)):
            v1 = random.choice(var_names)
            v2 = random.choice(var_names)
            bwop = random.choice(["bvand", "bvor", "bvxor"])
            target = random.randint(0, maxval)
            lines.append(f"(assert (= ({bwop} {v1} {v2}) (_ bv{target} {width})))")

        # At least one inequality to make it interesting
        if n_vars >= 2:
            v1, v2 = random.sample(var_names, 2)
            lines.append(f"(assert (distinct {v1} {v2}))")

        lines += ["(check-sat)", "(get-model)"]
        (out_dir / f"bitwise_{i:05d}.smt2").write_text("\n".join(lines))
        count += 1
    return count


def generate_signed_patterns(out_dir: Path, n: int):
    """Benchmarks with signed comparisons — covers bvslt/bvsgt/bvsle/bvsge."""
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for i in range(n):
        width  = random.choice([8, 16, 32])
        n_vars = random.randint(2, 4)
        half   = 1 << (width - 1)
        var_names = [f"s{j}" for j in range(n_vars)]

        lines = [
            "(set-logic QF_BV)",
            *[f"(declare-fun {v} () (_ BitVec {width}))" for v in var_names],
        ]

        # Mix of signed and unsigned constraints
        for _ in range(random.randint(3, 8)):
            v1 = random.choice(var_names)
            op = random.choice(["bvslt", "bvsle", "bvsgt", "bvsge",
                                 "bvult", "bvult", "bvugt"])  # bias unsigned
            # Small signed literal (both negative and positive representable)
            lit = random.randint(1, min(100, half - 1))
            lines.append(f"(assert ({op} {v1} (_ bv{lit} {width})))")

        lines += ["(check-sat)", "(get-model)"]
        (out_dir / f"signed_{i:05d}.smt2").write_text("\n".join(lines))
        count += 1
    return count


def generate_mixed_arith(out_dir: Path, n: int):
    """Mixed arithmetic + comparison — closest to real program constraints."""
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for i in range(n):
        width  = random.choice([8, 16, 32, 64])
        n_vars = random.randint(2, 6)
        maxval = min((1 << width) - 1, (1 << 32) - 1)
        var_names = [f"x{j}" for j in range(n_vars)]

        lines = [
            "(set-logic QF_BV)",
            *[f"(declare-fun {v} () (_ BitVec {width}))" for v in var_names],
        ]

        for _ in range(random.randint(3, 12)):
            v1 = random.choice(var_names)
            v2 = random.choice(var_names)
            lit = random.randint(0, maxval // 4)
            kind = random.random()

            if kind < 0.3:
                # Arithmetic equality: v1 + v2 = lit
                lines.append(f"(assert (= (bvadd {v1} {v2}) (_ bv{lit} {width})))")
            elif kind < 0.5:
                # Arithmetic inequality: v1 - v2 < lit
                op = random.choice(["bvult", "bvugt", "bvule", "bvuge"])
                lines.append(f"(assert ({op} (bvsub {v1} {v2}) (_ bv{lit} {width})))")
            elif kind < 0.7:
                # Simple range
                lo = random.randint(1, max(1, lit))
                hi = random.randint(lo + 1, min(maxval, lo + maxval // 4))
                lines.append(f"(assert (bvuge {v1} (_ bv{lo} {width})))")
                lines.append(f"(assert (bvule {v1} (_ bv{hi} {width})))")
            else:
                # Bitwise + comparison
                bwop = random.choice(["bvand", "bvor"])
                op = random.choice(["bvult", "bvugt", "="])
                lines.append(f"(assert ({op} ({bwop} {v1} {v2}) (_ bv{lit} {width})))")

        lines += ["(check-sat)", "(get-model)"]
        (out_dir / f"mixed_{i:05d}.smt2").write_text("\n".join(lines))
        count += 1
    return count


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out",       default="data/bv_benchmarks")
    parser.add_argument("--synthetic", type=int, default=3000,
                        help="Number of synthetic benchmarks to generate")
    parser.add_argument("--no_real",   action="store_true",
                        help="Skip downloading real benchmarks")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    total = 0

    if not args.no_real:
        total += download_bitwuzla(out)
        total += download_stp(out)

    if args.synthetic > 0:
        n = args.synthetic
        q1, q2, q3, q4 = n // 4, n // 4, n // 4, n - 3 * (n // 4)
        print(f"[synthetic] Generating {n} benchmarks ({q1} KLEE + {q2} bitwise "
              f"+ {q3} signed + {q4} mixed-arith)...")
        total += generate_klee_style(out / "klee_style", q1)
        total += generate_bitwise_patterns(out / "bitwise", q2)
        total += generate_signed_patterns(out / "signed", q3)
        total += generate_mixed_arith(out / "mixed_arith", q4)

    all_files = list(out.rglob("*.smt2"))
    print(f"\n[done] {len(all_files)} total .smt2 files in {out}")


if __name__ == "__main__":
    main()
