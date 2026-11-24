# Canary Todo image (wangzitian0/infra2-sdk#247).
# Build context: the infra2-sdk repository root. The image installs the SDK from the same
# source, so image tag vX.Y.Z always runs SDK X.Y.Z.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

WORKDIR /app

COPY pyproject.toml README.md LICENSE /tmp/sdk/
COPY src /tmp/sdk/src
RUN pip install --no-cache-dir \
       "uvicorn[standard]>=0.28,<1" \
       "/tmp/sdk[fastapi,postgres,s3,http,pydantic-ai]" \
    && rm -rf /tmp/sdk

COPY canary/todo/app.py canary/todo/ui.py canary/todo/canary_settings.py \
     canary/todo/canary_schema.py canary/todo/worker.py \
     canary/todo/env.manifest.json canary/todo/env-reference.generated.md \
     canary/todo/runtime-hooks.manifest.json /app/

ENV PYTHONPATH=/app
EXPOSE 8000

CMD ["python", "app.py"]
