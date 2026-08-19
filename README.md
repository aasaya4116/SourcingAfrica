# SourcingAfrica — African Tech Intelligence Pipeline

An automated intelligence system that tracks African technology and macro trends using the ADE framework (Automation, Discovery, Emergence). Aggregates news from across the open web, deduplicates, enriches each story with Claude, and serves it as a browsable feed plus on-demand Q&A.

---

## The Problem

African tech ecosystems — from Lagos to Nairobi to Cairo — generate significant signal, but that signal is fragmented across dozens of publications with no centralized, intelligent aggregation layer. Staying current requires manual effort that doesn't scale.

---

## Architecture

```
RSS feeds + GDELT  →  Ingestor  →  Supabase (Postgres)  →  Claude enrichment  →  FastAPI + PWA
```

**Stage 1 — Ingestor** (`ingestor/ingestor.py`)
Polls a configurable set of African tech/business RSS feeds plus the GDELT global news index every few hours. Deduplicates by URL / entry ID and stores raw articles in Supabase.

**Stage 2 — Enrichment** (`backend/qa.py`)
Claude summarises each full-text article (summary, takeaways, "so what"), tags it by country and topic, and splits long newsletter digests into individual stories. GDELT headlines are tagged by source country for free (no model call) and shown as link-outs.

**Stage 3 — Serve** (`backend/app.py` + `frontend/`)
A FastAPI backend serves a PWA: a deduped daily feed, article detail, a Claude-curated top 5, and natural-language Q&A across the archive. Every endpoint sits behind Supabase Auth with per-user rate limits, so the deployment's Anthropic key is never spendable by whoever has the URL.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Ingestion | Python 3 · feedparser · GDELT DOC 2.0 API |
| Database | Supabase PostgreSQL (`psycopg2`) |
| AI Analysis | Anthropic Claude API |
| Backend | FastAPI · uvicorn · APScheduler |
| Frontend | PWA — HTML · CSS · JavaScript |
| Infrastructure | Docker · Railway |

---

## Key Features

- Automated polling of RSS + GDELT with URL-based deduplication
- Per-article Claude summaries, takeaways, and ADE-framework tagging
- Country/topic tagging (free from GDELT metadata where available)
- Claude-curated daily top 5, plus Q&A over the archive with live web search
- Headline relevance filtering on GDELT, so syndicated wire noise stays out of the feed
- Every story links back to the outlet that reported it
- Magic-link sign-in with a tester allowlist and per-user rate limits

---

## Setup

**Prerequisites:** Python 3, Anthropic API key, a Supabase project

```bash
git clone https://github.com/aasaya4116/SourcingAfrica.git
cd SourcingAfrica

uv venv && uv pip install -r requirements.txt

cp .env.example .env
# Fill in ANTHROPIC_API_KEY, DATABASE_URL, and the Supabase auth vars

# Run the API + PWA. The ingestor runs inside it on a schedule.
uvicorn backend.app:app --reload
```

For local work without setting up sign-in, run with `AUTH_DISABLED=1`. The app
refuses to start unauthenticated any other way — an open deployment holding an
Anthropic key is the one failure mode worth making impossible.

Edit `config.json` to add/remove RSS feeds or tune the GDELT queries. Dead feeds
fail gracefully per-source; a *malformed* GDELT query raises, because a query
that can never succeed is a bug rather than a bad cycle.

**Useful env flags**

| Var | Effect |
|---|---|
| `AUTH_DISABLED=1` | Skip sign-in. Local development only. |
| `RUN_INGESTOR=0` | Don't schedule ingestion in this process. |
| `ENRICH_ON_INGEST=0` | Ingest without model calls; the capped startup backfill catches up. |
| `ASK_LIMIT_PER_HOUR` | Per-user question ceiling (default 30). |

**Maintenance**

```bash
python scripts/prune_offtopic.py           # dry run
python scripts/prune_offtopic.py --apply   # remove off-topic GDELT headlines
```

### Deploy (Railway)

Set `ANTHROPIC_API_KEY`, `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_ANON_KEY` and
`ALLOWED_EMAILS` in Railway → Variables. Railway builds from the `Dockerfile` and
runs a single process: uvicorn, with ingestion scheduled inside it. That matters —
the ingestor used to run as a backgrounded process next to uvicorn, so when it
died the platform saw a healthy container and the archive went stale unnoticed.

All persistence lives in Supabase — nothing is written to the container filesystem.

---

## Why I Built This

Built to solve a personal research problem — staying current on African tech ecosystems without manual aggregation. The ADE framework (Automation, Discovery, Emergence) emerged from thinking about how intelligence pipelines should progressively surface signal from noise.
