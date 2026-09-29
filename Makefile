COMPOSE := docker compose
RATE ?= 5
DURATION ?= 60

.PHONY: up down test load logs ps

up: ## Build and start the whole environment, wait until healthy
	$(COMPOSE) up -d --build --wait
	@echo "App:        http://localhost:$${NGINX_PORT:-8080}"
	@echo "Grafana:    http://localhost:$${GRAFANA_PORT:-3000}"
	@echo "Prometheus: http://localhost:$${PROMETHEUS_PORT:-9090}"

down: ## Stop the environment
	$(COMPOSE) down

test: ## Run integration tests against the running environment
	$(COMPOSE) --profile test run --rm --build tests

load: ## Generate steady traffic: make load RATE=5 DURATION=60
	$(COMPOSE) --profile test run --rm --build tests python load.py --rate $(RATE) --duration $(DURATION)

logs: ## Follow logs of all services
	$(COMPOSE) logs -f

ps: ## Show service status
	$(COMPOSE) ps
