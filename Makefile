PROD_HOST ?= 192.168.3.160
PROD_USER ?= root
PROD_DIR  = /opt/ragv2

.PHONY: lint test check deploy logs status restart

# — Локальные проверки —

lint:
	.venv/bin/ruff check .

test:
	.venv/bin/pytest -x -q

check: lint test

# — Деплой на prod —

deploy:
	ssh $(PROD_USER)@$(PROD_HOST) \
		"cd $(PROD_DIR) && git pull && docker compose up -d --build"

logs:
	ssh $(PROD_USER)@$(PROD_HOST) \
		"cd $(PROD_DIR) && docker compose logs -f app"

status:
	ssh $(PROD_USER)@$(PROD_HOST) \
		"cd $(PROD_DIR) && docker compose ps"

restart:
	ssh $(PROD_USER)@$(PROD_HOST) \
		"cd $(PROD_DIR) && docker compose restart app"
