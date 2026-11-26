#!/usr/bin/env bash
# Start canary/todo/compose.yaml and verify live readiness, OTLP export, Todo CRUD,
# worker/schema checks, fault injection (Redis down -> 503 -> 200), and offline rules.
set -euo pipefail

if [[ -d .venv/bin ]]; then
  export PATH="$(pwd)/.venv/bin:${PATH}"
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  runtime=docker
  compose=(docker compose -f canary/todo/compose.yaml)
elif command -v podman >/dev/null 2>&1; then
  runtime=podman
  compose=(podman compose -f canary/todo/compose.yaml)
else
  echo "neither a running docker daemon nor podman is available" >&2
  exit 1
fi

body="$(mktemp)"
trap 'rm -f "$body"; "${compose[@]}" down --volumes >/dev/null 2>&1 || true' EXIT

if [[ "${COMPOSE_BUILD:-0}" != "1" ]] && "$runtime" image inspect infra2-canary-todo:local >/dev/null 2>&1; then
  "${compose[@]}" up --no-build -d
else
  "${compose[@]}" up --build -d
fi

status=""
code="000"
for _ in $(seq 1 40); do
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
  "${compose[@]}" logs --tail 40 todo s3-init otlp-stub
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

for path in /livez /readyz /api/health; do
  code="$(curl -sS --max-time 10 -o "$body" -w '%{http_code}' "http://127.0.0.1:8000${path}")"
  if [[ "$code" != "200" ]]; then
    echo "expected HTTP 200 on ${path}, got ${code}: $(cat "$body")" >&2
    exit 1
  fi
done
python3 - "$body" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
assert doc.get("ok") is True and doc.get("status") == "healthy", doc
assert doc.get("canary", {}).get("ok") is True, doc
PY
echo "standalone Canary /livez, /readyz, /api/health: verified"

crud_out="$(python3 - <<'PY'
import json, urllib.request

def call(method, path, payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:8000{path}", data=data, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())

st, created = call("POST", "/api/todos", {"title": "standalone-crud", "capability": "redis"})
assert st == 201 and created["capability"] == "redis", (st, created)
tid = created["id"]
st, listed = call("GET", "/api/todos")
assert st == 200 and any(row["id"] == tid for row in listed), (st, listed)
st, patched = call("PATCH", f"/api/todos/{tid}", {"completed": True})
assert st == 200 and patched["completed"] is True, (st, patched)
st, deleted = call("DELETE", f"/api/todos/{tid}")
assert st == 200 and deleted["deleted_id"] == tid, (st, deleted)
st, summary = call("POST", "/api/todos/summary", {"limit": 5})
assert st == 200 and summary["status"] == "unconfigured" and summary["decoding_version"] == "todo.summary.v1", (st, summary)
st, me = call("GET", "/api/auth/me", headers={"X-authentik-username": "canary", "X-authentik-groups": "ops"})
assert st == 200 and me["authenticated"] is True and me["username"] == "canary", (st, me)
print(f"CRUD & auth ok (deleted id={tid})")
PY
)"
echo "standalone Canary ${crud_out}"

worker_out="$("${compose[@]}" exec -T -e OTEL_SERVICE_NAME=platform-todo-worker todo python /app/worker.py --once --validate-env)"
python3 - "$worker_out" <<'PY'
import json
import sys

records = [json.loads(line) for line in sys.argv[1].splitlines() if line.strip()]
assert len(records) >= 2, records
result = records[-1]
assert (
    result.get("ok") is True
    and result.get("status_code") == 200
    and result.get("service") == "platform-todo"
    and result.get("health_url", "").endswith("/readyz")
), result
assert any(
    r.get("logger") == "platform-todo-worker" and r.get("service.name") == "platform-todo-worker"
    for r in records[:-1]
), records[:-1]
PY
echo "standalone Canary worker: verified"

schema_out="$("${compose[@]}" exec -T todo python -m infra2_sdk.storage check-schema --metadata canary_schema:METADATA --db-url postgresql://todo:local-test-postgres@postgres:5432/todo --label canary/todo)"
echo "$schema_out" | grep -q "ROLLBACK_CLASS: A"
echo "standalone Canary schema_check: verified"

otlp_ok="false"
for _ in $(seq 1 15); do
  if curl -fsS http://127.0.0.1:4318/stats | python3 -c '
import json, sys
s = json.load(sys.stdin)
sys.exit(0 if (s.get("traces", 0) > 0 and s.get("metrics", 0) > 0 and s.get("logs", 0) > 0) else 1)
'; then
    otlp_ok="true"
    break
  fi
  sleep 2
done
if [[ "$otlp_ok" != "true" ]]; then
  echo "OTLP stub did not receive all three signals: $(curl -sS http://127.0.0.1:4318/stats || true)" >&2
  exit 1
fi
echo "standalone Canary OTLP export: $(curl -sS http://127.0.0.1:4318/stats)"

# Fault injection: stop redis and wait past STATUS_CACHE_TTL_SECONDS (2s in compose.yaml) until fail-closed 503.
"${compose[@]}" stop redis >/dev/null
fault_code="000"
for _ in $(seq 1 15); do
  fault_code="$(curl -sS --max-time 10 -o "$body" -w '%{http_code}' \
    http://127.0.0.1:8000/api/canary/status 2>/dev/null || true)"
  if [[ "$fault_code" == "503" ]]; then
    break
  fi
  sleep 3
done
if [[ "$fault_code" != "503" ]]; then
  echo "expected 503 on /api/canary/status with redis stopped, got ${fault_code}" >&2
  exit 1
fi
ready_fault_code="$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/readyz || true)"
if [[ "$ready_fault_code" != "503" ]]; then
  echo "expected 503 on /readyz with redis stopped, got ${ready_fault_code}" >&2
  exit 1
fi
"${compose[@]}" start redis >/dev/null
recovered_code="000"
for _ in $(seq 1 15); do
  recovered_code="$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' \
    http://127.0.0.1:8000/readyz 2>/dev/null || true)"
  if [[ "$recovered_code" == "200" ]]; then
    break
  fi
  sleep 3
done
if [[ "$recovered_code" != "200" ]]; then
  echo "expected /readyz recovery to 200 after redis restart, got ${recovered_code}" >&2
  exit 1
fi
echo "standalone Canary fault injection (503 -> 200): verified"

python3 -m infra2_sdk.delivery check-hooks canary/todo/runtime-hooks.manifest.json
APP_URL=http://127.0.0.1:8000 python3 -m infra2_sdk.delivery run-cell canary/todo/runtime-hooks.manifest.json --cell staging.operation
echo "standalone Canary offline hooks & staging.operation check: verified"
