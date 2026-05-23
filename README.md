# memex

> *"Consider a future device for individual use, which is a sort of mechanized private file and library. It needs a name, and, to coin one at random, 'memex' will do."*
> — Vannevar Bush, *As We May Think* (Atlantic Monthly, 1945)

Personal life-data consolidation. Many sources, one place. A working answer
to the question Bush asked 80 years ago, with the tools of today.

## What this is

A pipeline + store + views for signals from across my life — coding,
movement, sleep, calendar, messaging, finance, reading, anything that
produces a trace. Sources push or get pulled, normalized data lands in a
unified store, queries and dashboards expose patterns I couldn't see while
the data was scattered.

## What this is not

Not a product. Not for anyone else. Not a place where my actual data
lives.

## Code public, data private

This repository is public because the framework, schemas and integrations
are worth sharing. **No personal data ever lands here.** Raw signals,
intermediate parquet/sqlite, exports, dumps, env files — all live outside
the repo, in private storage. The `.gitignore` reflects that boundary
aggressively: `data/`, `*.db`, `*.parquet`, `*.csv`, `.env*` and friends
are blocked. If something sensitive ever slips in, treat it as a leak
(rotate, force-push history rewrite, the works).

## Position in the constellation

This repo doesn't stand alone. It is the layer that aggregates and views;
the sources and the runtime live elsewhere.

| Repo | Role |
|---|---|
| [`ingestors`](https://github.com/CoKeFish/ingestors) | Pull data from sources (Telegram, mail, etc.) |
| [`personal-api`](https://github.com/CoKeFish/personal-api) | Jobs, routers, processors — runtime orchestration |
| [`openclaw`](https://github.com/CoKeFish/openclaw) | The agent that asks questions of this data |
| **`memex`** *(this repo)* | Unified store and views over everything above |

## Status

Early. Currently designing the schema of "events" that unifies signals
from heterogeneous sources, and choosing the storage backend. No code yet
worth pulling. Open an issue if you somehow ended up here and want to
talk about quantified-self pipelines.

## License

TBD. Probably MIT for the code; data was never in scope.
