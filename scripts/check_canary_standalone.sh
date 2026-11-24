#!/usr/bin/env bash
# Start canary/todo/compose.yaml and require a green verdict: the Postgres, Redis and S3
# checks pass with only the variables of that file. No platform value is present.
set -euo pipefail

compose=(docker compose -f canary/todo/compose.yaml)
body="$(mktemp)"
trap 'rm -f "$body"; "${compose[@]}" down --volumes >/dev/null 2>&1 || true' EXIT

"${compose[@]}" up --build -d

# At most 40 x (10 s + 3 s) = 520 s. A red verdict is HTTP 503 with the same document, so
# the body is kept to name the red check.
status=""
code="000"
for _ in $(seq 1 40); do
  # An attempt with no response leaves no body of an earlier attempt.
  : > "$body"
  code="$(curl -sS --max-time 10 -o "$body" -w '%{http_code}' \
    http://127.0.0.1:8000/api/canary/status 2>/dev/null || true)"
  if [[ "$code" == "200" ]]; then
    status="$(cat "$body")"
    break
  fi
  sleep 3
done

if [[ -z "$status" ]]; then
  "${compose[@]}" ps
  "${compose[@]}" logs --tail 40 todo s3-init
  echo "the Canary did not answer /api/canary/status with HTTP 200; last HTTP code: $code" >&2
  [[ -s "$body" ]] && { echo "last body:" >&2; cat "$body" >&2; echo >&2; }
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
enum_name = (report.get("capabilities") or {}).get("schema_enum")
if enum_name != "canary_todo_capability":
    sys.exit(f"standalone verdict missing expected schema_enum capability: {report.get('capabilities')!r}")
print(f"standalone Canary: ok, {checks}")
PY

worker_out="$("${compose[@]}" exec -T todo python /app/worker.py --once --health-url http://127.0.0.1:8000/api/health)"
python3 - "$worker_out" <<'PY'
import json
import sys

records = [json.loads(line) for line in sys.argv[1].splitlines() if line.strip()]
assert len(records) >= 2, records
result = records[-1]
assert (
    result.get("ok") is True
    and result.get("status_code") == 200
    and result.get("service") == "platform/todo"
), result
assert any(
    r.get("logger") == "platform-todo-worker" and r.get("service.name") == "platform-todo-worker"
    for r in records[:-1]
), records[:-1]
PY
echo "standalone Canary worker: verified"

schema_out="$("${compose[@]}" exec -T todo python -m infra2_sdk.runtime.schema_check --metadata canary_schema:METADATA --db-url postgresql://todo:local-test-postgres@postgres:5432/todo --label canary/todo)"
echo "$schema_out" | grep -q "ROLLBACK_CLASS: A"
echo "standalone Canary schema_check: verified"
