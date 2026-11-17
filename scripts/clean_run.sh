#!/usr/bin/env bash
# Step 3.12: the clean-machine run.
#
# From a checkout with nothing running this script:
#   1. writes .env from .env.example if it is missing (random JWT secret and
#      one random password per role, printed once so the UI can be signed into)
#   2. docker compose up --build for the services the run needs, waiting for health
#   3. seeds the three accounts through the `seed` service
#   4. uploads scripts/fixtures/release_app and starts an audit
#   5. waits for the review gate (three fake secrets escalate the audit to critical)
#   6. approves it as the reviewer and waits for the run to finish
#   7. downloads the PDF report and checks it is a PDF
#
# The fixture is synthetic and its "credentials" are fake. Nothing is executed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

API="http://127.0.0.1:8000"
FIXTURE="$ROOT/scripts/fixtures/release_app"
ARTIFACTS="$ROOT/artifacts"
TIMEOUT_SECONDS="${CLEAN_RUN_TIMEOUT:-1200}"

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys' >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "error: a working python3 or python is required on the host for JSON handling" >&2
  echo "hint: on Windows the Microsoft Store 'python' alias is not an interpreter" >&2
  exit 2
fi
echo "using interpreter: $PY ($($PY -c 'import sys; print(sys.version.split()[0])'))"

say() { printf '\n== %s ==\n' "$*"; }
fail() { printf '\n!! %s\n' "$*" >&2; exit 1; }

# --- 1. environment ---------------------------------------------------------
say "environment"
if [ ! -f .env ]; then
  cp .env.example .env
fi
# Fill only the values this run needs that are missing or empty. An existing .env
# keeps every value it already has.
"$PY" - <<'PY'
from pathlib import Path
import secrets

path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines()
values = {
    "JWT_SECRET": secrets.token_hex(32),
    "SEED_ANALYST_EMAIL": "analyst@example.com",
    "SEED_ANALYST_PASSWORD": secrets.token_urlsafe(18),
    "SEED_REVIEWER_EMAIL": "reviewer@example.com",
    "SEED_REVIEWER_PASSWORD": secrets.token_urlsafe(18),
    "SEED_ADMIN_EMAIL": "admin@example.com",
    "SEED_ADMIN_PASSWORD": secrets.token_urlsafe(18),
}
filled = []
for key, value in values.items():
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            current = line.split("=", 1)[1].strip()
            placeholder = current.startswith("change-me")
            if current and not placeholder:
                replaced = True  # a real value wins; never overwrite it
            else:
                lines[index] = f"{key}={value}"
                filled.append(key)
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
        filled.append(key)
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("filled: " + (", ".join(filled) if filled else "nothing (all values present)"))
PY

set -a
# shellcheck disable=SC1091
. ./.env
set +a

if [ -z "${SEED_ANALYST_EMAIL:-}" ] || [ -z "${SEED_ANALYST_PASSWORD:-}" ] ||
   [ -z "${SEED_REVIEWER_EMAIL:-}" ] || [ -z "${SEED_REVIEWER_PASSWORD:-}" ]; then
  fail "SEED_ANALYST_* and SEED_REVIEWER_* must be set in .env"
fi

# The scanner containers run on the host daemon, so they need the host path of
# the data directory, not the path inside the worker container.
if command -v cygpath >/dev/null 2>&1; then
  HOST_DATA_DIR="$(cygpath -m "$ROOT/data")"
elif pwd -W >/dev/null 2>&1; then
  HOST_DATA_DIR="$(pwd -W)/data"
else
  HOST_DATA_DIR="$ROOT/data"
fi
export HOST_DATA_DIR
# No model is required: the fixture is judged by the deterministic scanners only.
export AGENTS_ENABLED=false
mkdir -p "$HOST_DATA_DIR"
echo "data directory on the host: $HOST_DATA_DIR"

# --- 2. compose -------------------------------------------------------------
say "docker compose up (postgres, redis, litellm, api, worker, frontend)"
docker compose up -d --build --wait postgres redis litellm api worker frontend

# --- 3. seed ----------------------------------------------------------------
say "seed users"
docker compose run --rm seed

# --- helper ----------------------------------------------------------------
json_get() { "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d[sys.argv[1]])' "$1"; }

wait_for_api() {
  for _ in $(seq 1 60); do
    if curl -sf "$API/health" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  fail "API did not answer /health"
}
wait_for_api

login() {
  "$PY" -c 'import json,sys; print(json.dumps({"email": sys.argv[1], "password": sys.argv[2]}))' "$1" "$2" |
    curl -sf -X POST "$API/api/v1/auth/login" -H 'Content-Type: application/json' --data @- |
    json_get access_token
}

# --- 4. upload and start ----------------------------------------------------
say "upload $FIXTURE and start the audit"
ZIP="$ROOT/artifacts/release_app.zip"
mkdir -p "$ARTIFACTS"
"$PY" - "$FIXTURE" "$ZIP" <<'PY'
import sys, zipfile
from pathlib import Path

source, target = Path(sys.argv[1]), Path(sys.argv[2])
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(source.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(source).as_posix())
PY

ANALYST_TOKEN="$(login "$SEED_ANALYST_EMAIL" "$SEED_ANALYST_PASSWORD")"
[ -n "$ANALYST_TOKEN" ] || fail "analyst login returned no token"

REPOSITORY_ID="$(
  curl -sf -H "Authorization: Bearer $ANALYST_TOKEN" \
    -F "file=@$ZIP" -F "name=release_app" \
    "$API/api/v1/repositories" | json_get id
)"
echo "repository $REPOSITORY_ID"

AUDIT_ID="$(
  curl -sf -X POST -H "Authorization: Bearer $ANALYST_TOKEN" \
    "$API/api/v1/repositories/$REPOSITORY_ID/audits" | json_get id
)"
echo "audit $AUDIT_ID"

# --- 5. wait for the review gate -------------------------------------------
say "wait for the review gate"
STATUS=""
DEADLINE=$(( $(date +%s) + TIMEOUT_SECONDS ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  STATUS="$(curl -sf -H "Authorization: Bearer $ANALYST_TOKEN" "$API/api/v1/audits/$AUDIT_ID" | json_get status)"
  printf '   status: %s\n' "$STATUS"
  case "$STATUS" in
    awaiting_review) break ;;
    completed)
      fail "the audit completed without reaching the review gate (risk was not critical)" ;;
    failed|needs_evidence)
      fail "the audit stopped at $STATUS before review" ;;
  esac
  sleep 5
done
[ "$STATUS" = "awaiting_review" ] || fail "timed out waiting for awaiting_review (last: $STATUS)"

# --- 6. review decision -----------------------------------------------------
say "approve as reviewer"
REVIEWER_TOKEN="$(login "$SEED_REVIEWER_EMAIL" "$SEED_REVIEWER_PASSWORD")"
[ -n "$REVIEWER_TOKEN" ] || fail "reviewer login returned no token"

REVIEW_ID="$(
  curl -sf -H "Authorization: Bearer $REVIEWER_TOKEN" "$API/api/v1/reviews?status=pending" |
    "$PY" -c '
import json, sys
payload = json.load(sys.stdin)
audit = sys.argv[1]
for review in payload["reviews"]:
    if review["audit_id"] == audit:
        print(review["id"])
        break
' "$AUDIT_ID"
)"
[ -n "$REVIEW_ID" ] || fail "no pending review for audit $AUDIT_ID"
echo "review $REVIEW_ID"

curl -sf -X POST -H "Authorization: Bearer $REVIEWER_TOKEN" -H 'Content-Type: application/json' \
  --data '{"decision":"approve","reason":"clean run: assessment accepted"}' \
  "$API/api/v1/reviews/$REVIEW_ID/decision" >/dev/null

STATUS=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  STATUS="$(curl -sf -H "Authorization: Bearer $ANALYST_TOKEN" "$API/api/v1/audits/$AUDIT_ID" | json_get status)"
  printf '   status: %s\n' "$STATUS"
  case "$STATUS" in
    completed) break ;;
    failed|needs_evidence) fail "the audit stopped at $STATUS after the decision" ;;
  esac
  sleep 5
done
[ "$STATUS" = "completed" ] || fail "timed out waiting for completion (last: $STATUS)"

# --- 7. report --------------------------------------------------------------
say "download the report"
PDF="$ARTIFACTS/clean-run-$AUDIT_ID.pdf"
HTML="$ARTIFACTS/clean-run-$AUDIT_ID.html"
curl -sf -H "Authorization: Bearer $ANALYST_TOKEN" "$API/api/v1/audits/$AUDIT_ID/report" -o "$HTML"
curl -sf -H "Authorization: Bearer $ANALYST_TOKEN" "$API/api/v1/audits/$AUDIT_ID/report.pdf" -o "$PDF"

HEADER="$(head -c 5 "$PDF" || true)"
[ "$HEADER" = "%PDF-" ] || fail "report.pdf is not a PDF (starts with: $HEADER)"
BYTES="$(wc -c < "$PDF" | tr -d ' ')"
[ "$BYTES" -gt 5000 ] || fail "report.pdf is suspiciously small ($BYTES bytes)"

say "clean run complete"
cat <<EOF
repository        $REPOSITORY_ID
audit             $AUDIT_ID
final status      $STATUS
html report       $HTML
pdf report        $PDF ($BYTES bytes)
analyst account   $SEED_ANALYST_EMAIL
reviewer account  $SEED_REVIEWER_EMAIL
UI                http://localhost:5173
EOF
