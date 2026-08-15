.PHONY: help install-dev lint format test build check-all

help:
	@echo "Comandos disponíveis:"
	@echo "  make install-dev - Instala dependências de produção e desenvolvimento"
	@echo "  make lint        - Executa análise estática / linter (Ruff & Flake8)"
	@echo "  make format      - Formata o código automaticamente (Ruff format)"
	@echo "  make test        - Executa os testes unitários e gera relatório de cobertura"
	@echo "  make build       - Executa o build da imagem Docker"
	@echo "  make check-all   - Executa lint, test e build sequencialmente"

install-dev:
	pip install -r requirements.txt -r requirements-dev.txt

lint:
	ruff check .
	flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics

format:
	ruff format .

test:
	pytest

build:
	docker build -t analytics-service:latest -f Dockerfile.analytics .

check-all: lint test build
