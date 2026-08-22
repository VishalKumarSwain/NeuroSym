"""
External-solver fallback — a safety net, not a shortcut.

NeuroSym's own pipeline can legitimately return "unknown" even with the CNF
search running through MiniSat (ns_minisat.py, a mature compiled SAT
solver) instead of a from-scratch Python DPLL -- MiniSat only ever sees the
formula *after* our own bit-blaster has already turned it into CNF, and a
genuinely large or theory-heavy formula (arrays, LIA) can still exceed the
time budget upstream of that. Rather than report "unknown" and leave the
caller with nothing, fall back to a real whole-formula SMT solver on the
exact same query before giving up -- this never makes NeuroSym's own answer
wrong (its own sat/unsat verdicts are never second-guessed, only its
"I couldn't decide" case is covered), and it means a caller always gets a
real verdict when one exists within the time budget.

Boolector before z3: boolector is the faster of the two on the bitvector
formulas this project actually deals with, so it goes first; z3 is the
more general fallback, tried only if boolector is unavailable or also
inconclusive.
"""

import re
import subprocess
import sys
import tempfile
import os

from .ns_ast import ArraySort, App

RESULT_SAT     = "sat"
RESULT_UNSAT   = "unsat"
RESULT_UNKNOWN = "unknown"

_FALLBACK_SOLVERS = ["boolector", "z3"]

_ARRAY_OPS = frozenset(("select", "store", "as_const"))


def formula_uses_arrays(formula) -> bool:
    """True if `formula` touches array theory anywhere -- a declared
    array-sorted variable, or a select/store/as_const term. Measured
    directly (test.c, an array-touching ESBMC-generated formula): NeuroSym's
    own DPLL took 60+s and still returned unknown on a formula z3 solved in
    under 10s -- ESBMC's memory model routes array/pointer access through
    machinery that blows a modest C program up into a formula with over a
    million boolean variables after bit-blasting, and there is no upside in
    waiting out NeuroSym's own --timeout on that class of formula first. If
    this check is true, skip straight to the external fallback instead."""
    if any(isinstance(v.sort, ArraySort) for v in formula.variables.values()):
        return True

    seen = set()

    def walk(term) -> bool:
        tid = id(term)
        if tid in seen:
            return False
        seen.add(tid)
        if isinstance(term, App):
            if term.op in _ARRAY_OPS:
                return True
            return any(walk(a) for a in term.args)
        return False

    return any(walk(a) for a in formula.assertions)


def _ensure_get_model(smtlib_str: str) -> str:
    """Make sure the query actually asks for a model on sat, so we can
    extract an assignment -- some callers (e.g. ESBMC) omit (get-model)
    since they extract counterexamples through a different channel."""
    if "get-model" in smtlib_str:
        return smtlib_str
    return smtlib_str.rstrip() + "\n(get-model)\n"


def _next_token(text: str, i: int):
    """Read one S-expression token starting at text[i:] (skipping leading
    whitespace): either a balanced-paren group or a bare atom. Returns
    (token_text, index_just_past_it). A naive [^)]+-style regex breaks the
    moment the sort or value itself contains parens (e.g. z3 emits the sort
    and value on separate lines: '(_ BitVec 8)\\n    #x2a)' -- a lazy
    non-paren-aware match stops at the sort's own closing paren)."""
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text):
        return "", i
    if text[i] == "(":
        depth, start = 0, i
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1], i + 1
            i += 1
        return text[start:], i
    start = i
    while i < len(text) and not text[i].isspace() and text[i] not in "()":
        i += 1
    return text[start:i], i


def _parse_model(text: str) -> dict:
    """Parse `(define-fun NAME () SORT VALUE)` blocks (the format z3 and
    boolector's SMT-LIB2 mode emit) into {name: int}. Token-scans rather
    than regex-matches so a parenthesized sort (e.g. `(_ BitVec 8)`) can't
    be mistaken for the value that follows it."""
    model = {}
    marker = "(define-fun"
    pos = 0
    while True:
        idx = text.find(marker, pos)
        if idx == -1:
            break
        i = idx + len(marker)
        name_tok, i = _next_token(text, i)
        name = name_tok.strip("|")
        args_tok, i = _next_token(text, i)   # "()" -- the (empty) arg list
        _sort_tok, i = _next_token(text, i)  # the return sort, discarded
        val_tok, i = _next_token(text, i)
        model[name] = _parse_value(val_tok)
        pos = i
    return model


def _parse_value(val: str):
    val = val.strip()
    if val.startswith("#x"):
        return int(val[2:], 16)
    if val.startswith("#b"):
        return int(val[2:], 2)
    m = re.match(r'\(_\s+bv(\d+)\s+\d+\)', val)
    if m:
        return int(m.group(1))
    if val == "true":
        return 1
    if val == "false":
        return 0
    try:
        return int(val)
    except ValueError:
        return 0


def _parse_boolector_model(text: str) -> dict:
    """Boolector's -m output is its own plain format, not SMT-LIB2:
    'name 00101010' per line -- a name then raw binary digits (width
    matching the variable's declared sort), not '(define-fun ...)'.
    Reusing the z3/SMT-LIB2 parser on this would silently return nothing."""
    model = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r'[01]+', parts[1]):
            model[parts[0]] = int(parts[1], 2)
    return model


def try_external_fallback(smtlib_str: str, timeout_s: float = 30.0):
    """Run a real solver on the same formula NeuroSym couldn't resolve.

    Returns (result, model_dict) where result is one of RESULT_SAT /
    RESULT_UNSAT / RESULT_UNKNOWN. Tries each solver in _FALLBACK_SOLVERS
    in order, moving to the next only if one is unavailable or itself
    inconclusive within its share of the time budget."""
    # boolector's SMT-LIB2 parser doesn't understand the (get-model)
    # *command* at all -- it errors out on it even with -m passed on the
    # command line, which is boolector's own (non-SMT-LIB2) way of asking
    # for a model. So each solver gets the query written the way it
    # actually expects, not one shared "add get-model" query.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".smt2", delete=False
    ) as f:
        f.write(_ensure_get_model(smtlib_str))
        tmp_path_with_model = f.name
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".smt2", delete=False
    ) as f:
        f.write(smtlib_str)
        tmp_path_plain = f.name

    try:
        per_solver_timeout = timeout_s / len(_FALLBACK_SOLVERS)
        for solver_bin in _FALLBACK_SOLVERS:
            if solver_bin == "boolector":
                cmd = [solver_bin, "--smt2", "-m", tmp_path_plain]
                parse = _parse_boolector_model
            else:
                cmd = [solver_bin, tmp_path_with_model]
                parse = _parse_model

            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True, text=True,
                    timeout=per_solver_timeout,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue

            # The verdict is scanned line-by-line, not out.startswith(...):
            # boolector prints a "[btorsmt2] WARNING ..." line on stdout
            # (not stderr) *before* the actual sat/unsat/unknown line
            # whenever the query has no trailing (exit), which every one
            # of these queries does -- startswith would silently treat
            # every boolector run as unparseable and move on.
            out = proc.stdout
            verdict = None
            for line in out.splitlines():
                stripped = line.strip()
                if stripped in ("sat", "unsat", "unknown"):
                    verdict = stripped
                    break

            if verdict == "unsat":
                return RESULT_UNSAT, None
            if verdict == "sat":
                return RESULT_SAT, parse(out)
            # "unknown", or no recognizable verdict line -- try the next solver
        return RESULT_UNKNOWN, None
    finally:
        for p in (tmp_path_with_model, tmp_path_plain):
            try:
                os.unlink(p)
            except OSError:
                pass
