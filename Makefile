# Makefile aerobriefer — validation avant commit.
#
# `make check` est la porte d'entrée : format, lint, typage statique, tests.
# Elle doit passer AVANT tout commit. `make build` en est un alias.
#
# Utilise le venv local (.venv). PY pointe dessus ; surchargeable :
#   make check PY=python3

PY ?= .venv/bin/python
RUFF = $(PY) -m ruff
MYPY = $(PY) -m mypy
PYTEST = $(PY) -m pytest

SRC = src tests

.DEFAULT_GOAL := check

.PHONY: help
help: ## Liste les cibles
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Installe le paquet + outils de dev dans le venv
	$(PY) -m pip install -e ".[dev,providers,render]"

.PHONY: format
format: ## Formate le code (modifie les fichiers)
	$(RUFF) format $(SRC)
	$(RUFF) check --fix $(SRC)

.PHONY: format-check
format-check: ## Vérifie le formatage SANS modifier (échoue si non formaté)
	$(RUFF) format --check $(SRC)

.PHONY: lint
lint: ## Lint (dont typage obligatoire des fonctions, bannissement de datetime)
	$(RUFF) check $(SRC)

.PHONY: typecheck
typecheck: ## Typage statique strict (mypy)
	$(MYPY)

.PHONY: test
test: ## Tests hors-ligne (exclut les tests réseau)
	$(PYTEST) -q -m "not network"

.PHONY: test-network
test-network: ## Tous les tests, y compris ceux qui frappent les vraies API
	AEROBRIEFER_NETWORK_TESTS=1 $(PYTEST) -q

# La cible de validation complète : c'est elle qui garde le commit.
.PHONY: check
check: format-check lint typecheck test ## Validation complète (format + lint + types + tests)
	@echo "\033[32m✓ check OK — prêt à committer\033[0m"

.PHONY: build
build: check ## Alias de check

.PHONY: clean
clean: ## Nettoie caches et artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
