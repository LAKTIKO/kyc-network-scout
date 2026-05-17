# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

**Phase 1 (current)** — university course deliverable (OSINT/AI, advanced level). Implements only the adverse media pipeline (Worker 3). Phase 2 (Opendatabot enrichment, OpenSanctions screening, PostgreSQL, Prometheus/Grafana) comes after the course submission.

## What Phase 1 does

Given a person's full name, the pipeline:
1. Generates trilingual search queries (Ukrainian / Russian / English) combining the name with a risk keyword dictionary
2. Sends queries to **Serper API** to discover adverse media URLs
3. Puts discovered URLs into a **Redis queue**
4. Scrapes each URL with **Crawl4AI** to extract article text
5. Classifies the content with **Anthropic Claude API** (adverse / not adverse, reasoning)
6. Outputs a structured JSON report

## Phase 1 architecture

Two Celery workers, Redis as broker and URL queue, Docker Compose for orchestration:

- **worker-search** — takes a name as input, builds query strings from `config/risk_keywords.yaml`, calls Serper API, pushes result URLs into Redis
- **worker-scraper** — consumes URLs from Redis, runs Crawl4AI to extract text, calls Claude API for classification

Basic observability: success/error counters emitted to logs only (no Prometheus in Phase 1).

Data flow: `data/raw/` stores raw scraped text; `data/normalized/` stores classified output. Neither is committed to git.

## Phase 1 stack

Python · Crawl4AI · Celery · Redis · Anthropic Claude API · Serper API · Docker Compose

## Environment setup

Copy `.env.example` to `.env`:

```
ANTHROPIC_API_KEY=...
SERPER_API_KEY=...
SCRAPER_DELAY_SECONDS=2
SCRAPER_USER_AGENT=kyc-network-scout/1.0 (educational; contact: your-email@example.com)
```

`SCRAPER_DELAY_SECONDS` must be respected between Crawl4AI requests.

## Risk keyword config

`config/risk_keywords.yaml` — trilingual (Ukrainian/Russian/English), with explicit RF/BY indicators (Россия/Беларусь/Russia/Belarus). This file drives query generation in worker-search and should be the only place keywords are defined.

## Future (Phase 2)

After course submission, planned additions:
- **Worker 1** — Opendatabot API (Ukrainian corporate registry: companies, directors, shareholders, beneficiaries)
- **Worker 2** — OpenSanctions bulk dataset screening (OFAC, EU, UK, UN; downloaded separately as `*.json.gz` into `opensanctions/`)
- PostgreSQL for structured persistence
- Prometheus + Grafana for metrics
