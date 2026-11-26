# =============================================================================
# AI-TestAutomation — Makefile
# =============================================================================
.PHONY: up down build logs health test run run-android run-desktop app-worker app-worker-start app-worker-stop app-worker-status appium open clean reset-runs psql security-check

## Build images and start every service (Postgres, API, worker, Prometheus, Grafana)
up:
	@mkdir -p -m 700 .artifacts-uploads
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
	docker compose exec worker python -m qaplatform.cli run --repo "$(REPO)" --live "$(LIVE)" --project "$(PROJECT)"

## Native app runs (Android / iOS / macOS) need this machine's Appium server, emulator and build
## files, so they run in a worker on the host, next to the dockerised web worker:
##   make up && make app-worker          then pick "Android app" or "macOS desktop app" in the form
app-worker:
	@test -x .venv/bin/python || (echo "Create the venv first: python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt" && exit 1)
	set -a; . ./.env; set +a; DATABASE_URL=postgresql+psycopg://$${POSTGRES_USER:-qa}:$${POSTGRES_PASSWORD:-qa}@localhost:$${POSTGRES_PORT:-5434}/$${POSTGRES_DB:-qa} \
		QA_WORKER_PLATFORMS=android,ios,macos .venv/bin/python -m qaplatform.worker

## Same worker, detached from the terminal (own session; survives closing it). Log: artifacts/logs/app-worker.log
app-worker-start:
	@mkdir -p artifacts/logs
	@if [ -f artifacts/app-worker.pid ] && kill -0 $$(cat artifacts/app-worker.pid) 2>/dev/null; then \
		echo "App worker already running (pid $$(cat artifacts/app-worker.pid)). Log: artifacts/logs/app-worker.log"; exit 0; fi; \
	perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV' $(MAKE) --no-print-directory app-worker > artifacts/logs/app-worker.log 2>&1 < /dev/null & \
	echo $$! > artifacts/app-worker.pid; sleep 3; \
	if kill -0 $$(cat artifacts/app-worker.pid) 2>/dev/null; then echo "App worker started (pid $$(cat artifacts/app-worker.pid)). Log: artifacts/logs/app-worker.log"; \
	else echo "App worker exited; see artifacts/logs/app-worker.log"; tail -5 artifacts/logs/app-worker.log; exit 1; fi

app-worker-stop:
	@if [ -f artifacts/app-worker.pid ] && kill -0 $$(cat artifacts/app-worker.pid) 2>/dev/null; then \
		kill -- -$$(cat artifacts/app-worker.pid) 2>/dev/null || kill $$(cat artifacts/app-worker.pid); \
		for i in $$(seq 1 30); do ps -axo pgid= | grep -qw $$(cat artifacts/app-worker.pid) || break; sleep 1; done; \
		echo "App worker stopped (its Appium session closed; an interrupted run goes back to the queue)"; \
	else echo "No app worker running"; fi; rm -f artifacts/app-worker.pid

app-worker-status:
	@if [ -f artifacts/app-worker.pid ] && kill -0 $$(cat artifacts/app-worker.pid) 2>/dev/null; then echo "App worker running (pid $$(cat artifacts/app-worker.pid))"; \
	else echo "App worker not running: make app-worker-start"; fi; tail -3 artifacts/logs/app-worker.log 2>/dev/null || true

## Start a local Appium server (the app worker also starts one on demand when APPIUM_AUTOSTART=true)
appium:
	appium --address 127.0.0.1 --port 4723

## One native run from the terminal, on the host:
##   make run-android APP=/path/app-release.apk PROJECT=my-android-app
##   make run-desktop REPO=https://github.com/jgraph/drawio-desktop APP=/Applications/draw.io.app
run-android:
	set -a; . ./.env; set +a; DATABASE_URL=postgresql+psycopg://$${POSTGRES_USER:-qa}:$${POSTGRES_PASSWORD:-qa}@localhost:$${POSTGRES_PORT:-5434}/$${POSTGRES_DB:-qa} \
		.venv/bin/python -m qaplatform.cli run --platform android --repo "$(REPO)" --app "$(APP)" --project "$(PROJECT)" --email "$${ANDROID_USER_EMAIL:-}" --password "$${ANDROID_USER_PASSWORD:-}"

run-desktop:
	set -a; . ./.env; set +a; DATABASE_URL=postgresql+psycopg://$${POSTGRES_USER:-qa}:$${POSTGRES_PASSWORD:-qa}@localhost:$${POSTGRES_PORT:-5434}/$${POSTGRES_DB:-qa} \
		.venv/bin/python -m qaplatform.cli run --platform macos --repo "$(REPO)" --app "$(or $(APP),$${DESKTOP_APP_PATH})" --project "$(PROJECT)"

## Check the repository for tracked secrets, builds and generated artifacts
security-check:
	./scripts/security-check.sh

## Open a psql shell on the findings database
psql:
	docker compose exec postgres psql -U $${POSTGRES_USER:-qa} -d $${POSTGRES_DB:-qa}

## Remove run artifacts (reports, screenshots, cloned repos) but keep the database
clean:
	rm -rf artifacts/runs/* artifacts/repos/* artifacts/cache/* artifacts/logs/*

## Delete runs from the database (all, or PROJECT=name) with their findings, tests and artifacts; keeps builds
reset-runs:
	@ids=$$(docker compose exec -T postgres psql -U $${POSTGRES_USER:-qa} -d $${POSTGRES_DB:-qa} -qAtc \
		"DELETE FROM runs $(if $(PROJECT),WHERE project = '$(PROJECT)') RETURNING id") || exit 1; \
	for id in $$ids; do rm -rf artifacts/runs/$$id artifacts/repos/$$id; rm -f .artifacts-uploads/$$id.apk; done; \
	echo "Deleted $$(echo $$ids | wc -w | tr -d ' ') run(s)$(if $(PROJECT), for project $(PROJECT))"
