"""
train_bv.py — GAN training script for QF_BV (bit-vector theory).

Same pipeline as train.py but uses BVIterativeGenerator, BVDiscriminator,
and bv_encode/bv_decode for KLEE-compatible bit-vector formulas.

Usage:
    python scripts/train_bv.py --data data/bv_benchmarks --out models/gansat_bv.pt

To generate synthetic QF_BV training data:
    python scripts/download_benchmarks.py --logic QF_BV --synthetic --max 2000
"""

import argparse
import sys
import random
import json
import subprocess
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import z3
from gansat.parser      import parse_file
from gansat.bv_encoder  import bv_encode, bv_decode_assignment, bv_feature_dim, MAX_VARS
from gansat.bv_gan      import (
    BVIterativeGenerator, BVDiscriminator, bv_assignment_to_tensor, BV_NOISE_DIM
)

# ── Subprocess worker entrypoint (called as __main__) ─────────────────────────
# When invoked as: python train_bv.py --_worker <path> <max_ms>
# Prints JSON result to stdout, exits 0 on success or 1 on failure.
def _worker_main():
    path       = sys.argv[sys.argv.index("--_worker") + 1]
    max_ms     = int(sys.argv[sys.argv.index("--_worker") + 2])
    try:
        formula = parse_file(path)
        if not formula.variables:
            sys.exit(1)
        if formula.logic not in ("QF_BV", "QF_ABV", "BV", "UNKNOWN"):
            sys.exit(1)
        solver = z3.Solver()
        solver.set("timeout", max_ms)
        for a in formula.assertions:
            solver.add(a)
        if solver.check() != z3.sat:
            sys.exit(1)
        model = solver.model()
        widths = {}
        assignment = {}
        for d in model.decls():
            val = model[d]
            if val is None:
                continue
            if z3.is_bv_value(val):
                widths[str(d)]     = val.sort().size()
                assignment[str(d)] = val.as_long()
        if not assignment:
            sys.exit(1)
        formula_enc = bv_encode(formula).tolist()
        assign_enc  = bv_assignment_to_tensor(
            assignment, formula.var_names, widths
        ).numpy().tolist()
        print(json.dumps({"fe": formula_enc, "ae": assign_enc}))
        sys.exit(0)
    except Exception:
        sys.exit(1)


def _process_file_with_timeout(path, max_solve_ms, timeout_sec=15):
    """Process a single SMT2 file in an isolated subprocess — crash-safe."""
    try:
        result = subprocess.run(
            [sys.executable, __file__, "--_worker", str(path), str(max_solve_ms)],
            capture_output=True, text=True, timeout=timeout_sec
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            return (np.array(data["fe"], dtype=np.float32),
                    np.array(data["ae"], dtype=np.float32))
    except Exception:
        pass
    return None


# ── Dataset ───────────────────────────────────────────────────────────────────

class BVSMTDataset(Dataset):
    def __init__(self, smt2_files: list, max_solve_ms: int = 5000,
                 file_timeout_sec: int = 15, num_workers: int = 8):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        self.samples = []
        print(f"[bv_dataset] Solving {len(smt2_files)} QF_BV benchmarks "
              f"(timeout={file_timeout_sec}s/file, {num_workers} parallel workers)...")

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(_process_file_with_timeout, f, max_solve_ms, file_timeout_sec): f
                for f in smt2_files
            }
            for future in tqdm(as_completed(futures), total=len(smt2_files)):
                try:
                    sample = future.result()
                    if sample is not None:
                        self.samples.append(sample)
                except Exception:
                    pass

        print(f"[bv_dataset] Collected {len(self.samples)} BV SAT training pairs.")

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        fe, ae = self.samples[idx]
        return (
            torch.tensor(fe, dtype=torch.float32),
            torch.tensor(ae, dtype=torch.float32),
        )


# ── Synthetic BV benchmark generator ─────────────────────────────────────────

def generate_synthetic_bv(out_dir: Path, n: int = 1000):
    """Generate random QF_BV benchmarks for fast bootstrapping."""
    import random
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[synthetic_bv] Generating {n} QF_BV benchmarks → {out_dir}")

    for i in range(n):
        width  = random.choice([8, 16, 32])
        n_vars = random.randint(2, 6)
        n_con  = random.randint(3, 10)
        maxval = (1 << width) - 1

        var_names = [f"x{j}" for j in range(n_vars)]
        lines = [
            f"(set-logic QF_BV)",
            *[f"(declare-fun {v} () (_ BitVec {width}))" for v in var_names],
        ]

        for _ in range(n_con):
            v1  = random.choice(var_names)
            v2  = random.choice(var_names)
            lit = random.randint(0, maxval // 2)
            op  = random.choice(["bvult", "bvule", "bvugt", "bvuge"])
            if random.random() < 0.4:
                # Constraint between two vars
                lines.append(f"(assert ({op} {v1} {v2}))")
            else:
                # Constraint vs literal
                lines.append(f"(assert ({op} {v1} (_ bv{lit} {width})))")

        # Add an equality to make it more interesting
        if n_vars >= 2:
            v1, v2 = random.sample(var_names, 2)
            lit    = random.randint(1, maxval // 4)
            lines.append(f"(assert (= (bvadd {v1} {v2}) (_ bv{lit} {width})))")

        lines += ["(check-sat)", "(get-model)"]
        (out_dir / f"synthetic_bv_{i:05d}.smt2").write_text("\n".join(lines))

    print(f"[synthetic_bv] Done.")


# ── Training loop ─────────────────────────────────────────────────────────────

def train_bv(
    data_dir:         str,
    out_path:         str,
    epochs:           int   = 50,
    batch_size:       int   = 32,
    lr:               float = 1e-4,
    n_d_steps:        int   = 2,
    device_str:       str   = "cpu",
    checkpoint_every: int   = 10,
    synthetic:        bool  = False,
    n_synthetic:      int   = 2000,
):
    device = torch.device(device_str)

    # Generate synthetic data if requested
    if synthetic:
        syn_dir = Path(data_dir) / "synthetic_bv"
        generate_synthetic_bv(syn_dir, n=n_synthetic)

    files = list(Path(data_dir).rglob("*.smt2"))
    random.shuffle(files)
    print(f"[train_bv] Found {len(files)} .smt2 files")

    dataset = BVSMTDataset(files)
    if len(dataset) == 0:
        print("[error] No BV training pairs. Run with --synthetic first.")
        sys.exit(1)

    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=0)

    G   = BVIterativeGenerator().to(device)
    D   = BVDiscriminator().to(device)
    optG = optim.Adam(G.parameters(), lr=lr, betas=(0.5, 0.999))
    optD = optim.Adam(D.parameters(), lr=lr, betas=(0.5, 0.999))
    crit = nn.BCEWithLogitsLoss()
    lam  = 0.5   # violation loss weight

    best_g = float("inf")
    history = {"loss_d": [], "loss_g": []}

    print(f"[train_bv] Epochs={epochs}  Batch={batch_size}  Device={device}")

    for epoch in range(1, epochs + 1):
        G.train(); D.train()
        sum_d = sum_g = n = 0

        for f_enc, real_assign in loader:
            f_enc       = f_enc.to(device)
            real_assign = real_assign.to(device)
            B           = f_enc.size(0)

            # Discriminator
            for _ in range(n_d_steps):
                optD.zero_grad()
                with torch.no_grad():
                    fake = G(f_enc)
                loss_d = 0.5 * (
                    crit(D(f_enc, real_assign), torch.ones(B, device=device)) +
                    crit(D(f_enc, fake),        torch.zeros(B, device=device))
                )
                loss_d.backward(); optD.step()

            # Generator
            optG.zero_grad()
            fake    = G(f_enc)
            loss_g  = crit(D(f_enc, fake), torch.ones(B, device=device))
            loss_v  = G.violation_score(f_enc, fake).mean()
            loss_g  = loss_g + lam * loss_v
            loss_g.backward(); optG.step()

            sum_d += loss_d.item(); sum_g += loss_g.item(); n += 1

        avg_d = sum_d / max(n, 1)
        avg_g = sum_g / max(n, 1)
        history["loss_d"].append(avg_d)
        history["loss_g"].append(avg_g)
        print(f"[epoch {epoch:03d}/{epochs}] loss_D={avg_d:.4f}  loss_G={avg_g:.4f}")

        if epoch % checkpoint_every == 0 or epoch == epochs:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            torch.save(G.state_dict(), out_path)
            print(f"[checkpoint] -> {out_path}")

        if avg_g < best_g:
            best_g = avg_g
            torch.save(G.state_dict(), out_path.replace(".pt", "_best.pt"))

    np.save(out_path.replace(".pt", "_history.npy"), history)
    print(f"[done] Best G loss: {best_g:.4f}")


def main():
    if "--_worker" in sys.argv:
        _worker_main()
        return

    parser = argparse.ArgumentParser()
    parser.add_argument("--data",        default="data/bv_benchmarks")
    parser.add_argument("--out",         default="models/gansat_bv.pt")
    parser.add_argument("--epochs",      type=int,   default=50)
    parser.add_argument("--batch",       type=int,   default=32)
    parser.add_argument("--lr",          type=float, default=1e-4)
    parser.add_argument("--d_steps",     type=int,   default=2)
    parser.add_argument("--device",      default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--checkpoint",  type=int,   default=10)
    parser.add_argument("--synthetic",   action="store_true")
    parser.add_argument("--n_synthetic", type=int,   default=2000)
    args = parser.parse_args()

    train_bv(
        data_dir=args.data, out_path=args.out,
        epochs=args.epochs, batch_size=args.batch, lr=args.lr,
        n_d_steps=args.d_steps, device_str=args.device,
        checkpoint_every=args.checkpoint,
        synthetic=args.synthetic, n_synthetic=args.n_synthetic,
    )


if __name__ == "__main__":
    main()
