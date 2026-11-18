#!/usr/bin/env bash
# Build the four scanner images the worker starts at scan time.
#
#   ./scripts/build_scanners.sh
#
# Each image is pinned to the version the adapters name (see backend/app/scanners).
# Docker reuses layers, so a second run is fast; on a machine that has never built
# them this is required before an audit can scan anything.
set -euo pipefail

cd "$(dirname "$0")/.."

build() {
  local tag="$1" context="$2"
  printf '\n== building %s ==\n' "$tag"
  docker build -t "$tag" "$context"
}

build due-diligence-gitleaks:8.28.0 docker/gitleaks
build due-diligence-semgrep:1.182.1 docker/semgrep
build due-diligence-osv:2.7.0 docker/osv
build due-diligence-scancode:32.5.0 docker/scancode

printf '\nscanner images ready:\n'
for tag in due-diligence-gitleaks:8.28.0 due-diligence-semgrep:1.182.1 \
           due-diligence-osv:2.7.0 due-diligence-scancode:32.5.0; do
  docker image inspect --format '  {{.RepoTags}}' "$tag" >/dev/null 2>&1 \
    || { echo "missing image $tag" >&2; exit 1; }
  printf '  %s\n' "$tag"
done
