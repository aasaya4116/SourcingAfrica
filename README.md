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
A FastAPI backend serves a PWA: a deduped daily feed, article detail, a Claude-curated top 5, and natural-language Q&A across the archive.

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
- Claude-curated daily top 5 and natural-language Q&A over the archive
- Fully shareable — no personal inbox or OAuth in the loop

---

## Setup

**Prerequisites:** Python 3, Anthropic API key, a Supabase project

```bash
git clone https://github.com/aasaya4116/SourcingAfrica.git
cd SourcingAfrica

pip install -r requirements.txt

cp .env.example .env
# Fill in ANTHROPIC_API_KEY and DATABASE_URL (Supabase → Connect → Direct, port 5432)

# Run the ingestor (polls sources, writes to Supabase)
python ingestor/ingestor.py

# Run the API + PWA
uvicorn backend.app:app --reload
```

Edit `config.json` to add/remove RSS feeds or tune the GDELT queries. Dead feeds fail gracefully per-source.

### Deploy (Railway)

Set `ANTHROPIC_API_KEY` and `DATABASE_URL` in Railway → Variables. Railway builds from the `Dockerfile` and runs both the ingestor and the web server. All persistence lives in Supabase — nothing is written to the container filesystem.

---

## Why I Built This

Built to solve a personal research problem — staying current on African tech ecosystems without manual aggregation. The ADE framework (Automation, Discovery, Emergence) emerged from thinking about how intelligence pipelines should progressively surface signal from noise.
