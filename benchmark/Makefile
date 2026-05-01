INPUT_JSONL ?= ../data/articles_ready*.jsonl
RUNS        ?= 20
WARMUP      ?= 3

.PHONY: help
help:
	@echo "News Benchmark — ES vs ClickHouse vs PostgreSQL"
	@echo ""
	@echo "QUICK START:"
	@echo "  make install       pip install deps"
	@echo "  make up            docker-compose up Postgres"
	@echo "  make load          load JSONL vào Postgres"
	@echo "  make run           chạy benchmark"
	@echo "  make plot          vẽ chart từ kết quả mới nhất"
	@echo ""
	@echo "OR:"
	@echo "  make all           load + run + plot"
	@echo ""
	@echo "CONFIG:"
	@echo "  INPUT_JSONL=$(INPUT_JSONL)"
	@echo "  RUNS=$(RUNS) WARMUP=$(WARMUP)"

.PHONY: install
install:
	pip install psycopg2-binary elasticsearch clickhouse-driver matplotlib

.PHONY: up
up:
	docker-compose up -d
	@echo "Waiting for Postgres..."
	@sleep 5

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
	python scripts/03_plot_results.py --csv $$(ls -t results/benchmark_*.csv | head -1)

.PHONY: all
all: load run plot

.PHONY: quick
quick:
	python scripts/02_run_benchmark.py --runs 10 --warmup 2
