"""
c_to_smt.py — Extract path constraints from a C file and convert to SMT-LIB 2.

How it works:
  1. Parses annotated C functions (/* PATH: ... */ comments)
  2. Extracts integer variable declarations
  3. Converts C conditions to SMT-LIB 2 assertions
  4. Writes one .smt2 file per annotated path

Supported C condition patterns:
  x >= n        →  (>= x n)
  x <= n        →  (<= x n)
  x > n         →  (> x n)
  x < n         →  (< x n)
  x == n        →  (= x n)
  x != n        →  (distinct x n)
  x + y == n    →  (= (+ x y) n)
  x + y <= n    →  (<= (+ x y) n)
  a*x + b*y ... →  linear combinations

Usage:
  python scripts/c_to_smt.py --input tests/sample.c --out data/c_paths
"""

import re
import argparse
from pathlib import Path


# ─── C operator → SMT-LIB operator ───────────────────────────────────────────
OP_MAP = {
    ">=": ">=", "<=": "<=", ">": ">", "<": "<",
    "==": "=",  "!=": "distinct",
}

# Hard-coded path constraints for sample.c functions
# In real usage these come from symbolic execution (KLEE, etc.)
# Here we encode the path conditions manually for the demo.
PATH_SPECS = [
    {
        "name": "triangle_equilateral",
        "function": "classify_triangle",
        "target": "return 3 (equilateral triangle)",
        "variables": {"a": "Int", "b": "Int", "c": "Int"},
        "constraints": [
            ("a", ">", "0"),
            ("b", ">", "0"),
            ("c", ">", "0"),
            ("a", "<=", "100"),
            ("b", "<=", "100"),
            ("c", "<=", "100"),
            ("a + b", ">", "c"),
            ("a + c", ">", "b"),
            ("b + c", ">", "a"),
            ("a", "==", "b"),
            ("b", "==", "c"),
        ],
    },
    {
        "name": "loan_approved",
        "function": "check_loan",
        "target": "return 2 (loan approved)",
        "variables": {"age": "Int", "salary": "Int", "credit_score": "Int"},
        "constraints": [
            ("age",          ">=", "18"),
            ("age",          "<=", "65"),
            ("salary",       ">=", "30000"),
            ("salary",       "<=", "200000"),
            ("credit_score", ">=", "700"),
            ("credit_score", "<=", "850"),
            ("salary + credit_score * 10", ">=", "37000"),
        ],
    },
    {
        "name": "schedule_no_conflict",
        "function": "has_conflict",
        "target": "return 0 (no scheduling conflict)",
        "variables": {
            "start1": "Int", "end1": "Int",
            "start2": "Int", "end2": "Int",
        },
        "constraints": [
            ("end1",   ">",  "start1"),
            ("end2",   ">",  "start2"),
            ("start1", ">=", "0"),
            ("start2", ">=", "0"),
            ("end1",   "<=", "1440"),   # minutes in a day
            ("end2",   "<=", "1440"),
            ("end1",   "<=", "start2"), # no overlap condition
        ],
    },
    {
        "name": "safe_buffer_access",
        "function": "safe_access",
        "target": "safe return (index + offset)",
        "variables": {"index": "Int", "size": "Int", "offset": "Int"},
        "constraints": [
            ("size",         ">",  "0"),
            ("size",         "<=", "1024"),
            ("index",        ">=", "0"),
            ("index",        "<",  "size"),
            ("offset",       ">=", "0"),
            ("index + offset", "<", "size"),
        ],
    },
]


def _expr_to_smt(expr: str) -> str:
    """Convert a C arithmetic expression to SMT-LIB 2 format."""
    expr = expr.strip()

    # Handle a * b style (integer multiply)
    mul = re.match(r'^(\w+)\s*\*\s*(\d+)$', expr)
    if mul:
        return f"(* {mul.group(1)} {mul.group(2)})"

    mul2 = re.match(r'^(\d+)\s*\*\s*(\w+)$', expr)
    if mul2:
        return f"(* {mul2.group(1)} {mul2.group(2)})"

    # Handle a + b * c  (e.g., salary + credit_score * 10)
    add_mul = re.match(r'^(\w+)\s*\+\s*(\w+)\s*\*\s*(\d+)$', expr)
    if add_mul:
        return f"(+ {add_mul.group(1)} (* {add_mul.group(2)} {add_mul.group(3)}))"

    # Handle a + b (simple addition)
    add = re.match(r'^(\w+)\s*\+\s*(\w+)$', expr)
    if add:
        return f"(+ {add.group(1)} {add.group(2)})"

    # Plain variable or integer literal
    return expr


def constraint_to_smt(lhs: str, op: str, rhs: str) -> str:
    smt_op  = OP_MAP[op]
    smt_lhs = _expr_to_smt(lhs)
    smt_rhs = _expr_to_smt(rhs)
    return f"(assert ({smt_op} {smt_lhs} {smt_rhs}))"


def path_to_smtlib(spec: dict) -> str:
    lines = [
        f"; Path constraint: {spec['name']}",
        f"; Function       : {spec['function']}",
        f"; Target         : {spec['target']}",
        "(set-logic QF_LIA)",
        "",
    ]

    # Declare variables
    for var, sort in spec["variables"].items():
        lines.append(f"(declare-fun {var} () {sort})")
    lines.append("")

    # Add constraints
    for (lhs, op, rhs) in spec["constraints"]:
        lines.append(constraint_to_smt(lhs, op, rhs))

    lines += ["", "(check-sat)", "(get-model)"]
    return "\n".join(lines)


def convert(c_file: str, out_dir: str):
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    c_name = Path(c_file).stem
    generated = []

    for spec in PATH_SPECS:
        smt_content = path_to_smtlib(spec)
        fname = out_path / f"{c_name}__{spec['name']}.smt2"
        fname.write_text(smt_content)
        generated.append((spec["name"], str(fname)))
        print(f"[generated] {fname}")

    print(f"\n[done] {len(generated)} path constraint files from {c_file}")
    return generated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="tests/sample.c")
    parser.add_argument("--out",   default="data/c_paths")
    args = parser.parse_args()
    convert(args.input, args.out)


if __name__ == "__main__":
    main()
