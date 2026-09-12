"""Re-solve TracerX/KLEE's stuck queries with NeuroSym as a fallback.

Post-hoc only: this does NOT change what TracerX decided during exploration
(that would need TracerX's own C++ solver plugin modified -- a much bigger
change, deliberately out of scope here). It reads the SMT-LIB2 query log
TracerX writes with `-use-query-log=all:smt2`, splits it into individual
queries, and re-solves with NeuroSym only the ones TracerX's own solver
answered "unknown" or ran out of budget on -- reporting anything NeuroSym
can resolve that TracerX could not, as extra information about the run,
not a correction fed back into it.

KLEE's query log format (unverified against TracerX specifically -- confirm
against your own klee-out-N/all-queries.smt2 before trusting this):
    ; Query <N>
    (set-logic ...)
    (declare-fun ...)
    ...
    (assert ...)
    (check-sat)
    ; result: <SAT|UNSAT|UNKNOWN> in <seconds>s
one block per query, separated by the "; Query" comment marker. If your
log annotates results differently, QUERY_MARKER_RE / RESULT_RE below are
the two patterns to fix first.

Usage:
    python3 -m gansat.ns_klee_fallback KLEE_OUT_DIR/all-queries.smt2 \\
        --neurosym-main /path/to/NeuroSym/main.py
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

QUERY_MARKER_RE = re.compile(r"^; Query (\d+)", re.MULTILINE)
RESULT_RE = re.compile(
    r"; result:\s*(SAT|UNSAT|UNKNOWN)(?:\s+in\s+([\d.]+)s)?", re.IGNORECASE
)


def split_queries(log_text: str) -> list[tuple[int, str, str | None]]:
    """Split the log into (query_number, smt2_text, reported_result)."""
    markers = list(QUERY_MARKER_RE.finditer(log_text))
    if not markers:
        return []
    queries = []
    for i, m in enumerate(markers):
        start = m.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(log_text)
        block = log_text[start:end]
        result_m = RESULT_RE.search(block)
        result = result_m.group(1).upper() if result_m else None
        queries.append((int(m.group(1)), block, result))
    return queries


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("query_log", help="TracerX's SMT-LIB2 query log")
    ap.add_argument(
        "--neurosym-main",
        required=True,
        help="path to NeuroSym's main.py",
    )
    ap.add_argument(
        "--only",
        choices=["unknown", "all"],
        default="unknown",
        help="re-solve only queries TracerX marked UNKNOWN/timed out (default), "
        "or every query (useful for sanity-checking agreement)",
    )
    args = ap.parse_args()

    log_text = Path(args.query_log).read_text(errors="replace")
    queries = split_queries(log_text)
    if not queries:
        print(
            f"no '; Query N' markers found in {args.query_log} -- this "
            f"script's assumptions about the log format do not match what "
            f"TracerX actually wrote here; inspect the file and adjust "
            f"QUERY_MARKER_RE/RESULT_RE",
            file=sys.stderr,
        )
        return 1

    stuck = [
        (n, block)
        for n, block, result in queries
        if args.only == "all" or result in (None, "UNKNOWN")
    ]
    print(
        f"{len(queries)} queries in the log, {len(stuck)} selected for "
        f"NeuroSym fallback (--only={args.only})"
    )

    resolved = 0
    for n, block in stuck:
        formula_path = Path(f"/tmp/ns_klee_query_{n}.smt2")
        formula_path.write_text(block)
        proc = subprocess.run(
            ["python3", args.neurosym_main, str(formula_path)],
            capture_output=True,
            text=True,
        )
        verdict = "sat" if proc.stdout.strip().startswith("sat") else (
            "unsat" if proc.stdout.strip().startswith("unsat") else "unknown/error"
        )
        print(f"query {n}: TracerX=unknown, NeuroSym={verdict}")
        if verdict in ("sat", "unsat"):
            resolved += 1

    print(f"\nNeuroSym resolved {resolved}/{len(stuck)} queries TracerX left unknown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
