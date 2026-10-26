# =============================================================================
# AI-TestAutomation — Makefile
# =============================================================================
.PHONY: up down build logs health test run open clean psql

## Build images and start every service (Postgres, API, worker, Prometheus, Grafana)
up:
	docker compose up -d --build
	@echo ""
	@echo "  UI:         http://localhost:$${API_PORT:-8000}"
	@echo "  Grafana:    http://localhost:$${GRAFANA_PORT:-3000}  (admin / admin)"
	@echo "  Prometheus: http://localhost:$${PROMETHEUS_PORT:-9090}"

## Stop every service (data volumes are kept)
down:
	docker compose down

## Rebuild the application image only
build:
	docker compose build api worker

## Tail logs of the worker and API
logs:
	docker compose logs -f --tail=100 worker api

## Check that each service answers
health:
	@curl -sf http://localhost:$${API_PORT:-8000}/healthz > /dev/null && echo "  api        ok" || echo "  api        DOWN"
	@curl -sf http://localhost:$${GRAFANA_PORT:-3000}/api/health > /dev/null && echo "  grafana    ok" || echo "  grafana    DOWN"
	@curl -sf http://localhost:$${PROMETHEUS_PORT:-9090}/-/healthy > /dev/null && echo "  prometheus ok" || echo "  prometheus DOWN"
	@docker compose exec -T postgres pg_isready -U $${POSTGRES_USER:-qa} > /dev/null 2>&1 && echo "  postgres   ok" || echo "  postgres   DOWN"

## Run the unit tests locally (needs .venv: python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium)
test:
	.venv/bin/python -m pytest tests/unit -q

## Run one pipeline from the terminal inside the worker container:
##   make run REPO=https://github.com/org/app.git LIVE=https://app.example.com
run:
	docker compose exec worker python -m qaplatform.cli run --repo "$(REPO)" --live "$(LIVE)" --project "$${PROJECT_NAME:-default}"

## Open a psql shell on the findings database
psql:
	docker compose exec postgres psql -U $${POSTGRES_USER:-qa} -d $${POSTGRES_DB:-qa}

## Remove run artifacts (reports, screenshots, cloned repos) but keep the database
clean:
	rm -rf artifacts/runs/* artifacts/repos/* artifacts/cache/*
