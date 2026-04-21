# News Analytics App — Makefile

.PHONY: help
help:
	@echo "News Analytics App (Backend + Frontend)"
	@echo ""
	@echo "LOCAL DEV (không cần docker):"
	@echo "  make install          Install Python deps"
	@echo "  make dev              Run backend với uvicorn --reload"
	@echo "  make frontend-serve   Serve frontend tĩnh bằng python http.server"
	@echo ""
	@echo "DOCKER:"
	@echo "  make build            Build backend image"
	@echo "  make up               Start backend + frontend containers"
	@echo "  make down             Stop & remove"
	@echo "  make logs             Follow logs"
	@echo ""
	@echo "Prerequisites: infra stack phải chạy trước (cd ../infra && make up)"

.PHONY: install
install:
	pip install -r backend/requirements.txt

.PHONY: dev
dev:
	PYTHONPATH=. uvicorn backend.main:app --reload --port 8000

.PHONY: frontend-serve
frontend-serve:
	cd frontend && python -m http.server 8080

.PHONY: build
build:
	docker-compose build

.PHONY: up
up:
	docker-compose up -d
	@echo ""
	@echo "Backend:  http://localhost:8000/docs"
	@echo "Frontend: http://localhost:8080"

.PHONY: down
down:
	docker-compose down

.PHONY: logs
logs:
	docker-compose logs -f
