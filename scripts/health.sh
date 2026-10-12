#!/usr/bin/env bash
# =============================================================================
# Health check for all infrastructure services
# =============================================================================
set -euo pipefail

# Load .env defaults
SELENIUM_PORT="${SELENIUM_PORT:-4444}"
DEMO_APP_PORT="${DEMO_APP_PORT:-8080}"
PUSHGATEWAY_PORT="${PUSHGATEWAY_PORT:-9091}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-9090}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"

echo "=== AI Testing Automation — Health Check ==="
echo ""

FAILED=0

check_service() {
    local name="$1"
    local url="$2"
    if curl -sf --connect-timeout 5 "$url" > /dev/null 2>&1; then
        echo "  ✅ $name — healthy"
    else
        echo "  ❌ $name — NOT reachable at $url"
        FAILED=$((FAILED + 1))
    fi
}

check_service "Selenium"    "http://localhost:${SELENIUM_PORT}/wd/hub/status"

check_service "Pushgateway" "http://localhost:${PUSHGATEWAY_PORT}/-/healthy"
check_service "Prometheus"  "http://localhost:${PROMETHEUS_PORT}/-/healthy"
check_service "Grafana"     "http://localhost:${GRAFANA_PORT}/api/health"

echo ""
if [ "$FAILED" -eq 0 ]; then
    echo "=== All services healthy ✅ ==="
    exit 0
else
    echo "=== $FAILED service(s) unhealthy ❌ ==="
    echo ""
    echo "Troubleshooting:"
    echo "  1. Run 'docker compose ps' to check container status"
    echo "  2. Run 'docker compose logs <service>' for logs"
    echo "  3. Selenium may need 15-30s to fully start"
    echo "  4. Check if ports are already in use"
    exit 1
fi
