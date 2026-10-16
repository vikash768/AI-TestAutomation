#!/usr/bin/env bash
# =============================================================================
# Clean up artifacts, Docker volumes, and metrics
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "=== AI Testing Automation — Cleanup ==="

echo "Removing test artifacts..."
rm -rf artifacts/robot/*
rm -rf artifacts/screenshots/*
rm -rf artifacts/logs/*
rm -rf artifacts/runs/*
if [ "${CLEAN_CACHE:-false}" = "true" ]; then
    echo "Clearing AI test plan cache..."
    rm -rf artifacts/cache/*
fi

echo "Clearing Pushgateway metrics..."
curl -sf -X PUT "http://localhost:${PUSHGATEWAY_PORT:-9091}/api/v1/admin/wipe" 2>/dev/null || \
    echo "  (Pushgateway not reachable — skipping)"

echo "Stopping and removing containers + volumes..."
docker compose down -v --remove-orphans 2>/dev/null || true

echo ""
echo "=== Cleanup complete ==="
