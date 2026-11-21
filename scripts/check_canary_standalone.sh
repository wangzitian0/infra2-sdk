#!/usr/bin/env bash
# Start canary/todo/compose.yaml and require a green verdict: the Postgres, Redis and S3
# checks pass with only the variables of that file. No platform value is present.
set -euo pipefail

compose=(docker compose -f canary/todo/compose.yaml)
trap '"${compose[@]}" down --volumes >/dev/null 2>&1 || true' EXIT

"${compose[@]}" up --build -d

status=""
for _ in $(seq 1 40); do
  if status="$(curl -fsS --max-time 20 http://127.0.0.1:8000/api/canary/status 2>/dev/null)"; then
    break
  fi
  sleep 3
done

if [[ -z "$status" ]]; then
  "${compose[@]}" ps
  "${compose[@]}" logs --tail 40 todo s3-init
  echo "the Canary did not answer /api/canary/status with HTTP 200" >&2
  exit 1
fi

python3 - "$status" <<'PY'
import json
import sys

report = json.loads(sys.argv[1])
checks = {name: check["status"] for name, check in report["checks"].items()}
expected = {
    "postgres": "pass",
    "redis": "pass",
    "s3": "pass",
    "signoz": "unconfigured",
    "openpanel": "unconfigured",
    "authentik": "unconfigured",
}
if report.get("ok") is not True or checks != expected:
    sys.exit(f"standalone verdict is not green: ok={report.get('ok')} checks={checks}")
print(f"standalone Canary: ok, {checks}")
PY
