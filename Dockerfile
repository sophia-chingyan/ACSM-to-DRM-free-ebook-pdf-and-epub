FROM python:3.11-slim

# Build dependencies for libgourou only — no OCR toolchain (spec decision #3).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake make g++ \
    libpugixml-dev libzip-dev libssl-dev libcurl4-openssl-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone and build libgourou (acsmdownloader, adept_activate, adept_remove).
RUN git clone --recurse-submodules https://forge.soutade.fr/soutade/libgourou.git /app/libgourou \
    && cd /app/libgourou \
    && make BUILD_UTILS=1 BUILD_STATIC=1 BUILD_SHARED=0 \
    && ls -la /app/libgourou/utils/acsmdownloader

# Canonical ADEPT credential path (spec decision #8). Mount this exact path as a
# persistent volume so the Adobe device registers once and survives restarts.
ENV ADEPT_DIR=/app/.adept

# Python dependencies.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY app.py converter.py ./
COPY templates/ templates/

# Runtime data directories (output and covers are mounted volumes in Zeabur).
RUN mkdir -p uploads output covers .adept

EXPOSE 8080

# Health check hits the public login page.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-8080}/login" || exit 1

# Single worker (required by the in-memory job dict) + threads for concurrent
# polling. Shell form so ${PORT} from Zeabur is expanded; 8080 fallback locally.
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4 --timeout 300 --graceful-timeout 30"]
