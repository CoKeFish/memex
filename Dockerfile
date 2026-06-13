FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.28 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONPATH=/app/src

WORKDIR /app

# Codex CLI (proveedor LLM 'codex' del gate de relevancia): binario musl estático del release
# oficial. La SESIÓN no vive en la imagen: CODEX_HOME apunta al mount de ./secrets (compose),
# donde el dueño copia su auth.json (`codex login` hecho en el host). Capa temprana → cachea.
ARG CODEX_VERSION=0.128.0
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL -o /tmp/codex.tar.gz \
       "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-x86_64-unknown-linux-musl.tar.gz" \
    && tar -xzf /tmp/codex.tar.gz -C /tmp \
    && install -m 755 /tmp/codex-x86_64-unknown-linux-musl /usr/local/bin/codex \
    && rm -f /tmp/codex.tar.gz /tmp/codex-x86_64-unknown-linux-musl \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

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
