# WikiMasters — raccourcis de développement
PYTHON ?= python3
REDIS_URL ?= redis://localhost:6379/0
SERVER ?= user@mon-serveur

.PHONY: help up down logs ps test test-tracker test-ml test-api tunnel train train-smoke deploy-model

help: ## Liste des commandes
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up: ## Construit et lance la stack (docker compose)
	docker compose up -d --build

down: ## Arrête la stack
	docker compose down

logs: ## Suit les logs de la stack
	docker compose logs -f --tail=100

ps: ## État des services
	docker compose ps

test: test-tracker test-ml test-api ## Lance tous les tests

test-tracker: ## Tests du tracker (pytest)
	cd tracker && $(PYTHON) -m pytest -q

test-ml: ## Tests ML (les tests PyTorch sont ignorés si torch est absent)
	cd ml_predictor && $(PYTHON) -m pytest -q

test-api: ## Tests de l'API Fastify (node:test)
	cd api_bridge && npm test

tunnel: ## Tunnel SSH vers le Redis du serveur (SERVER=user@host)
	ssh -N -L 6379:127.0.0.1:6379 $(SERVER)

train: ## Entraîne le modèle sur le GPU local à partir du Redis (tunnel ouvert)
	cd ml_predictor && $(PYTHON) train.py --redis-url $(REDIS_URL)

train-smoke: ## Entraînement express sur données synthétiques
	cd ml_predictor && $(PYTHON) train.py --synthetic 200 --epochs 5 --out models/smoke

deploy-model: ## Copie model.onnx + model.json vers le serveur (SERVER=..., MODEL_DIR=...)
	scp ml_predictor/models/model.onnx ml_predictor/models/model.json $(SERVER):$(or $(MODEL_DIR),/opt/wikimasters/models)/
