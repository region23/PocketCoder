#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export POCKETCODER_CODEX_BIN="${POCKETCODER_CODEX_BIN:-python3}"
export POCKETCODER_CODEX_SAFE_ARGS="${POCKETCODER_CODEX_SAFE_ARGS:--c \"import sys; print(\\\"SAFE:\\\" + sys.argv[-1])\"}"
export POCKETCODER_CODEX_YOLO_ARGS="${POCKETCODER_CODEX_YOLO_ARGS:--c \"import sys; print(\\\"YOLO:\\\" + sys.argv[-1])\"}"

cleanup() {
  docker compose down --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[e2e] Building and starting daemon..."
docker compose up -d --build daemon

echo "[e2e] Waiting for daemon readiness..."
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:8080/ready" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl -fsS "http://127.0.0.1:8080/ready" >/dev/null 2>&1; then
  echo "[e2e] Daemon is not ready"
  exit 1
fi

echo "[e2e] Creating SAFE job..."
create_response="$(curl -fsS -X POST "http://127.0.0.1:8080/jobs" \
  -H "Content-Type: application/json" \
  -d '{"engine":"codex","repo":"e2e-repo","mode":"SAFE","prompt":"hello-from-compose"}')"
job_id="$(printf '%s' "$create_response" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')"

echo "[e2e] Waiting for job #${job_id} completion..."
status=""
for _ in $(seq 1 60); do
  detail="$(curl -fsS "http://127.0.0.1:8080/jobs/${job_id}")"
  status="$(printf '%s' "$detail" | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')"
  case "$status" in
    COMPLETED|FAILED|CANCELLED|TIMEOUT|LOST)
      break
      ;;
  esac
  sleep 1
done

detail="$(curl -fsS "http://127.0.0.1:8080/jobs/${job_id}")"
status="$(printf '%s' "$detail" | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')"
branch="$(printf '%s' "$detail" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("branch",""))')"
stdout_preview="$(printf '%s' "$detail" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("stdout_preview",""))')"

echo "[e2e] job_id=${job_id} status=${status} branch=${branch}"
echo "[e2e] stdout_preview=${stdout_preview}"

if [[ "$status" != "COMPLETED" ]]; then
  echo "[e2e] Job did not complete successfully"
  exit 1
fi

echo "[e2e] OK"
