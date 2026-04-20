# News Analytics Stack — Makefile
# Run `make help` to see all commands
#
# Folder flat: scripts ở ngay main_process/, mapping/schema cũng ở đây.
# Input default glob match multi-partition (..._1.jsonl, ..._2.jsonl, ...).

INPUT_JSONL ?= ../data/articles_ner*.jsonl
CLEAN_JSONL ?= ../data/articles_ready.jsonl
CLEAN_GLOB  ?= ../data/articles_ready*.jsonl
ES_URL      ?= http://localhost:9200
CH_HOST     ?= localhost
CH_PORT     ?= 9000

.PHONY: help
help:
	@echo "News Analytics Stack"
	@echo ""
	@echo "QUICK START (run in order):"
	@echo "  make install          Install Python deps"
	@echo "  make up               Start docker services (ES, Kibana, ClickHouse)"
	@echo "  make install-icu      Install ICU plugin on Elasticsearch"
	@echo "  make clean-entities   Clean noise entities from NER output"
	@echo "  make index-es         Index articles to Elasticsearch"
	@echo "  make load-ch          Load articles to ClickHouse"
	@echo "  make verify           Run demo queries on all 3 scenarios"
	@echo ""
	@echo "ONE-SHOT:"
	@echo "  make all              Do everything from clean → verify"
	@echo ""
	@echo "OTHER:"
	@echo "  make ps               Show running containers"
	@echo "  make logs-es          Tail Elasticsearch logs"
	@echo "  make logs-ch          Tail ClickHouse logs"
	@echo "  make down             Stop services"
	@echo "  make clean-docker     Stop + remove containers & volumes (DATA LOSS!)"
	@echo ""
	@echo "CONFIG (via env):"
	@echo "  INPUT_JSONL = $(INPUT_JSONL)"
	@echo "  CLEAN_JSONL = $(CLEAN_JSONL)"
	@echo "  CLEAN_GLOB  = $(CLEAN_GLOB)"


.PHONY: install
install:
	pip install 'elasticsearch>=8,<9' clickhouse-driver

.PHONY: up
up:
	docker-compose up -d
	@echo "Waiting for services healthy..."
	@sleep 5
	@docker-compose ps

.PHONY: down
down:
	docker-compose down

.PHONY: clean-docker
clean-docker:
	docker-compose down -v

.PHONY: ps
ps:
	docker-compose ps

.PHONY: logs-es
logs-es:
	docker logs -f --tail 50 news-es

.PHONY: logs-ch
logs-ch:
	docker logs -f --tail 50 news-ch

.PHONY: install-icu
install-icu:
	docker exec news-es bin/elasticsearch-plugin install --batch analysis-icu || true
	docker restart news-es
	@echo "Waiting for ES to restart..."
	@sleep 15


.PHONY: clean-entities
clean-entities:
	python 01_clean_entities.py \
	  --input '$(INPUT_JSONL)' \
	  --output $(CLEAN_JSONL)

.PHONY: index-es
index-es:
	python 02_index_to_es.py \
	  --input '$(CLEAN_GLOB)' \
	  --es $(ES_URL) \
	  --mapping es_mapping.json \
	  --reset

.PHONY: load-ch
load-ch:
	python 03_load_to_ch.py \
	  --input '$(CLEAN_GLOB)' \
	  --host $(CH_HOST) --port $(CH_PORT) \
	  --schema clickhouse_schema.sql \
	  --reset

.PHONY: verify
verify:
	python 04_verify.py \
	  --es $(ES_URL) \
	  --ch-host $(CH_HOST) --ch-port $(CH_PORT)

.PHONY: all
all: clean-entities index-es load-ch verify
