# =============================================================================
# AI Testing Automation — Makefile
# =============================================================================

WEB_SUITE ?= testsuites/trackpilot.testsuite.md
MOBILE_SUITE ?=
DESKTOP_SUITE ?= testsuites/drawio-desktop.testsuite.md

.PHONY: run up down health build test test-ai test-web test-suite test-desktop test-mobile test-mobile-detailed demo-web demo-mobile demo-desktop clean help

## Run unified pipeline (usage: make run repo=https://github.com/org/repo platform=android)
run:
	./scripts/run-suite.sh $(if $(repo),--repo-url $(repo),) $(if $(platform),--platform $(platform),) $(if $(suite),--suite $(suite),)

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

## Run the default Selenium web demo (TrackPilot)
test-ai: test-web

test-web:
	AI_MAX_CATEGORIES= AI_MAX_TESTS= TARGET_TYPE=web PROJECT_NAME=$(or $(PROJECT),trackpilot) \
		APP_REPO_URL= REQUIRE_FULL_MD_COVERAGE=true \
		./scripts/run-suite.sh $(or $(SUITE),$(WEB_SUITE))

## Run AI-driven tests for a specific suite (usage: make test-suite SUITE=testsuites/xxx.testsuite.md)
## Supports optional TARGET_TYPE=web|desktop|mobile (default: web)
test-suite:
	AI_MAX_CATEGORIES= AI_MAX_TESTS= TARGET_TYPE=$(or $(TARGET_TYPE),web) \
		REQUIRE_FULL_MD_COVERAGE=true ./scripts/run-suite.sh $(SUITE)

## Run AI-driven tests (defaults to the Selenium web demo)
test: test-web

## Run desktop tests via Appium (usage: make test-desktop SUITE=testsuites/xxx.testsuite.md)
test-desktop:
	AI_MAX_CATEGORIES= AI_MAX_TESTS= TARGET_TYPE=desktop REQUIRE_REPO_CONTEXT=true \
		REQUIRE_BUILD_CONTEXT=true REQUIRE_FULL_MD_COVERAGE=true \
		./scripts/run-suite.sh $(or $(SUITE),$(DESKTOP_SUITE))

## Run mobile tests via Appium (usage: make test-mobile SUITE=testsuites/xxx.testsuite.md)
test-mobile:
	AI_MAX_CATEGORIES= AI_MAX_TESTS= TARGET_TYPE=mobile \
		REQUIRE_REPO_CONTEXT=true REQUIRE_BUILD_CONTEXT=true REQUIRE_FULL_MD_COVERAGE=true \
		./scripts/run-suite.sh $(or $(SUITE),$(MOBILE_SUITE))

## Explicit alias for the full repository- and build-grounded mobile suite.
test-mobile-detailed: test-mobile

## Lead-demo commands limit AI generation to one category for a fast live run.
## Override with AI_MAX_CATEGORIES=N when a longer demo is desired.
demo-web:
	AI_MAX_CATEGORIES=$(or $(AI_MAX_CATEGORIES),1) AI_MAX_TESTS=$(or $(AI_MAX_TESTS),1) TARGET_TYPE=web \
		PROJECT_NAME=$(or $(PROJECT),trackpilot) APP_REPO_URL= \
		./scripts/run-suite.sh $(or $(SUITE),$(WEB_SUITE))

demo-mobile:
	AI_MAX_CATEGORIES=$(or $(AI_MAX_CATEGORIES),1) AI_MAX_TESTS=$(or $(AI_MAX_TESTS),1) TARGET_TYPE=mobile \
		REQUIRE_REPO_CONTEXT=true \
		REQUIRE_BUILD_CONTEXT=true MOBILE_RESET_APP=true \
		./scripts/run-suite.sh $(or $(SUITE),$(MOBILE_SUITE))

demo-desktop:
	AI_MAX_CATEGORIES=$(or $(AI_MAX_CATEGORIES),1) AI_MAX_TESTS=$(or $(AI_MAX_TESTS),1) TARGET_TYPE=desktop \
		REQUIRE_REPO_CONTEXT=true REQUIRE_BUILD_CONTEXT=true ./scripts/run-suite.sh $(or $(SUITE),$(DESKTOP_SUITE))

## Clean up test run outputs (runs, screenshots, logs) while keeping AI test plan cache
clean-runs:
	rm -rf artifacts/runs/* artifacts/screenshots/* artifacts/logs/*

## Clean up artifacts, volumes, and containers
clean:
	./scripts/clean.sh

## Clean everything including AI test plan cache
clean-all:
	CLEAN_CACHE=true ./scripts/clean.sh

## Show help
help:
	@echo "AI Testing Automation — Available targets:"
	@echo ""
	@echo "  make run repo=<url> platform=<platform> [suite=<file>]"
	@echo "                     Unified single-command pipeline: analyze, source build, execute"
	@echo "  make up            Start infrastructure"
	@echo "  make health        Check service health"
	@echo "  make build         Build test runner image"
	@echo ""
	@echo "  make demo-web      Run TrackPilot in Selenium (starts/checks Selenium)"
	@echo "  make demo-mobile  [SUITE=testsuites/xxx.testsuite.md]"
	@echo "                     Run one mobile category in Appium (starts/checks emulator + Appium)"
	@echo "  make demo-desktop  Run one source/build-grounded desktop category via Appium"
	@echo "  make test-web      Run the complete TrackPilot Selenium suite"
	@echo "  make test-ai       Same as test-web (backward-compatible)"
	@echo "  make test-suite SUITE=testsuites/xxx.testsuite.md [TARGET_TYPE=web|desktop|mobile]"
	@echo "                     Run AI tests for a specific suite"
	@echo "  make test-desktop [SUITE=testsuites/xxx.testsuite.md]"
	@echo "                     Run repo/build-grounded desktop tests via Appium"
	@echo "  make test-mobile  [SUITE=testsuites/xxx.testsuite.md]"
	@echo "                     Run repo/build-grounded mobile tests via Appium"
	@echo ""
	@echo "  make test          Same as test-web"
	@echo "  make down          Stop infrastructure"
	@echo "  make clean         Full cleanup"
	@echo ""
