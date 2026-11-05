.PHONY: setup test lint check demo territory serve web-build
setup:
	uv sync --frozen
	npm --prefix web ci

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	npm --prefix web run lint

check: lint test web-build

demo:
	uv run folego demo

territory:
	uv run folego territory

serve:
	npm --prefix web run dev -- --host 127.0.0.1

web-build:
	npm --prefix web run build
