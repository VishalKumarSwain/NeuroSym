"""
train.py — GAN training script for QF_LIA (linear integer arithmetic).

Usage:
    python -u scripts/train.py --data data/smtcomp2025/extracted/single_query/QF_LIA \
                                --out  models/gansat_lia.pt \
                                --epochs 100

Each file is processed in an isolated subprocess so a Z3 crash or hang on one
formula never kills the whole training run. 8 parallel workers keep CPU saturated.
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
from gansat.parser  import parse_file
from gansat.encoder import encode, MAX_VARS
from gansat.gan     import IterativeGenerator, Discriminator, assignment_to_tensor, NOISE_DIM

_LIA_LOGICS = {"QF_LIA", "QF_NIA", "QF_LRA", "LIA", "UNKNOWN"}


# ── Subprocess worker ─────────────────────────────────────────────────────────
# Called as: python train.py --_worker <path> <max_ms>
# Prints JSON {"fe": [...], "ae": [...]} on success, exits 1 on failure.

def _worker_main():
    path   = sys.argv[sys.argv.index("--_worker") + 1]
    max_ms = int(sys.argv[sys.argv.index("--_worker") + 2])
    try:
        formula = parse_file(path)
        if not formula.variables:
            sys.exit(1)
        if formula.logic.upper() not in _LIA_LOGICS:
            sys.exit(1)

        solver = z3.Solver()
        solver.set("timeout", max_ms)
        for a in formula.assertions:
            solver.add(a)
        if solver.check() != z3.sat:
            sys.exit(1)

        model = solver.model()
        assignment = {
            str(d): model[d].as_long()
            for d in model.decls()
            if model[d] is not None and z3.is_int_value(model[d])
        }
        if not assignment:
            sys.exit(1)

        formula_enc = encode(formula).tolist()
        assign_enc  = assignment_to_tensor(assignment, formula.var_names).numpy().tolist()
        print(json.dumps({"fe": formula_enc, "ae": assign_enc}))
        sys.exit(0)
    except Exception:
        sys.exit(1)


def _process_file(path, max_solve_ms, timeout_sec=15):
    """Run one file in an isolated subprocess — crash-safe."""
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

class LIASMTDataset(Dataset):
    def __init__(self, smt2_files: list, max_solve_ms: int = 5000,
                 file_timeout_sec: int = 15, num_workers: int = 8):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        self.samples = []
        print(f"[lia_dataset] Processing {len(smt2_files)} QF_LIA benchmarks "
              f"(timeout={file_timeout_sec}s/file, {num_workers} workers)...")

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(_process_file, f, max_solve_ms, file_timeout_sec): f
                for f in smt2_files
            }
            for future in tqdm(as_completed(futures), total=len(smt2_files)):
                try:
                    sample = future.result()
                    if sample is not None:
                        self.samples.append(sample)
                except Exception:
                    pass

        print(f"[lia_dataset] Collected {len(self.samples)} LIA SAT training pairs.")

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        fe, ae = self.samples[idx]
        return (
            torch.tensor(fe, dtype=torch.float32),
            torch.tensor(ae, dtype=torch.float32),
        )


# ── Training loop ─────────────────────────────────────────────────────────────

def train(
    data_dir:         str,
    out_path:         str,
    epochs:           int   = 100,
    batch_size:       int   = 16,
    lr:               float = 1e-4,
    n_d_steps:        int   = 2,
    device_str:       str   = "cpu",
    checkpoint_every: int   = 10,
):
    device = torch.device(device_str)

    files = list(Path(data_dir).rglob("*.smt2"))
    random.shuffle(files)
    print(f"[train] Found {len(files)} .smt2 files in {data_dir}")

    dataset = LIASMTDataset(files)
    if len(dataset) == 0:
        print("[error] No LIA SAT training pairs found.")
        sys.exit(1)

    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=0)

    G    = IterativeGenerator().to(device)
    D    = Discriminator().to(device)
    optG = optim.Adam(G.parameters(), lr=lr, betas=(0.5, 0.999))
    optD = optim.Adam(D.parameters(), lr=lr, betas=(0.5, 0.999))
    crit = nn.BCEWithLogitsLoss()
    lam  = 0.5

    best_g  = float("inf")
    history = {"loss_d": [], "loss_g": []}

    print(f"[train] Epochs={epochs}  Batch={batch_size}  Device={device}")
    print(f"[train] Pairs={len(dataset)}  Batches/epoch={len(loader)}")

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
            fake   = G(f_enc)
            loss_g = crit(D(f_enc, fake), torch.ones(B, device=device))
            loss_v = G.violation_score(f_enc, fake).mean()
            loss_g = loss_g + lam * loss_v
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
    parser.add_argument("--data",       default="data/benchmarks")
    parser.add_argument("--out",        default="models/gansat_lia.pt")
    parser.add_argument("--epochs",     type=int,   default=100)
    parser.add_argument("--batch",      type=int,   default=16)
    parser.add_argument("--lr",         type=float, default=1e-4)
    parser.add_argument("--d_steps",    type=int,   default=2)
    parser.add_argument("--device",     default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--checkpoint", type=int,   default=10)
    args = parser.parse_args()

    train(
        data_dir=args.data,
        out_path=args.out,
        epochs=args.epochs,
        batch_size=args.batch,
        lr=args.lr,
        n_d_steps=args.d_steps,
        device_str=args.device,
        checkpoint_every=args.checkpoint,
    )


if __name__ == "__main__":
    main()
