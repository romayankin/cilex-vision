.PHONY: up down test lint proto-check migrate seed status launch

COMPOSE ?= docker compose
COMPOSE_FILE ?= infra/docker-compose.yml

up:
	$(COMPOSE) -f $(COMPOSE_FILE) up -d

down:
	$(COMPOSE) -f $(COMPOSE_FILE) down --remove-orphans

test:
	@python -m pytest tests/ -v; \
	status=$$?; \
	if [ $$status -ne 0 ] && [ $$status -ne 5 ]; then \
		exit $$status; \
	fi

lint:
	ruff check .
	mypy --ignore-missing-imports services/

proto-check:
	buf lint proto/
	buf breaking proto/ --against '.git#branch=main'

migrate:
	cd services/db && alembic upgrade head

seed:
	@if [ -f scripts/data/seed.py ]; then \
		python scripts/data/seed.py; \
	else \
		echo "scripts/data/seed.py not implemented yet"; \
	fi

status:
	.agents/status.sh

launch:
	@echo "Usage: .agents/launch.sh <task-id>"
