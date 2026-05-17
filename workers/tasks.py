from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workers.celery_app import app
from workers.classifier import classify
from workers.scraper import scrape
from workers.search_queries import generate_queries
from workers.serper_client import search
from workers.run_pipeline import _is_blocked, _slugify, _url_hash

logger = logging.getLogger(__name__)

_OUTPUT_BASE = Path(os.getenv("OUTPUT_DIR", "data"))


@app.task(name="workers.tasks.search_task")
def search_task(person_name: str, max_queries: int = 5) -> dict[str, Any]:
    """Generate queries, search Serper, queue a scrape+classify task per URL."""
    try:
        queries = generate_queries(person_name)[:max_queries]
        logger.info("search_task: %r — %d queries", person_name, len(queries))

        seen: set[str] = set()
        all_urls: list[str] = []
        total_credits = 0
        queries_run = 0

        for q in queries:
            result = search(q)
            if result["error"]:
                logger.warning("search_task query error: %s", result["error"])
                continue
            queries_run += 1
            total_credits += result["credits_used"]
            for r in result["results"]:
                url = r.get("link", "").strip()
                if url and url not in seen and not _is_blocked(url):
                    seen.add(url)
                    all_urls.append(url)

        urls_queued = 0
        for url in all_urls:
            scrape_and_classify_task.apply_async(args=[url, person_name])
            urls_queued += 1

        logger.info(
            "search_task: %r — queued %d URLs", person_name, urls_queued
        )
        return {
            "person_name": person_name,
            "queries_run": queries_run,
            "urls_found": len(all_urls),
            "urls_queued": urls_queued,
            "credits_used": total_credits,
            "error": None,
        }

    except Exception as exc:
        logger.exception("search_task failed for %r", person_name)
        return {
            "person_name": person_name,
            "queries_run": 0,
            "urls_found": 0,
            "urls_queued": 0,
            "credits_used": 0,
            "error": f"search_task error: {exc}",
        }


@app.task(name="workers.tasks.scrape_and_classify_task")
def scrape_and_classify_task(url: str, person_name: str) -> dict[str, Any]:
    """Scrape one URL and classify it; idempotent (skips if result cached)."""
    base: dict[str, Any] = {
        "url": url,
        "is_adverse": False,
        "severity": "none",
        "category": "other",
        "error": None,
    }

    try:
        slug = _slugify(person_name)
        url_hash = _url_hash(url)
        norm_dir = _OUTPUT_BASE / "normalized" / slug
        norm_file = norm_dir / f"{url_hash}.json"

        if norm_file.exists():
            logger.info("scrape_and_classify_task: cached %s", url)
            cached = json.loads(norm_file.read_text(encoding="utf-8"))
            return {
                "url": url,
                "is_adverse": cached.get("is_adverse", False),
                "severity": cached.get("severity", "none"),
                "category": cached.get("category", "other"),
                "error": None,
            }

        raw_dir = _OUTPUT_BASE / "raw" / slug
        os.makedirs(norm_dir, exist_ok=True)
        os.makedirs(raw_dir, exist_ok=True)

        article = asyncio.run(scrape(url))
        logger.info(
            "scrape_and_classify_task: scraped %s — %d chars, error=%s",
            url, article.get("markdown_length", 0), article.get("error"),
        )

        if not article.get("error"):
            (raw_dir / f"{url_hash}.md").write_text(
                article["markdown"], encoding="utf-8"
            )

        result = classify(article, person_name)
        logger.info(
            "scrape_and_classify_task: classified %s — is_adverse=%s severity=%s",
            url, result.get("is_adverse"), result.get("severity"),
        )

        norm_file.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return {
            "url": url,
            "is_adverse": result.get("is_adverse", False),
            "severity": result.get("severity", "none"),
            "category": result.get("category", "other"),
            "error": result.get("error"),
        }

    except Exception as exc:
        logger.exception("scrape_and_classify_task failed for %s", url)
        base["error"] = f"Task error: {exc}"
        return base


@app.task(name="workers.tasks.heartbeat")
def heartbeat() -> dict[str, str]:
    """Periodic health-check task — executed by Celery Beat every 10 minutes."""
    ts = datetime.now(timezone.utc).isoformat()
    logger.info("Heartbeat at %s", ts)
    return {"timestamp": ts}
