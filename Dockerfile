FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Postgres (Debian default) + build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    postgresql postgresql-client postgresql-contrib \
    curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml /app/pyproject.toml
# Upgrade pip toolchain to patched versions before installing deps
RUN pip install --upgrade pip "setuptools>=78.1.1" "wheel>=0.46.2" && \
    pip install .
COPY . /app

# Your entrypoint
COPY docker/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r//' /entrypoint.sh && chmod +x /entrypoint.sh

EXPOSE 8000 5432
CMD ["/entrypoint.sh"]
