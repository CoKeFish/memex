# memex

> *"Consider a future device for individual use, which is a sort of mechanized private file and library. It needs a name, and, to coin one at random, 'memex' will do."*
> — Vannevar Bush, *As We May Think* (1945)

Personal life-data consolidation: many sources, one place. Mail, chats, calendars, bank
statements, documents and photos come in as raw messages; a pipeline turns them into things
you can act on — tasks, events, people, money, "matters" (the threads of your life) — and a
dashboard, a Telegram assistant and a morning summary hand them back to you.

The code is public; **no personal data lives in this repository** (see
[`docs/privacidad.md`](docs/privacidad.md)). Everything you ingest stays in your own Postgres.

## What it does

```
sources ──► inbox ──► relevance gate ──► extraction modules ──► domain stores ──► surfaces
(IMAP, Outlook,        (deterministic     (calendar, tareas,       (Postgres +       (dashboard,
 Telegram, WhatsApp,    rules + LLM)       asuntos, identidades,    graph)            Telegram bot,
 Google Calendar,                          finance, hackathones)                      notifications,
 documents, photos)                                                                   CLIs)
```

- **Ingestion** — pluggable sources with cursors (IMAP/Gmail, Outlook desktop plugin, Telegram,
  WhatsApp, Google Calendar sync, a watched folder, social via Apify). OCR for images and PDFs.
- **Relevance gate** — cheap rules first (allow/block lists, sender tiers), an LLM judge for the
  rest, so most noise never reaches the extractors.
- **Extraction modules**, each with its own prompt and deterministic dedup:
  - `tareas` — what you have to do, with deadlines inferred from the rhythm of each program.
  - `calendar` — events with a commitment level (confirmed / provisional / announcement),
    recurrence, conflicts, Google Calendar write-back.
  - `asuntos` — the ongoing threads of your life (a course, a scholarship, a procedure): each
    message is assigned to one, with milestones, state and priority.
  - `identidades` — people and organizations, deduplicated, with hierarchy and roles.
  - `finance` — movements, subscriptions, categories, cross-source reconciliation.
- **Graph** — everything is a vertex; relations are proposed, confirmed and clustered.
- **Semantic blocking** — local embeddings (fastembed, no network) propose "same thing"
  candidates; an LLM judge decides. Never merges on similarity alone.
- **Surfaces** — a React dashboard, a Telegram assistant that speaks in plain words and asks
  before writing, a notifications daemon (morning summary, reminders), and typed CLIs for
  every module (`memex tareas …`, `memex calendario …`, `memex asuntos …`).
- **Editable prompts** — every system prompt can be inspected and edited from the dashboard,
  with confirmation, versions and reset to code.

Three general principles run through every extractor (`src/memex/modules/principios.py`):
a signal is a task, event or state only if it is addressed to you (or to everyone, as a
requirement); one thing in the world is one record; and a message belongs to its thread.

## Running it

Requirements: Docker (with Compose), Python 3.12 and [`uv`](https://docs.astral.sh/uv/),
Node 20+ only if you want to develop the dashboard.

```bash
git clone https://github.com/CoKeFish/memex.git && cd memex
cp .env.example .env            # read it: every knob is documented in place
docker compose up -d            # postgres, minio, api (:8787), dashboard (:5173)
```

Migrations run automatically when the API container starts. Open `http://localhost:5173`.

Daemons (scheduler, ingest scheduler, Telegram assistant, notifications) are behind Compose
profiles; `COMPOSE_PROFILES` in `.env` chooses which ones start. LLM providers are configured
per consumer from the dashboard (`/procesamiento`) or with `memex-llm settings set`; keys
(`DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OCR_API_KEY`) come from the
environment — a plain `.env` works; the maintainers use Doppler (`scripts/memex_up.ps1`).

For local development without containers for the app itself:

```bash
uv sync
docker compose up -d postgres minio
uv run alembic upgrade head
uv run uvicorn memex.api.app:app --reload --port 8787
cd frontend && npm install && npm run dev
```

Tests need the `postgres-test` service (`docker compose up -d postgres-test`), then
`uv run pytest`. Lint and types: `uv run ruff check src tests && uv run mypy src tests`.
Pre-commit hooks enforce all of it plus a secrets scan and a personal-data guard.

## Where to read next

- `docs/` — the flow end to end (`arquitectura-flujo.md`, `procesamiento-automatico.md`), one
  document per module (`modulo-tareas.md`, `modulo-eventos.md`, `modulo-asuntos.md`,
  `modulo-identidades.md`, `modulo-finanzas.md`, `modulo-relevancia.md`), the general
  principles (`principios-generales.md`), notifications, prompts, semantic blocking, the
  Telegram assistant, and runbooks. Written in Spanish.
- `docs/privacidad.md` — how this repository stays free of personal data, and the guard that
  keeps it that way.

## Status

Used daily by its author; alpha in the sense that the extraction quality is still being
tuned case by case, in general rules, never with the author's data in the tree. Issues and
questions are welcome.

## License

TBD (likely MIT for the code). Data was never in scope.
