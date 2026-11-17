#!/usr/bin/env bash
# Create the analyst, reviewer and admin accounts inside the compose network.
#
#   SEED_ANALYST_EMAIL=analyst@example.com SEED_ANALYST_PASSWORD=... \
#   SEED_REVIEWER_EMAIL=reviewer@example.com SEED_REVIEWER_PASSWORD=... \
#   SEED_ADMIN_EMAIL=admin@example.com SEED_ADMIN_PASSWORD=... \
#   ./scripts/seed.sh
#
# Values in .env are used when the shell does not provide them. The script never
# prints a password; the seed command reports which accounts were created.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

exec docker compose run --rm seed
