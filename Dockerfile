FROM python:3.11-slim

LABEL maintainer="NIT Warangal — NeuroSym SMT-COMP '26"
LABEL description="Neural-Symbolic SMT Solver targeting QF_LIA and QF_BV"

WORKDIR /neurosym

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip first to avoid hash/resolver issues
RUN pip install --upgrade pip

# Install heavy deps individually to isolate failures and allow retries
RUN pip install --no-cache-dir --retries 5 --timeout 120 \
    "z3-solver>=4.12.0" \
    "numpy>=1.24.0" \
    "tqdm>=4.65.0" \
    "networkx>=3.1" \
    "pysmt>=0.9.5"

RUN pip install --no-cache-dir --retries 5 --timeout 300 \
    "torch>=2.1.0" --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir --retries 5 --timeout 120 \
    "scikit-learn>=1.3.0" \
    "matplotlib>=3.7.0"

# Copy source
COPY gansat/    ./gansat/
COPY main.py    .

# Copy pre-trained model if available (optional — solver falls back to Z3)
COPY models/    ./models/

# SMT-COMP entry point: receives benchmark path as $1
ENTRYPOINT ["python", "main.py"]
