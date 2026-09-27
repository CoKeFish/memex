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
#
# `codex-code-mode-host` va JUNTO al binario principal: sin él, `-c web_search_mode=live` NO busca.
# codex avisa «failed to spawn code-mode host» por stderr y contesta igual, de memoria, así que el
# fallo es SILENCIOSO para quien lee solo la respuesta. Los curadores de asuntos e identidades
# corrían con `web_search=True` creyendo que miraban la web (probado el 2026-09-15: no la miraba).
ARG CODEX_VERSION=0.153.4
# libmagic1: lo necesita `python-magic` (dependencia de neonize, el cliente de WhatsApp).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates libmagic1 \
    && curl -fsSL -o /tmp/codex.tar.gz \
       "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-x86_64-unknown-linux-musl.tar.gz" \
    && tar -xzf /tmp/codex.tar.gz -C /tmp \
    && install -m 755 /tmp/codex-x86_64-unknown-linux-musl /usr/local/bin/codex \
    && curl -fsSL -o /tmp/codex-cmh.tar.gz \
       "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz" \
    && tar -xzf /tmp/codex-cmh.tar.gz -C /tmp \
    && install -m 755 /tmp/codex-code-mode-host-x86_64-unknown-linux-musl \
       /usr/local/bin/codex-code-mode-host \
    && rm -f /tmp/codex.tar.gz /tmp/codex-x86_64-unknown-linux-musl \
       /tmp/codex-cmh.tar.gz /tmp/codex-code-mode-host-x86_64-unknown-linux-musl \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Capa 1: solo manifests → cache de deps mientras el código cambie pero deps no.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Modelo de embeddings del bloqueo semántico (memex/semantic), HORNEADO en la imagen: fastembed lo
# descarga en build y en runtime no toca la red. Capa propia, después de las deps: se cachea
# mientras no cambie el modelo. Cambiar de modelo = cambiar MEMEX_SEMANTIC_MODEL Y esta línea.
ENV FASTEMBED_CACHE_PATH=/app/models
RUN .venv/bin/python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"

# Capa 2: el código del servidor (no copiamos memex_local — eso corre en PC).
COPY src/memex /app/src/memex
COPY migrations /app/migrations
COPY alembic.ini /app/alembic.ini

# Versión del código que corre (sello `code_version` por mensaje/etapa y `GET /version`). Llega como
# build-arg desde el host (`scripts/build_api.ps1` la calcula con git); `.git` no entra al contexto,
# así que sin el arg queda «unknown» — visible en la UI a propósito.
ARG MEMEX_CODE_VERSION=unknown
ENV MEMEX_CODE_VERSION=${MEMEX_CODE_VERSION}

# Entrypoint y comando.
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8787
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "memex.api.app:app", "--host", "0.0.0.0", "--port", "8787"]
