FROM python:3.12-slim

LABEL maintainer="NIT Warangal / University of Manchester — NeuroSym SMT-COMP '26"
LABEL description="NeuroSym: GAN + Z3 + Bitwuzla portfolio solver for QF_LIA, QF_BV, QF_ABV"
LABEL version="1.0"

WORKDIR /neurosym

# System deps — bitwuzla wheel on PyPI is manylinux so no build tools needed;
# only libgomp is required for torch CPU kernels
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip

# Core solver dependencies
RUN pip install --no-cache-dir --retries 5 --timeout 120 \
    "z3-solver>=4.12.0" \
    "numpy>=1.24.0" \
    "tqdm>=4.65.0" \
    "networkx>=3.1" \
    "pysmt>=0.9.5"

# Bitwuzla — BV-specialist solver for portfolio race
RUN pip install --no-cache-dir --retries 5 --timeout 120 \
    "bitwuzla>=0.9.0"

# PyTorch CPU-only (keeps image ~1.5GB instead of ~5GB for CUDA)
RUN pip install --no-cache-dir --retries 5 --timeout 300 \
    "torch>=2.1.0" --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir --retries 5 --timeout 120 \
    "scikit-learn>=1.3.0" \
    "matplotlib>=3.7.0"

# Copy source
COPY gansat/  ./gansat/
COPY main.py  .

# Copy pre-trained models (optional — solver falls back to Z3+Bitwuzla if absent)
COPY models/  ./models/

# SMT-COMP interface: python main.py <benchmark.smt2>
ENTRYPOINT ["python", "main.py"]
