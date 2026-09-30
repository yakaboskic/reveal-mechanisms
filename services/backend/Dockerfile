FROM python:3.12.12-slim-bookworm AS runtime
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
CMD ["sh", "-c", "exec uvicorn reveal_backend.app:app --host 0.0.0.0 --port \"${PORT:-8000}\""]

FROM runtime AS deployment
COPY .deployment-assets/dapper /app/.runtime/dapper
COPY .deployment-assets/dismech /app/dismech
COPY .deployment-assets/rds-ca.pem /app/runtime-ca.pem
ENV GIT_OPTIONAL_LOCKS=0 TMPDIR=/tmp REVEAL_WORK_DIR=/work PYSTOW_HOME=/tmp/pystow XDG_CACHE_HOME=/tmp/cache
RUN python -c "from reveal_backend.dapper_release import verify_release; verify_release('/app/.runtime/dapper', '/app/services/backend/agent-runtime/dapper-release.json')"
RUN python -c "from pathlib import Path; import yaml; schema = yaml.safe_load(Path('/app/dismech/src/dismech/schema/dismech.yaml').read_bytes()); assert all(key in schema['prefixes'] for key in ('MONDO', 'CL', 'PMID', 'GO', 'ECTO'))"
