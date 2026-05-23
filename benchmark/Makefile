INPUT_JSONL ?= ../data/articles_ready*.jsonl
RUNS        ?= 20
WARMUP      ?= 3

.PHONY: help
help:
	@echo "News Benchmark — ES vs ClickHouse vs PostgreSQL vs MongoDB"
	@echo ""
	@echo "QUICK START — LATENCY benchmark:"
	@echo "  make install       pip install deps"
	@echo "  make up            docker-compose up Postgres + MongoDB"
	@echo "  make load          load JSONL vào Postgres"
	@echo "  make run           chạy latency benchmark"
	@echo "  make plot          vẽ chart latency"
	@echo "  make all           load + run + plot"
	@echo ""
	@echo "STORAGE comparison (PG vs Mongo vs ES vs CH vs raw JSONL):"
	@echo "  make load-mongo    load JSONL vào MongoDB"
	@echo "  make storage       đo disk usage 4 engines, save md/csv/json"
	@echo "  make plot-storage  vẽ chart storage"
	@echo "  make storage-all   load-mongo + storage + plot-storage"
	@echo ""
	@echo "CONFIG:"
	@echo "  INPUT_JSONL=$(INPUT_JSONL)"
	@echo "  RUNS=$(RUNS) WARMUP=$(WARMUP)"

.PHONY: install
install:
	pip install psycopg2-binary elasticsearch clickhouse-driver matplotlib pymongo

.PHONY: up
up:
	docker-compose up -d

.PHONY: down
down:
	docker-compose down

.PHONY: clean-docker
clean-docker:
	docker-compose down -v

.PHONY: load
load:
	python scripts/01_load_pg.py --input $(INPUT_JSONL) --reset

.PHONY: run
run:
	python scripts/02_run_benchmark.py --runs $(RUNS) --warmup $(WARMUP)

.PHONY: plot
plot:
	python scripts/03_plot_results.py

.PHONY: all
all: load run plot

.PHONY: quick
quick:
	python scripts/02_run_benchmark.py --runs 10 --warmup 2

# ===== STORAGE comparison (MongoDB + PG + ES + CH + raw JSONL) =====
.PHONY: load-mongo
load-mongo:
	python scripts/01_load_mongo.py --input $(INPUT_JSONL) --reset

.PHONY: storage
storage:
	python scripts/04_storage_compare.py

.PHONY: plot-storage
plot-storage:
	python scripts/05_plot_storage.py

.PHONY: storage-all
storage-all: load-mongo storage plot-storage
