FROM python:3.11-slim

LABEL maintainer="University of Manchester — GANSAT SMT-COMP '26"
LABEL description="GAN-guided SMT solver targeting QF_LIA"

WORKDIR /gansat

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY gansat/    ./gansat/
COPY main.py    .

# Copy pre-trained model if available
COPY models/    ./models/

# SMT-COMP entry point: receives benchmark path as $1
ENTRYPOINT ["python", "main.py"]
