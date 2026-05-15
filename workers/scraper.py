from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv()

_USER_AGENT: str = os.getenv(
    "SCRAPER_USER_AGENT",
    "kyc-network-scout/1.0 (educational)",
)
PAGE_TIMEOUT_MS: int = 30_000
MAX_TEXT_CHARS: int = 50_000


def _extract_markdown(result: Any) -> str:
    """Extract markdown string from a Crawl4AI result.

    Handles crawl4ai 0.3.x (result.markdown is a str) and
    0.8.x (result.markdown is a MarkdownGenerationResult with fit_markdown /
    raw_markdown). fit_markdown is preferred — it strips nav/header/footer noise
    via PruningContentFilter; raw_markdown is the fallback.
    """
    md = getattr(result, "markdown", None)
    if md is None:
        return ""
    if isinstance(md, str):
        return md
    # 0.4.x+ MarkdownGenerationResult
    fit = getattr(md, "fit_markdown", None)
    if fit:
        return fit
    return getattr(md, "raw_markdown", None) or str(md)


async def scrape(url: str) -> dict[str, Any]:
    """Scrape a single article URL with Crawl4AI and return clean markdown text.

    Never raises — all failure modes are reported through the ``error`` field
    so the pipeline can continue processing other URLs.

    Args:
        url: Full URL of the article to scrape.

    Returns:
        Dict with keys:
            url              — original URL as passed in
            title            — page title extracted from metadata (may be "")
            markdown         — article text in markdown, capped at MAX_TEXT_CHARS
            html_length      — raw HTML size in chars (for logging/debug)
            markdown_length  — length of the returned markdown string
            scraped_at       — ISO 8601 UTC timestamp of when scraping started
            error            — None on success, descriptive string on failure
    """
    base: dict[str, Any] = {
        "url": url,
        "title": "",
        "markdown": "",
        "html_length": 0,
        "markdown_length": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }

    try:
        from crawl4ai import AsyncWebCrawler
    except ImportError:
        base["error"] = (
            "crawl4ai not installed — "
            "run: pip install crawl4ai && playwright install chromium"
        )
        return base

    # Build config objects for 0.4.x+; fall back to inline kwargs for 0.3.x.
    crawler_kwargs: dict[str, Any] = {"verbose": False}
    run_kwargs: dict[str, Any] = {"url": url}

    try:
        from crawl4ai import BrowserConfig, CrawlerRunConfig

        # Try to attach PruningContentFilter for cleaner article extraction.
        # Strips navigation, ads, share-buttons before markdown conversion.
        try:
            from crawl4ai.content_filter_strategy import PruningContentFilter
            from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

            markdown_generator = DefaultMarkdownGenerator(
                content_filter=PruningContentFilter(
                    threshold=0.48,
                    threshold_type="fixed",
                    min_word_threshold=10,
                )
            )
        except ImportError:
            markdown_generator = None

        crawler_kwargs = {
            "config": BrowserConfig(headless=True, user_agent=_USER_AGENT)
        }
        run_config_kwargs: dict[str, Any] = {
            "page_timeout": PAGE_TIMEOUT_MS,
            "word_count_threshold": 10,
            "wait_for_images": False,
        }
        if markdown_generator is not None:
            run_config_kwargs["markdown_generator"] = markdown_generator

        run_kwargs["config"] = CrawlerRunConfig(**run_config_kwargs)

    except ImportError:
        # crawl4ai 0.3.x — pass params directly to arun()
        run_kwargs.update(
            page_timeout=PAGE_TIMEOUT_MS,
            word_count_threshold=10,
        )

    try:
        async with AsyncWebCrawler(**crawler_kwargs) as crawler:
            result = await crawler.arun(**run_kwargs)
    except Exception as exc:
        msg = str(exc)
        if "timeout" in msg.lower():
            base["error"] = f"Page timeout after {PAGE_TIMEOUT_MS // 1000}s"
        elif any(w in msg.lower() for w in ("network", "connection", "dns", "refused")):
            base["error"] = f"Network error: {msg}"
        else:
            base["error"] = f"Scraper error: {msg}"
        return base

    if not result.success:
        base["error"] = (
            getattr(result, "error_message", None) or "Crawl returned success=False"
        )
        return base

    html: str = getattr(result, "html", "") or ""
    metadata: dict = getattr(result, "metadata", {}) or {}
    title: str = metadata.get("title", "") if isinstance(metadata, dict) else ""
    markdown: str = _extract_markdown(result)[:MAX_TEXT_CHARS]

    base.update(
        {
            "title": title,
            "markdown": markdown,
            "html_length": len(html),
            "markdown_length": len(markdown),
        }
    )
    return base


if __name__ == "__main__":
    import sys

    test_url = sys.argv[1] if len(sys.argv) > 1 else "https://www.bbc.com/ukrainian"

    print(f"Scraping: {test_url}\n")
    data = asyncio.run(scrape(test_url))

    if data["error"]:
        print(f"ERROR: {data['error']}")
        sys.exit(1)

    print(f"Title           : {data['title']}")
    print(f"HTML length     : {data['html_length']} chars")
    print(f"Markdown length : {data['markdown_length']} chars")
    print(f"Scraped at      : {data['scraped_at']}")

    # Find first H1 heading (article title in markdown) and print 2000 chars after it.
    # Falls back to chars 1500–3500 if no heading found — typically past the nav block.
    md = data["markdown"]
    lines = md.splitlines()
    h1_pos = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
    if h1_pos is not None:
        body_start = sum(len(l) + 1 for l in lines[:h1_pos])
        snippet = md[body_start: body_start + 2000]
        print(f"\n--- 2000 chars from first H1 (line {h1_pos + 1}) ---")
    else:
        snippet = md[1500:3500]
        print("\n--- chars 1500–3500 (no H1 heading found) ---")

    print(snippet)
    print("...")
