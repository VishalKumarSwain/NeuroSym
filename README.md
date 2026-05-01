# NeuroSym — Neural-Symbolic SMT Solver

> A GAN-guided SMT solver targeting QF_LIA and QF_BV theories, submitted to **SMT-COMP 2026**.

NeuroSym combines a custom **Iterative Refinement GAN** with a complete Z3-based fallback to solve Satisfiability Modulo Theories (SMT) problems. The GAN learns to generate satisfying assignments directly from formula encodings, dramatically reducing the search space before falling back to symbolic reasoning.

---

## Features

- **Iterative Refinement GAN** — novel architecture: initial guess + K×violation-guided refinement rounds
- **QF_LIA support** — linear integer arithmetic (8576-dim formula encoding)
- **QF_BV support** — bit-vector theory (10752-dim encoding, 16 BV operation types)
- **KLEE plugin** — C++ `SolverImpl` backend to use NeuroSym as a KLEE solver
- **SMT-COMP ready** — SMT-LIB 2 interface via `main.py` + Dockerfile
- **Z3 fallback** — guaranteed completeness via Z3 when GAN candidates fail

---

## Architecture

```
Formula (SMT-LIB 2)
       │
       ▼
  BV/LIA Encoder  ──►  Feature Vector (10752-dim / 8576-dim)
       │
       ▼
 InitialGuesser   ──►  x̂⁽⁰⁾  (initial candidate assignment)
       │
       ▼
 RefinementStep × K  ──►  x̂⁽¹⁾ → x̂⁽²⁾ → ... → x̂⁽ᴷ⁾
  (violation-guided)
       │
       ▼
 Verify with Z3?  ──►  SAT → return model
       │ NO
       ▼
   Z3 Fallback    ──►  SAT / UNSAT / UNKNOWN
```

---

## Quick Start

### Install dependencies
```bash
pip install -r requirements.txt
```

### Run solver (SMT-COMP interface)
```bash
# From file
python main.py benchmark.smt2

# From stdin
echo "(set-logic QF_LIA)..." | python main.py --stdin
```

### Run tests
```bash
python tests/test_pipeline.py       # 11 QF_LIA tests
python tests/test_bv_pipeline.py    # 14 QF_BV tests
```

### Train the GAN
```bash
# Generate synthetic benchmarks + train QF_BV model
python scripts/train_bv.py --synthetic --data data/bv_benchmarks --epochs 50

# Download real benchmarks + train
python scripts/download_bv_benchmarks.py --out data/bv_benchmarks
python scripts/train_bv.py --data data/bv_benchmarks --epochs 50 --out models/gansat_bv.pt
```

### Docker (SMT-COMP submission)
```bash
docker build -t neurosym .
echo "(set-logic QF_LIA)..." | docker run -i neurosym --stdin
```

---

## KLEE Integration

Patch an existing KLEE source tree to use NeuroSym as a solver backend:

```bash
chmod +x klee_plugin/patch_klee.sh
./klee_plugin/patch_klee.sh /path/to/klee/source

# Build KLEE
cd /path/to/klee/build && cmake .. -DENABLE_SOLVER_Z3=ON && make -j$(nproc)

# Run KLEE with NeuroSym
klee --solver-backend=gansat --gansat-model=models/gansat_bv.pt program.bc
```

---

## Project Structure

```
NeuroSym/
├── gansat/
│   ├── parser.py        # SMT-LIB 2 parser (Z3-based)
│   ├── encoder.py       # QF_LIA feature encoder (8576-dim)
│   ├── bv_encoder.py    # QF_BV feature encoder (10752-dim)
│   ├── gan.py           # Iterative Refinement GAN (QF_LIA)
│   ├── bv_gan.py        # Iterative Refinement GAN (QF_BV)
│   └── solver.py        # Unified solver dispatcher
├── klee_plugin/
│   ├── gansat_solver.h/.cpp   # KLEE C++ SolverImpl
│   ├── gansat_bridge.py       # Python subprocess bridge
│   ├── patch_klee.sh          # Auto-patch KLEE source
│   └── CMakeLists.txt
├── scripts/
│   ├── train.py               # QF_LIA GAN training
│   ├── train_bv.py            # QF_BV GAN training
│   └── download_bv_benchmarks.py
├── tests/
│   ├── test_pipeline.py       # 11 QF_LIA tests
│   └── test_bv_pipeline.py    # 14 QF_BV tests
├── main.py                    # SMT-COMP entry point
├── Dockerfile
└── requirements.txt
```

---

## Mathematical Details

See [GANSAT_Mathematical_Details.md](GANSAT_Mathematical_Details.md) for:
- Full formula encoding equations
- ViolationComputer derivation
- Iterative refinement forward pass
- Training objectives and gradient flow
- Parameter counts (~12.1M total)

---

## Results

| Theory | Test Cases | SAT Accuracy | Avg Latency |
|--------|-----------|-------------|-------------|
| QF_LIA | 11 | 100% (Z3 fallback) | ~25ms |
| QF_BV  | 14 | 100% (Z3 fallback) | ~20ms |

---

## Authors

* **Vishal Kumar Swain** (NIT Warangal)
* **Sangharatna Godboley** (NIT Warangal)
* **P. Radha Krishna** (NIT Warangal)
* **Avijit Das** (DRDO, LRDE Bengaluru)
* **Bhaskar Sri Viswaroopanand** (Manipal University Jaipur)

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

