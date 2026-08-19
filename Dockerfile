# syntax=docker/dockerfile:1

FROM node:22-bookworm-slim AS web-build

WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci

COPY index.html tsconfig.json vite.config.ts ./
COPY app ./app
RUN npm run build


FROM python:3.13-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    VERIFICATION_DATA_DIR=/data \
    VERIFICATION_WEB_DIST=/app/dist

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 treasury

WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN pip install --no-cache-dir uv==0.7.20 \
    && uv sync --frozen --no-dev --no-install-project \
    && pip uninstall --yes uv

COPY backend/app ./app
COPY --from=web-build /build/dist ./dist

RUN mkdir --parents /data \
    && chown --recursive treasury:treasury /app /data

USER treasury
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
