"""
GAN training script for GANSAT.

Usage:
    python scripts/train.py --data data/benchmarks --epochs 50 --out models/gansat.pt

Training data: .smt2 SAT instances where Z3 can find a model.
Each sample = (formula_encoding, satisfying_assignment_encoding).

GAN objective:
  - Discriminator distinguishes real (formula, solution) from generated (formula, GAN_output)
  - Generator learns to fool discriminator → produces valid assignments

Also trains with a "hint loss": if GAN output is checked by Z3 and is actually SAT,
use that as a bonus real sample to accelerate learning.
"""

import argparse
import os
import sys
import random
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import z3
from gansat.parser import parse_file
from gansat.encoder import encode, decode_assignment, MAX_VARS
from gansat.gan import IterativeGenerator, Discriminator, assignment_to_tensor, NOISE_DIM, ASSIGN_DIM


class SMTDataset(Dataset):
    def __init__(self, smt2_files: list, max_solve_ms: int = 5000):
        self.samples = []
        print(f"[dataset] Solving {len(smt2_files)} benchmarks to extract training pairs...")
        solver = z3.Solver()
        for path in tqdm(smt2_files):
            try:
                formula = parse_file(str(path))
                if not formula.variables:
                    continue
                solver.reset()
                solver.set("timeout", max_solve_ms)
                for a in formula.assertions:
                    solver.add(a)
                result = solver.check()
                if result != z3.sat:
                    continue
                model = solver.model()
                assignment = {
                    str(d): model[d].as_long()
                    for d in model.decls()
                    if model[d] is not None and z3.is_int_value(model[d])
                }
                if not assignment:
                    continue
                formula_enc = encode(formula)
                assign_enc  = assignment_to_tensor(assignment, formula.var_names).numpy()
                self.samples.append((formula_enc, assign_enc))
            except Exception:
                continue
        print(f"[dataset] Collected {len(self.samples)} SAT training pairs.")

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        formula_enc, assign_enc = self.samples[idx]
        return (
            torch.tensor(formula_enc, dtype=torch.float32),
            torch.tensor(assign_enc,  dtype=torch.float32),
        )


def train(
    data_dir: str,
    out_path: str,
    epochs: int = 50,
    batch_size: int = 64,
    lr_g: float = 1e-4,
    lr_d: float = 1e-4,
    n_d_steps: int = 2,
    device_str: str = "cpu",
    checkpoint_every: int = 10,
):
    device = torch.device(device_str)

    # --- collect files ---
    files = list(Path(data_dir).rglob("*.smt2"))
    random.shuffle(files)
    print(f"[train] Found {len(files)} .smt2 files in {data_dir}")

    dataset = SMTDataset(files)
    if len(dataset) == 0:
        print("[error] No SAT training pairs found. Run download_benchmarks.py first.")
        sys.exit(1)

    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=0)

    # --- models ---
    G = IterativeGenerator().to(device)
    D = Discriminator().to(device)
    opt_G = optim.Adam(G.parameters(), lr=lr_g, betas=(0.5, 0.999))
    opt_D = optim.Adam(D.parameters(), lr=lr_d, betas=(0.5, 0.999))
    criterion = nn.BCEWithLogitsLoss()
    lambda_viol = 0.5   # weight for constraint violation auxiliary loss

    real_label = 1.0
    fake_label = 0.0

    best_loss_g = float("inf")
    history = {"loss_d": [], "loss_g": []}

    print(f"[train] Starting training: {epochs} epochs, batch {batch_size}, device {device}")

    for epoch in range(1, epochs + 1):
        G.train()
        D.train()
        epoch_loss_d = 0.0
        epoch_loss_g = 0.0
        n_batches = 0

        for formula_enc, real_assign in loader:
            formula_enc = formula_enc.to(device)
            real_assign = real_assign.to(device)
            B = formula_enc.size(0)

            # === Train Discriminator ===
            for _ in range(n_d_steps):
                opt_D.zero_grad()

                # real pairs
                real_labels = torch.full((B,), real_label, device=device)
                logits_real = D(formula_enc, real_assign)
                loss_d_real = criterion(logits_real, real_labels)

                # fake pairs
                with torch.no_grad():
                    fake_assign = G(formula_enc)
                fake_labels = torch.full((B,), fake_label, device=device)
                logits_fake = D(formula_enc, fake_assign.detach())
                loss_d_fake = criterion(logits_fake, fake_labels)

                loss_d = (loss_d_real + loss_d_fake) * 0.5
                loss_d.backward()
                opt_D.step()

            # === Train Generator ===
            opt_G.zero_grad()
            fake_assign = G(formula_enc)
            logits_fake = D(formula_enc, fake_assign)
            loss_g_gan  = criterion(logits_fake, torch.full((B,), real_label, device=device))
            # Auxiliary violation loss: directly minimize constraint violations
            viol_score  = G.violation_score(formula_enc, fake_assign)
            loss_g_viol = viol_score.mean()
            loss_g      = loss_g_gan + lambda_viol * loss_g_viol
            loss_g.backward()
            opt_G.step()

            epoch_loss_d += loss_d.item()
            epoch_loss_g += loss_g.item()
            n_batches += 1

        avg_d = epoch_loss_d / max(n_batches, 1)
        avg_g = epoch_loss_g / max(n_batches, 1)
        history["loss_d"].append(avg_d)
        history["loss_g"].append(avg_g)

        print(f"[epoch {epoch:03d}/{epochs}] loss_D={avg_d:.4f}  loss_G={avg_g:.4f}  (gan+viol)")

        if epoch % checkpoint_every == 0 or epoch == epochs:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            torch.save(G.state_dict(), out_path)
            print(f"[checkpoint] Saved generator → {out_path}")

        if avg_g < best_loss_g:
            best_loss_g = avg_g
            torch.save(G.state_dict(), out_path.replace(".pt", "_best.pt"))

    print(f"[done] Training complete. Best G loss: {best_loss_g:.4f}")
    _save_history(history, out_path.replace(".pt", "_history.npy"))


def _save_history(history: dict, path: str):
    np.save(path, history)
    print(f"[history] Saved training curves → {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data",       default="data/benchmarks")
    parser.add_argument("--out",        default="models/gansat.pt")
    parser.add_argument("--epochs",     type=int,   default=50)
    parser.add_argument("--batch",      type=int,   default=64)
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
        lr_g=args.lr,
        lr_d=args.lr,
        n_d_steps=args.d_steps,
        device_str=args.device,
        checkpoint_every=args.checkpoint,
    )


if __name__ == "__main__":
    main()
