FROM python:3.11-slim

# Build dependencies for libgourou only — no OCR toolchain (spec decision #3).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake make g++ pkg-config \
    libpugixml-dev libzip-dev libssl-dev libcurl4-openssl-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Step 1: Build and install updfparser ────────────────────────────────────
# libgourou depends on updfparser. It must be built and installed system-wide
# first so CMake's find_package(updfparser) call succeeds when building libgourou.
RUN git clone https://github.com/SamuelMarks/updfparser.git /tmp/updfparser \
    && cd /tmp/updfparser \
    && mkdir build && cd build \
    && cmake .. -DBUILD_SHARED_LIBS=OFF \
    && make -j"$(nproc)" \
    && make install \
    && ldconfig \
    && rm -rf /tmp/updfparser

# ── Step 2: Build libgourou (now that updfparser is on the system) ───────────
# Pinned to commit 254e56e (latest on 'cmake' branch as of 2022-12-05).
RUN git clone https://github.com/SamuelMarks/libgourou.git -b cmake /app/libgourou \
    && cd /app/libgourou \
    && git checkout 254e56ecc57b8871134eb2f3461506109e9cb231 \
    && sed -i 's|find_package(CURL CONFIG REQUIRED)|find_package(CURL REQUIRED)|' utils/CMakeLists.txt \
    && mkdir build && cd build \
    && cmake .. -DBUILD_UTILS=ON -DBUILD_SHARED_LIBS=OFF \
    && make -j"$(nproc)" \
    && mkdir -p /app/libgourou/utils \
    && cp -f /app/libgourou/build/utils/acsmdownloader \
             /app/libgourou/build/utils/adept_activate \
             /app/libgourou/build/utils/adept_remove \
             /app/libgourou/utils/ \
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
