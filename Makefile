# Port overrides etc. from .env (as compose reads it), exported to recipes and the host scripts.
-include .env
export

SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c  # a failing curl fails the recipe, even when piped into the pretty-printer
COMPOSE := docker compose
CURL := curl -sS --fail-with-body
RATE ?= 5
DURATION ?= 60
FAULTS_URL := http://localhost:$${FAULT_INJECTOR_PORT:-8090}
EXPERIMENTS_URL := http://localhost:$${EXPERIMENT_RUNNER_PORT:-8091}
PARAMS ?= {}
PRETTY := python3 -m json.tool
comma := ,

.PHONY: up down reset smoke acceptance test test-faults test-experiments test-challenges load fault faults recover experiment experiments scenarios logs ps

up: ## Build and start the whole environment, wait until healthy
	$(COMPOSE) up -d --build --wait
	@echo "App:        http://localhost:$${NGINX_PORT:-8080}"
	@echo "Grafana:    http://localhost:$${GRAFANA_PORT:-3000}"
	@echo "Prometheus: http://localhost:$${PROMETHEUS_PORT:-9090}"

down: ## Stop the environment
	$(COMPOSE) down

reset: ## Clean slate: revert faults, delete all state (DB, fault/experiment history, telemetry), start again
	@$(CURL) --max-time 120 -XDELETE $(FAULTS_URL)/faults > /dev/null \
		|| echo "warning: faults not reverted (environment down?); redeploys and edge config come back with the volumes"
	$(COMPOSE) --profile test down -v --remove-orphans
	$(MAKE) up
	@python3 scripts/smoke.py

smoke: ## Quick health check of the running environment (~5 s)
	@python3 scripts/smoke.py

acceptance: ## Run every scenario end to end: make acceptance [FULL=1] [ROUNDS=2] [SCENARIOS="slow-cpu slow-redis"]
	@python3 scripts/acceptance.py $(if $(FULL),--full) --rounds $(or $(ROUNDS),1) $(SCENARIOS)

test: ## Run all tests (integration, then faults) against the running environment
	$(COMPOSE) --profile test run --rm --build tests

test-faults: ## Run only the fault injection tests (~3 min)
	$(COMPOSE) --profile test run --rm --build tests pytest -v -p no:cacheprovider faults

test-experiments: ## Run only the experiment tests (~3 min)
	$(COMPOSE) --profile test run --rm --build tests pytest -v -p no:cacheprovider experiments

test-challenges: ## Run only the M8 challenge scenario tests (~10 min)
	$(COMPOSE) --profile test run --rm --build tests pytest -v -p no:cacheprovider experiments/test_challenges.py

fault: ## Inject a fault: make fault TYPE=payment-latency [PARAMS='{"latency_ms":2500}'] [TARGET=payment] [EXP=exp-1]
	@test -n "$(TYPE)" || { echo "TYPE is required, e.g. payment-latency, network-latency, bad-deployment (all types: README, Fault injection)"; exit 2; }
	@$(CURL) -XPOST $(FAULTS_URL)/faults -H 'content-type: application/json' \
		-d '{"type":"$(subst -,_,$(TYPE))","parameters":$(PARAMS)$(if $(TARGET),$(comma)"target":"$(TARGET)")$(if $(EXP),$(comma)"experiment_id":"$(EXP)")}' | $(PRETTY)

faults: ## List active faults
	@$(CURL) "$(FAULTS_URL)/faults?state=active" | $(PRETTY)

recover: ## Remove all active faults
	@$(CURL) -XDELETE $(FAULTS_URL)/faults | $(PRETTY)

experiment: ## Run a scenario end to end and print the result: make experiment SCENARIO=payment-latency
	@test -n "$(SCENARIO)" || { echo "SCENARIO is required (list: make scenarios)"; exit 2; }
	@$(CURL) --max-time 1800 -XPOST "$(EXPERIMENTS_URL)/experiments?wait=true&format=text" \
		-H 'content-type: application/json' -d '{"scenario":"$(SCENARIO)"}'

experiments: ## List recorded experiments (with hidden ground truth)
	@$(CURL) $(EXPERIMENTS_URL)/experiments | $(PRETTY)

scenarios: ## List scenario files and whether they are valid
	@$(CURL) "$(EXPERIMENTS_URL)/scenarios?format=text"

load: ## Generate steady traffic: make load RATE=5 DURATION=60
	$(COMPOSE) --profile test run --rm --build tests python load.py --rate $(RATE) --duration $(DURATION)

logs: ## Follow logs of all services
	$(COMPOSE) logs -f

ps: ## Show service status
	$(COMPOSE) ps
