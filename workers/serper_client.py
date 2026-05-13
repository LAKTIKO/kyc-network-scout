from __future__ import annotations

import os
import time
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

_API_KEY: str = os.getenv("SERPER_API_KEY", "")
_DELAY: float = float(os.getenv("SCRAPER_DELAY_SECONDS", "1"))
_ENDPOINT: str = "https://google.serper.dev/search"
_TIMEOUT: int = 15  # seconds per request


def search(query: dict) -> dict:
    """Send one search query to Serper API and return structured results.

    Never raises — all error conditions are captured in the returned dict's
    ``error`` field so the caller can continue processing remaining queries.

    Args:
        query: A single item produced by ``generate_queries()``, containing
               keys ``query``, ``language``, ``type``, ``gl``, ``hl``.

    Returns:
        Dict with keys:
            query        — original query string
            language     — original language tag
            type         — original query type
            credits_used — Serper credits consumed (1 per call on success)
            results      — list of organic result dicts (may be empty)
            error        — None on success, error message string on failure

        Each item in ``results`` has: title, link, snippet, date, position.
    """
    base: dict[str, Any] = {
        "query": query.get("query", ""),
        "language": query.get("language", ""),
        "type": query.get("type", ""),
        "credits_used": 0,
        "results": [],
        "error": None,
    }

    if not _API_KEY:
        base["error"] = "SERPER_API_KEY is not set in environment"
        return base

    try:
        response = requests.post(
            _ENDPOINT,
            headers={
                "X-API-KEY": _API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "q": query["query"],
                "gl": query.get("gl", "us"),
                "hl": query.get("hl", "en"),
            },
            timeout=_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        base["error"] = f"Network error: request timed out after {_TIMEOUT}s"
        return base
    except requests.exceptions.ConnectionError as exc:
        base["error"] = f"Network error: connection failed ({exc})"
        return base
    except requests.exceptions.RequestException as exc:
        base["error"] = f"Network error: {exc}"
        return base
    finally:
        # Rate-limit every outbound call regardless of outcome.
        time.sleep(_DELAY)

    if not response.ok:
        base["error"] = f"HTTP {response.status_code}: {response.reason}"
        return base

    try:
        data: dict[str, Any] = response.json()
    except ValueError:
        base["error"] = "Invalid response format: response is not valid JSON"
        return base

    base["credits_used"] = data.get("credits", 1)

    base["results"] = [
        {
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "snippet": item.get("snippet", ""),
            "date": item.get("date") or None,
            "position": item.get("position", idx + 1),
        }
        for idx, item in enumerate(data.get("organic", []))
    ]

    return base


if __name__ == "__main__":
    import sys
    from pathlib import Path

    # Allow running from project root or from within workers/
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from workers.search_queries import generate_queries

    TEST_NAME = "Іван Петренко"
    TEST_LIMIT = 3

    queries = generate_queries(TEST_NAME)
    print(f"Running first {TEST_LIMIT} of {len(queries)} generated queries for '{TEST_NAME}'\n")

    total_credits = 0

    for i, q in enumerate(queries[:TEST_LIMIT], 1):
        print(f"[{i}/{TEST_LIMIT}] [{q['language']}][{q['type']}] {q['query']!r}")
        result = search(q)

        if result["error"]:
            print(f"  ERROR: {result['error']}")
        else:
            urls = [r["link"] for r in result["results"]]
            first_url = urls[0] if urls else "(no results)"
            print(f"  Found: {len(urls)} URL(s)")
            print(f"  First: {first_url}")
            total_credits += result["credits_used"]

        print()

    print(f"Total credits used: {total_credits}")
