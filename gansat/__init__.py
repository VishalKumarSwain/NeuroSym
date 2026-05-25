from .solver     import GANSATSolver
from .encoder    import encode, decode_assignment
from .bv_encoder import bv_encode, bv_decode_assignment, bv_feature_dim

# GAN modules require PyTorch — import only when available
try:
    from .gan    import IterativeGenerator, Discriminator, ViolationComputer, Generator
    from .bv_gan import BVIterativeGenerator, BVDiscriminator, BVViolationComputer
    _GAN_AVAILABLE = True
except ImportError:
    _GAN_AVAILABLE = False

__version__ = "1.1.0"
__all__ = [
    "GANSATSolver",
    "encode", "decode_assignment",
    "bv_encode", "bv_decode_assignment", "bv_feature_dim",
]
