COMPOSE := docker compose

.PHONY: up down test logs ps

up: ## Build and start the whole environment, wait until healthy
	$(COMPOSE) up -d --build --wait

down: ## Stop the environment
	$(COMPOSE) down

test: ## Run integration tests against the running environment
	$(COMPOSE) --profile test run --rm --build tests

logs: ## Follow logs of all services
	$(COMPOSE) logs -f

ps: ## Show service status
	$(COMPOSE) ps
