# =============================================================================
# Stage 1: build shellpower-cli
# =============================================================================
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y \
    cmake \
    make \
    git \
    gcc \
    libx11-dev \
    libxrandr-dev \
    libxinerama-dev \
    libxcursor-dev \
    libxi-dev \
    libgl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only the C source tree needed for the CLI
COPY CMakeLists.txt .
COPY src/ ./src/

# Configure and build just the CLI target (FetchContent downloads raylib for headers)
RUN cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_EXPORT_COMPILE_COMMANDS=OFF \
    && cmake --build build --target shellpower-cli -j$(nproc)

# =============================================================================
# Stage 2: runtime
# =============================================================================
FROM python:3.11-slim

WORKDIR /app

# Pull in the compiled CLI binary
COPY --from=builder /build/build/shellpower-cli /app/shellpower-cli

# Install Python dependencies
RUN apt-get update && apt-get install -y gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY data/ ./data/

RUN mkdir -p uploads

EXPOSE 8000

CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
