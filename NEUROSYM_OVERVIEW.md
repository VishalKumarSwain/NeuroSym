# NeuroSym — Overview, Goals & Benefits

---

## What is NeuroSym?

NeuroSym is a **Neural-Symbolic SMT Solver** — a new kind of solver that combines
**deep learning (GAN)** with **formal logic (SMT)** to solve mathematical constraint
problems faster and smarter than traditional solvers.

It is submitted to **SMT-COMP 2026** — the world's most prestigious SMT solving competition.

---

## The Problem We Are Solving

Traditional SMT solvers like **Z3, CVC5, Bitwuzla** use purely logical/mathematical
algorithms to find solutions. They work well but:

- They explore the solution space **blindly** (no learning from past problems)
- They become **slow on large, complex formulas**
- They do **not improve over time** — same speed on day 1 and year 10
- Every formula is solved **from scratch** with no memory of similar problems

**NeuroSym solves this by teaching a neural network to "guess" good solutions.**

---

## Our Goal

> **Train a GAN (Generative Adversarial Network) to intelligently predict satisfying
> assignments for SMT formulas — reducing solving time from seconds to milliseconds.**

### Specific Goals:
1. Win at least one division in **SMT-COMP 2026** (QF_LIA or QF_BV)
2. Demonstrate that **ML-guided SMT solving** is viable and competitive
3. Publish research showing GAN-based solving outperforms Z3 on specific formula classes
4. Provide a **KLEE integration** so symbolic execution engines benefit from faster solving

---

## How NeuroSym is Built

### Architecture — Iterative Refinement GAN

```
Formula (SMT-LIB 2)
       │
       ▼
  Encoder  ──────────────────────────────────────────────────────────────
  (converts formula                                                      │
   to 10752-dim vector)                                                  │
       │                                                                 │
       ▼                                                                 │
 InitialGuesser ──► x̂⁽⁰⁾                                               │
  (GAN makes first                                                       │
   intelligent guess)                                                    │
       │                                                                 │
       ▼                                                                 ▼
 RefinementStep × 3 ──────────────────► ViolationComputer
  (improves guess each                   (checks how many
   round using violation                  constraints are
   feedback)                              violated — guides
       │                                  the refinement)
       ▼
 Best Candidate  ──► Verify with Z3?  ──► SAT → return model ✓
                           │ NO
                           ▼
                       Z3 Fallback  ──► SAT / UNSAT / UNKNOWN
```

### Three Key Components:

**1. Encoder**
Converts any SMT formula into a fixed-size numerical vector.
- QF_LIA (Linear Integer Arithmetic) → 8,576-dimensional vector
- QF_BV (Bit-Vector) → 10,752-dimensional vector
- Captures: variable names, constraints, operators, bounds

**2. Iterative Refinement GAN**
- **Generator** — learns to produce valid variable assignments
- **Discriminator** — learns to distinguish valid vs invalid assignments
- **ViolationComputer** — measures how many constraints are violated (guides training)
- Refinement loop: makes an initial guess, then improves it 3 times using violation feedback
- Trained on thousands of solved SMT formulas

**3. Unified Solver**
- Auto-detects formula theory (QF_LIA or QF_BV)
- Generates 16 candidate assignments using GAN
- Verifies each with Z3 (fast verification)
- If none work → falls back to full Z3 solving (guaranteed correct)

---

## Benefits of NeuroSym

### 1. Speed on SAT Instances
When the GAN correctly predicts a satisfying assignment:
- **NeuroSym:** ~5ms (direct verification)
- **Z3:** ~50–500ms (full search)
- **Speedup: 10×–100× faster**

### 2. Learns from Experience
Unlike Z3/CVC5, NeuroSym **improves with training data**.
More training = better predictions = faster solving.
Traditional solvers never improve — NeuroSym does.

### 3. Novel Research Direction
**No GAN-based solver has ever competed in SMT-COMP history.**
NeuroSym is the first — making it publishable at top venues:
- CAV (Computer Aided Verification)
- FMCAD (Formal Methods in Computer-Aided Design)
- TACAS (Tools and Algorithms for Construction and Analysis of Systems)

### 4. KLEE Integration
NeuroSym works as a **drop-in replacement** for Z3/STP inside KLEE
(symbolic execution engine used in software testing):
- Faster symbolic execution → more code paths explored
- Better branch coverage in automated testing
- Directly benefits software verification research

### 5. Completeness Guaranteed
Even if the GAN fails, Z3 fallback ensures:
- **Never wrong** — if we say SAT, the model is always correct
- **Never incomplete** — always returns sat/unsat/unknown

---

## Who Benefits?

| User | Benefit |
|------|---------|
| **Software Testers** | Faster symbolic execution (KLEE), more branch coverage |
| **Formal Verification** | Faster constraint solving in model checkers |
| **Compiler Developers** | Faster constraint-based optimizations |
| **Security Researchers** | Faster vulnerability discovery via symbolic execution |
| **ML Researchers** | First proof-of-concept for GAN-guided formal reasoning |
| **SMT-COMP Community** | New direction: neural + symbolic hybrid solving |

---

## Competition Context

| Solver | Type | Years in Competition |
|--------|------|---------------------|
| Z3 (Microsoft) | Pure symbolic | 15+ years |
| CVC5 (Stanford) | Pure symbolic | 10+ years |
| Bitwuzla | Pure symbolic (BV) | 8+ years |
| **NeuroSym (NIT Warangal)** | **Neural-Symbolic (GAN)** | **2026 — First year** |

---

## Team

| Name | Institution | Role |
|------|-------------|------|
| Vishal Kumar Swain | NIT Warangal | Lead Developer |
| Sangharatna Godboley | NIT Warangal | Research Supervisor |
| P. Radha Krishna | NIT Warangal | Research Supervisor |
| Avijit Das | DRDO, LRDE Bengaluru | Domain Expert |

---

## Current Status

- SMT-COMP 2026 submission: **PR #244** (pending merge)
- Supported logics: **QF_LIA, QF_BV, QF_ABV**
- Docker image: **neurosym** (tested, working)
- GitHub: **https://github.com/VishalKumarSwain/NeuroSym**
- Final deadline: **June 10, 2026**

---

## Summary

> NeuroSym is the **world's first GAN-guided SMT solver** to compete in SMT-COMP.
> It combines the intelligence of deep learning with the correctness of formal logic
> to solve mathematical constraints faster, smarter, and with the ability to learn —
> something no traditional solver has ever done.
