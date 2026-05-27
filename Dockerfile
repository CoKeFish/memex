FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.28 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONPATH=/app/src

WORKDIR /app

# Capa 1: solo manifests → cache de deps mientras el código cambie pero deps no.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Capa 2: el código del servidor (no copiamos memex_local — eso corre en PC).
COPY src/memex /app/src/memex
COPY migrations /app/migrations
COPY alembic.ini /app/alembic.ini

# Entrypoint y comando.
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8787
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "memex.api.app:app", "--host", "0.0.0.0", "--port", "8787"]
