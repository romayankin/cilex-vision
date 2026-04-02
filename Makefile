.PHONY: up down test lint proto-check migrate seed status launch

up:
	docker-compose -f infra/docker-compose.yml up -d

down:
	docker-compose -f infra/docker-compose.yml down

test:
	python -m pytest tests/ -v

lint:
	ruff check .
	mypy --ignore-missing-imports services/

proto-check:
	cd proto && buf lint && buf breaking --against ../.git#branch=main

migrate:
	cd services/db && alembic upgrade head

seed:
	python scripts/data/seed.py

status:
	.agents/status.sh

launch:
	@echo "Usage: .agents/launch.sh <task-id>"
