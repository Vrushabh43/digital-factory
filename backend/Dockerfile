# DMF backend (FastAPI + read-only OPC UA client + tracker).
# Build context is the REPOSITORY ROOT (needs config/ and database/):
#   docker build -f backend/Dockerfile -t dmf-backend .
# The image mirrors the repo layout under /app so config and migrations are found at the
# same relative paths as in development (/app/config, /app/database, /app/backend).

FROM python:3.12-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY backend/pyproject.toml backend/constraints.txt ./
COPY backend/dmf ./dmf
COPY backend/dmf_mock ./dmf_mock
# constraints.txt pins every transitive dependency to the tested versions
RUN python -m venv /opt/venv && /opt/venv/bin/pip install -c constraints.txt .

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH \
    PLANT_CONFIG_PATH=/app/config/plant.yaml \
    NODES_CONFIG_PATH=/app/config/opcua_nodes.yaml \
    ALEMBIC_INI_PATH=/app/database/alembic.ini
RUN groupadd --system --gid 10001 dmf && useradd --system --uid 10001 --gid dmf --no-create-home dmf
COPY --from=build /opt/venv /opt/venv
WORKDIR /app/backend
COPY --chown=root:root database /app/database
COPY --chown=root:root config /app/config
USER dmf
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"
# ONE worker: the tracker keeps the live state in-process.
CMD ["uvicorn", "dmf.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header", "--timeout-graceful-shutdown", "20"]
