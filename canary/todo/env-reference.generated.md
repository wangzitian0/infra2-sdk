# Environment reference

Source: `canary/todo`

## Application

| Variable | Required | Sensitive | Source | Description |
|---|---|---|---|---|
| `PORT` | no | no | code | HTTP port of the service. |

## postgres

| Variable | Required | Sensitive | Source | Description |
|---|---|---|---|---|
| `DATABASE_URL` | yes | yes | runtime | PostgreSQL URL with host, user and password. |

## redis

| Variable | Required | Sensitive | Source | Description |
|---|---|---|---|---|
| `REDIS_URL` | no | yes | runtime | Redis connection URL with credentials. The Redis check is unconfigured without it. |

## s3

| Variable | Required | Sensitive | Source | Description |
|---|---|---|---|---|
| `S3_ENDPOINT` | no | no | code | S3 endpoint URL of the S3 check. |
| `S3_BUCKET` | no | no | code | Bucket of the S3 check. |
| `S3_ACCESS_KEY` | no | yes | human | S3 access key. |
| `S3_SECRET_KEY` | no | yes | human | S3 secret key. |

## liveness

| Variable | Required | Sensitive | Source | Description |
|---|---|---|---|---|
| `SIGNOZ_OTEL_COLLECTOR` | no | no | code | Health URL of the SigNoz collector. |
| `OPENPANEL_API_URL` | no | no | code | Health URL of OpenPanel. |
| `OPENPANEL_HEALTH_URL` | no | no | code | Internal OpenPanel health URL for liveness checks. |
| `AUTHENTIK_ENDPOINT` | no | no | code | Liveness URL of Authentik. |

## frontend_telemetry

| Variable | Required | Sensitive | Source | Description |
|---|---|---|---|---|
| `OPENPANEL_CLIENT_ID` | no | no | code | OpenPanel client ID from x-capabilities openpanel. |
| `OPENPANEL_ENVIRONMENT` | no | no | code | OpenPanel environment from x-capabilities openpanel. |
| `OPENPANEL_SCRIPT_URL` | no | no | code | OpenPanel script URL from x-capabilities openpanel. |
| `NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT` | no | no | code | Browser OTLP endpoint from x-capabilities browser_otel. |
| `NEXT_PUBLIC_DEPLOYMENT_ENVIRONMENT` | no | no | code | Browser deployment environment from x-capabilities browser_otel. |
| `NEXT_PUBLIC_GIT_SHA` | no | no | code | Browser Git SHA from x-capabilities browser_otel. |

## sdk

| Variable | Required | Sensitive | Source | Description |
|---|---|---|---|---|
| `STATUS_CACHE_TTL_SECONDS` | no | no | code | Canary status probe cache TTL in seconds. |
| `ENVIRONMENT` | no | no | code | Environment tier. The SDK reads it. |
| `SERVICE_VERSION` | no | no | code | Deployed service version. The SDK reads it. |
| `IMAGE_DIGEST` | no | no | code | Deployed immutable image digest. The SDK reads it. |
| `RELEASE_ID` | no | no | code | Release identifier. The SDK reads it. |
| `CONFIGURATION_SHA256` | no | no | code | Configuration hash of the deployment. The SDK reads it. |
| `GIT_COMMIT_SHA` | no | no | code | Commit of the deployment. The SDK reads it. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | no | no | code | OTLP endpoint. Telemetry export is off without it. |
| `OTEL_SERVICE_NAME` | no | no | code | Telemetry service name. The SDK reads it. |
| `OTEL_RESOURCE_ATTRIBUTES` | no | no | code | Telemetry resource attributes. The SDK reads them. |
| `LLM_PROVIDER` | no | no | code | LLM provider. The SDK reads it. |
| `LLM_BASE_URL` | no | no | code | LLM base URL. The SDK reads it. |
| `LLM_SMALL_MODEL` | no | no | code | LLM model of the todo summary. The SDK reads it. |
| `LLM_API_KEY` | no | yes | human | LLM API key. The todo summary is unconfigured without it. |
