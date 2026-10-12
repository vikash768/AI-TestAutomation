#!/usr/bin/env bash
# =============================================================================
# Stop all infrastructure services
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "=== AI Testing Automation — Stopping Infrastructure ==="
docker compose down
echo "=== All services stopped ==="
