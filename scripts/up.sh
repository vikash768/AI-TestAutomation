#!/usr/bin/env bash
# =============================================================================
# Start all infrastructure services
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "=== AI Testing Automation — Starting Infrastructure ==="

# Load .env if it exists
if [ -f .env ]; then
    echo "Loading .env configuration..."
else
    echo "WARNING: .env file not found. Copy .env.example to .env and configure."
    echo "  cp .env.example .env"
    exit 1
fi

# Create artifact directories
mkdir -p artifacts/robot artifacts/screenshots artifacts/logs

# Build test runner image
echo "Building test-runner image..."
docker compose build test-runner

# Start infrastructure services (not test-runner — it uses profiles)
echo "Starting services..."
docker compose up -d selenium pushgateway prometheus grafana

echo ""
echo "Waiting for services to become healthy..."
sleep 5

# Wait for Selenium readiness
echo -n "Waiting for Selenium..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:${SELENIUM_PORT:-4444}/wd/hub/status > /dev/null 2>&1; then
        echo " ready!"
        break
    fi
    echo -n "."
    sleep 2
done

echo ""
echo "=== Infrastructure Started ==="
echo ""
echo "Service URLs:"
echo "  Selenium:    http://localhost:${SELENIUM_PORT:-4444}"
echo "  Selenium VNC: http://localhost:${SELENIUM_VNC_PORT:-7900} (password: secret)"
echo "  Pushgateway: http://localhost:${PUSHGATEWAY_PORT:-9091}"
echo "  Prometheus:  http://localhost:${PROMETHEUS_PORT:-9090}"
echo "  Grafana:     http://localhost:${GRAFANA_PORT:-3000} (admin/admin)"
echo ""
echo "Run './scripts/health.sh' to verify all services."
