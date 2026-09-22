from .ns_solver   import NeuroSymSolver, format_output

# GAN modules (gan.py / bv_gan.py) and the numpy-based encoders (ns_encoder /
# ns_bv_encoder) are intentionally NOT imported here at package-init time.
# torch alone costs ~1s to import and numpy ~60-90ms; forcing every caller
# of `import gansat` (or `from gansat.ns_solver import ...`, which runs this
# __init__ first) to pay that cost when no trained model is ever loaded was
# the case on every run we tested with no models/ directory present.
# ns_solver._import_torch() imports them lazily, on first actual use, once a
# model_path is supplied. Nothing in this codebase imports encode/
# decode_assignment/bv_encode/bv_decode_assignment/bv_feature_dim from the
# top-level `gansat` package (tests and scripts/train*.py import them
# directly from gansat.encoder / gansat.bv_encoder instead), so dropping the
# re-export here is safe.

__version__ = "1.1.0"
__all__ = ["NeuroSymSolver", "format_output"]
