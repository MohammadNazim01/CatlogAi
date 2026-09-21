# CatalogAI

AI-powered product catalog & marketplace automation platform. Design docs: [`docs/`](docs/).

## Local development

```bash
cp .env.example .env                       # dev-only values; never commit .env
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
curl localhost:8000/health/ready
```

Backend tooling (host side; needs Python 3.12 and [uv](https://docs.astral.sh/uv/)):

```bash
cd backend
uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

Tip (WSL on `/mnt/c`): keep the virtualenv on the Linux filesystem:
`export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/catalogai UV_LINK_MODE=copy`.
