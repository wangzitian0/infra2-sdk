# Canary Todo image (wangzitian0/infra2-sdk#247).
# Build context: the infra2-sdk repository root. The image installs the SDK from the same
# source, so image tag vX.Y.Z always runs SDK X.Y.Z.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md LICENSE /tmp/sdk/
RUN uv sync --locked --no-dev \
      --extra fastapi --extra postgres --extra s3 --extra llm \
      --no-install-project --project /tmp/sdk

COPY src /tmp/sdk/src
RUN uv pip install --no-cache --python /app/.venv/bin/python --no-deps /tmp/sdk \
    && rm -rf /tmp/sdk

COPY canary/todo/app.py canary/todo/ui.py canary/todo/canary_settings.py \
     canary/todo/canary_schema.py canary/todo/worker.py \
     canary/todo/runtime-hooks.manifest.json /app/

ENV PYTHONPATH=/app
EXPOSE 8000

CMD ["python", "app.py"]
