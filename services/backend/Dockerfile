FROM python:3.12.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git curl gcc libc6-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/backend/pyproject.toml /app/services/backend/pyproject.toml
COPY services/backend/agent-runtime/box-requirements.txt /app/services/backend/agent-runtime/box-requirements.txt
RUN python - <<'PY'
import pathlib, subprocess, sys, tomllib
project = tomllib.loads(pathlib.Path('/app/services/backend/pyproject.toml').read_text())['project']
subprocess.run([sys.executable, '-m', 'pip', 'install', *project['dependencies'],
    *project['optional-dependencies']['eaggl'], '-r', '/app/services/backend/agent-runtime/box-requirements.txt'], check=True)
PY
COPY services/backend/src /app/services/backend/src
RUN pip install --no-deps /app/services/backend
RUN git config --system --add safe.directory /app/.runtime/dapper
COPY api /app/api
COPY schema /app/schema
COPY docs /app/docs
COPY scripts /app/scripts
COPY services/backend/agent-runtime /app/services/backend/agent-runtime
COPY services/backend/agent-skills /app/services/backend/agent-skills
COPY services/backend/csl /app/services/backend/csl
COPY data/dapper/2026-09-24-v8 /app/data/dapper/2026-09-24-v8
COPY data/dismech-gaps/2026-09-24 /app/data/dismech-gaps/2026-09-24
COPY data/cfde-genesets/2026-09-24/manifest.json data/cfde-genesets/2026-09-24/activity.json /app/data/cfde-genesets/2026-09-24/
ENV PYTHONPATH=/app/services/backend/src REVEAL_DAPPER_ROOT=/app/.runtime/dapper REVEAL_DISMECH_SOURCE=/app/dismech REVEAL_ARTIFACTS_DIR=/app/.runtime/artifacts
EXPOSE 8000
CMD ["uvicorn", "reveal_backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
