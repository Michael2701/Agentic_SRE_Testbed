COMPOSE := docker compose
RATE ?= 5
DURATION ?= 60
FAULTS_URL := http://localhost:$${FAULT_INJECTOR_PORT:-8090}
PARAMS ?= {}
PRETTY := python3 -m json.tool
comma := ,

.PHONY: up down test test-faults load fault faults recover logs ps

up: ## Build and start the whole environment, wait until healthy
	$(COMPOSE) up -d --build --wait
	@echo "App:        http://localhost:$${NGINX_PORT:-8080}"
	@echo "Grafana:    http://localhost:$${GRAFANA_PORT:-3000}"
	@echo "Prometheus: http://localhost:$${PROMETHEUS_PORT:-9090}"

down: ## Stop the environment
	$(COMPOSE) down

test: ## Run all tests (integration, then faults) against the running environment
	$(COMPOSE) --profile test run --rm --build tests

test-faults: ## Run only the fault injection tests (~3 min)
	$(COMPOSE) --profile test run --rm --build tests pytest -v -p no:cacheprovider faults

fault: ## Inject a fault: make fault TYPE=payment-latency PARAMS='{"latency_ms":2000}' [TARGET=payment] [EXP=exp-1]
	@test -n "$(TYPE)" || { echo "TYPE is required (payment-latency | payment-error | service-unavailable)"; exit 2; }
	@curl -s -XPOST $(FAULTS_URL)/faults -H 'content-type: application/json' \
		-d '{"type":"$(subst -,_,$(TYPE))","parameters":$(PARAMS)$(if $(TARGET),$(comma)"target":"$(TARGET)")$(if $(EXP),$(comma)"experiment_id":"$(EXP)")}' | $(PRETTY)

faults: ## List active faults
	@curl -s "$(FAULTS_URL)/faults?state=active" | $(PRETTY)

recover: ## Remove all active faults
	@curl -s -XDELETE $(FAULTS_URL)/faults | $(PRETTY)

load: ## Generate steady traffic: make load RATE=5 DURATION=60
	$(COMPOSE) --profile test run --rm --build tests python load.py --rate $(RATE) --duration $(DURATION)

logs: ## Follow logs of all services
	$(COMPOSE) logs -f

ps: ## Show service status
	$(COMPOSE) ps
