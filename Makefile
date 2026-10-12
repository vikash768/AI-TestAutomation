# =============================================================================
# AI Testing Automation — Makefile
# =============================================================================

.PHONY: up down health build test test-login test-ai test-suite clean help

## Start all infrastructure services
up:
	./scripts/up.sh

## Stop all services
down:
	./scripts/down.sh

## Check health of all services
health:
	./scripts/health.sh

## Build the test runner container
build:
	docker compose build test-runner



## Run AI-driven tests (default: TrackPilot)
test-ai:
	./scripts/run-suite.sh testsuites/trackpilot.testsuite.md

## Run AI-driven tests for a specific suite (usage: make test-suite SUITE=testsuites/xxx.testsuite.md)
test-suite:
	./scripts/run-suite.sh $(SUITE)

## Run AI-driven tests (default: TrackPilot)
test: test-ai

## Run legacy Robot Framework tests (if needed)
test-robot:
	docker compose run --rm test-runner robot \
		--listener automation/listeners/prometheus_listener.py \
		--outputdir /opt/testing/artifacts/robot \
		--loglevel INFO \
		tests/robot/suites/

## Clean up artifacts, volumes, and containers
clean:
	./scripts/clean.sh

## Show help
help:
	@echo "AI Testing Automation — Available targets:"
	@echo ""
	@echo "  make up            Start infrastructure"
	@echo "  make health        Check service health"
	@echo "  make build         Build test runner image"
	@echo ""
	@echo "  make test-ai       Run AI-driven tests (TrackPilot)"
	@echo "  make test-suite SUITE=testsuites/xxx.testsuite.md"
	@echo "                     Run AI tests for a specific suite"
	@echo ""
	@echo "  make test          Run AI-driven tests (default: TrackPilot)"
	@echo "  make test-robot    Run legacy Robot Framework tests"
	@echo ""
	@echo "  make down          Stop infrastructure"
	@echo "  make clean         Full cleanup"
	@echo ""
