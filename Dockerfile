FROM node:22-bookworm AS frontend-build

WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
COPY .agents/skills/pulseguard-operations/ /build/.agents/skills/pulseguard-operations/
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.9.30 AS uv-bin

FROM mcr.microsoft.com/playwright/python:v1.49.1-noble AS runtime-base
ARG PULSEGUARD_VERSION=0.1.0
ARG PULSEGUARD_BUILD_SHA=unknown

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/backend
ENV PATH=/app/.venv/bin:$PATH
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV PULSEGUARD_HOST=0.0.0.0
ENV PULSEGUARD_PORT=8787
ENV PULSEGUARD_ALERT_DETAIL_BASE_URL=http://localhost:8787
ENV PULSEGUARD_DATA_DIR=/app/data
ENV PULSEGUARD_REPORTS_DIR=/app/reports
ENV PULSEGUARD_STATIC_DIR=/app/frontend/dist
ENV PULSEGUARD_VERSION=${PULSEGUARD_VERSION}
ENV PULSEGUARD_BUILD_SHA=${PULSEGUARD_BUILD_SHA}

COPY --from=uv-bin /uv /uvx /bin/
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project

COPY backend /app/backend

RUN mkdir -p /app/data /app/reports/screenshots /app/reports/traces /app/reports/responses

FROM runtime-base AS app

COPY --from=frontend-build /build/frontend/dist /app/frontend/dist

EXPOSE 8787
CMD ["sh", "-c", "uvicorn app.main:app --host ${PULSEGUARD_HOST:-0.0.0.0} --port ${PULSEGUARD_PORT:-8787}"]
