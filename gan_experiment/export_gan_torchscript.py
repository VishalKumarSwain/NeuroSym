import sys, os
sys.path.insert(0, "/tmp/neurosym_gan_check")
import torch
from gansat.bv_gan import BVIterativeGenerator, BV_FORMULA_DIM, BV_NOISE_DIM

model = BVIterativeGenerator()
state = torch.load("/tmp/neurosym_gan_check/models/gansat_bv.pt", map_location="cpu")
model.load_state_dict(state)
model.eval()

# Trace the forward(f, z) path with fixed-shape dummy inputs (batch=1).
f = torch.randn(1, BV_FORMULA_DIM)
z = torch.randn(1, BV_NOISE_DIM)

traced = torch.jit.trace(model, (f, z))
out_path = "/tmp/gansat_bv_traced.pt"
traced.save(out_path)
print("saved:", out_path)

# Sanity check: traced output matches eager output
with torch.no_grad():
    eager_out = model(f, z)
    traced_out = traced(f, z)
    diff = (eager_out - traced_out).abs().max().item()
    print("max diff eager vs traced:", diff)
