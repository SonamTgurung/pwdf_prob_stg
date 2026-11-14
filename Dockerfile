FROM python:3.11-slim

# NUMBA_CACHE_DIR: numba (via pysheds) caches compiled code next to
# site-packages, which a non-root user (Hugging Face Spaces runs as uid 1000)
# can't write to.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NUMBA_CACHE_DIR=/tmp/numba_cache

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gdal-bin \
        libgdal-dev \
        libspatialindex-dev \
    && rm -rf /var/lib/apt/lists/*

COPY assets/backend/requirements.txt /app/assets/backend/requirements.txt
COPY assets/wildcat /app/assets/wildcat
# Hugging Face Spaces runs the container as uid 1000, not root; Wildcat's
# default data dir (used when FIRE_DATA_DIR is unset) must be writable there.
RUN chmod -R a+rwX /app/assets/wildcat

WORKDIR /app/assets/backend
RUN pip install -r requirements.txt

WORKDIR /app
COPY assets/backend /app/assets/backend
COPY assets/asbpa_data /app/assets/asbpa_data

WORKDIR /app/assets/backend
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
