from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

from workers.classifier import classify
from workers.scraper import scrape
from workers.search_queries import generate_queries
from workers.serper_client import search

_BLOCKED_DOMAINS: frozenset[str] = frozenset({
    "facebook.com", "twitter.com", "x.com", "linkedin.com",
    "instagram.com", "t.me", "youtube.com",
    "opendatabot.ua", "youcontrol.com.ua", "ring.org.ua",
})

_SEVERITY_RANK: dict[str, int] = {
    "critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4,
}

_COST_PER_1M_TOKENS: float = 3.0  # rough estimate; most tokens are input

_SCRAPER_DELAY: float = float(os.getenv("SCRAPER_DELAY_SECONDS", "2"))

# Multi-char mappings must come before their sub-strings (щ before ш, etc.)
_CYR_TO_LAT: list[tuple[str, str]] = [
    ("щ", "shch"), ("ш", "sh"), ("ч", "ch"), ("ж", "zh"), ("ц", "ts"),
    ("ю", "yu"),   ("я", "ya"), ("є", "ye"), ("ї", "yi"), ("ё", "yo"),
    ("а", "a"),    ("б", "b"),  ("в", "v"),  ("г", "h"),  ("ґ", "g"),
    ("д", "d"),    ("е", "e"),  ("з", "z"),  ("и", "y"),  ("і", "i"),
    ("й", "y"),    ("к", "k"),  ("л", "l"),  ("м", "m"),  ("н", "n"),
    ("о", "o"),    ("п", "p"),  ("р", "r"),  ("с", "s"),  ("т", "t"),
    ("у", "u"),    ("ф", "f"),  ("х", "kh"), ("ь", ""),   ("ъ", ""),
    ("ы", "y"),    ("э", "e"),
]


def _slugify(name: str) -> str:
    result = name.lower()
    for cyr, lat in _CYR_TO_LAT:
        result = result.replace(cyr, lat)
    result = re.sub(r"[\s\-]+", "_", result)
    result = re.sub(r"[^\w]", "", result)
    return re.sub(r"_+", "_", result).strip("_")


def _url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _is_blocked(url: str) -> bool:
    try:
        netloc = urlparse(url).netloc.lstrip("www.")
        return any(blocked in netloc for blocked in _BLOCKED_DOMAINS)
    except Exception:
        return False


async def _scrape_all(
    urls: list[str],
    raw_dir: Path,
    delay: float,
) -> list[dict[str, Any]]:
    articles: list[dict[str, Any]] = []
    total = len(urls)
    for i, url in enumerate(urls, 1):
        print(f"  [{i}/{total}] Scraping: {url}", flush=True)
        article = await scrape(url)
        if article["error"]:
            print(f"    ERROR: {article['error']}", flush=True)
        else:
            (raw_dir / f"{_url_hash(url)}.md").write_text(
                article["markdown"], encoding="utf-8"
            )
            print(f"    → {article['markdown_length']} chars", flush=True)
        articles.append(article)
        if i < total:
            await asyncio.sleep(delay)
    return articles


def _print_report(
    *,
    person_name: str,
    queries_sent: int,
    urls_found: int,
    urls_after_dedup: int,
    urls_processed: int,
    scrape_errors: int,
    classify_errors: int,
    total_credits: int,
    total_tokens: int,
    classified: list[dict[str, Any]],
    norm_dir: Path,
    interrupted: bool = False,
) -> None:
    sep = "=" * 44
    cost = total_tokens * _COST_PER_1M_TOKENS / 1_000_000

    adverse = sorted(
        [
            r for r in classified
            if r.get("is_adverse") and r.get("is_about_target_person")
        ],
        key=lambda r: _SEVERITY_RANK.get(r.get("severity", "none"), 4),
    )
    non_adverse = [
        r for r in classified
        if not (r.get("is_adverse") and r.get("is_about_target_person"))
    ]

    print(f"\n{sep}")
    print("KYC ADVERSE MEDIA REPORT" + (" (INTERRUPTED)" if interrupted else ""))
    print(sep)
    print(f"Person:           {person_name}")
    print(f"Queries sent:     {queries_sent}")
    print(f"URLs found:       {urls_found} (after dedup: {urls_after_dedup})")
    print(f"URLs processed:   {urls_processed}")
    print(f"Scrape errors:    {scrape_errors}")
    print(f"Classify errors:  {classify_errors}")
    print(f"Total Serper credits used: {total_credits}")
    print(f"Total Claude tokens used:  {total_tokens} (~${cost:.2f})")

    if not classified:
        print("\nNo articles classified.")
    else:
        print(f"\nADVERSE MEDIA FOUND: {len(adverse)} of {len(classified)} articles")

        counter = 0
        for r in adverse:
            counter += 1
            print(
                f"\n[{counter}] {r.get('severity', '?').upper()} "
                f"| {r.get('category', '?')} "
                f"| match={r.get('match_confidence', '?')}"
            )
            print(f"    URL: {r.get('url', '')}")
            print(f"    Title: {r.get('title', '')}")
            print(f"    Summary: {r.get('summary', '')}")
            quotes = r.get("key_quotes") or []
            if quotes:
                print("    Quotes:")
                for q in quotes:
                    print(f"      « {q} »")

        if non_adverse:
            print("\nNOT ADVERSE (or different person):")
            for r in non_adverse:
                counter += 1
                if r.get("error"):
                    label = f"error — {r['error']}"
                elif not r.get("is_about_target_person"):
                    label = (
                        f"not_match — "
                        f"{r.get('summary') or r.get('title', '')}"
                    )
                else:
                    label = (
                        f"is_adverse=false — "
                        f"{r.get('summary') or r.get('title', '')}"
                    )
                print(f"[{counter}] {label}")

    print(f"\n{sep}")
    print(f"Results saved to: {norm_dir}/")
    print(sep)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "KYC adverse media pipeline: "
            "name → search → scrape → classify → report"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("person_name", help="Full name of the person to investigate")
    parser.add_argument(
        "--max-queries", type=int, default=5, metavar="N",
        help="Number of search queries to execute",
    )
    parser.add_argument(
        "--max-urls", type=int, default=5, metavar="N",
        help="Maximum unique URLs to scrape and classify",
    )
    parser.add_argument(
        "--output-dir", default="data", metavar="PATH",
        help="Base output directory",
    )
    return parser.parse_args()


def run_adverse_media(
    subject_name: str,
    slug: str,
    max_queries: int = 5,
    max_urls: int = 5,
    output_dir: str = "data",
) -> dict[str, Any]:
    """Adverse-media гілка для оркестратора (синхронна обгортка).

    На відміну від main(), приймає slug ЗЗОВНІ — щоб медіа-результати лягли
    в ту саму теку, що registry.json/sanctions.json (єдиний ключ для
    aggregate). Повертає summary, не друкує. Чесна деградація: помилки
    окремих кроків не валять функцію.
    """
    output_base = Path(output_dir)
    raw_dir = output_base / "raw" / slug
    norm_dir = output_base / "normalized" / slug
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(norm_dir, exist_ok=True)

    summary: dict[str, Any] = {
        "queries_sent": 0, "urls_found": 0, "urls_processed": 0,
        "adverse_found": 0, "classified": 0, "credits_used": 0,
        "tokens_used": 0, "error": None,
    }

    try:
        queries = generate_queries(subject_name)[:max_queries]
        summary["queries_sent"] = len(queries)

        all_results: list[dict[str, Any]] = []
        for q in queries:
            sr = search(q)
            if sr.get("error"):
                continue
            all_results.extend(sr["results"])
            summary["credits_used"] += sr.get("credits_used", 0)
        summary["urls_found"] = len(all_results)

        seen: set[str] = set()
        filtered: list[str] = []
        for r in all_results:
            url = r.get("link", "").strip()
            if url and url not in seen:
                seen.add(url)
                if not _is_blocked(url):
                    filtered.append(url)
        urls = filtered[:max_urls]
        summary["urls_processed"] = len(urls)

        # кеш vs нове
        cached: list[dict[str, Any]] = []
        to_scrape: list[str] = []
        for url in urls:
            nf = norm_dir / f"{_url_hash(url)}.json"
            if nf.exists():
                try:
                    cached.append(json.loads(nf.read_text(encoding="utf-8")))
                except Exception:
                    to_scrape.append(url)
            else:
                to_scrape.append(url)

        articles: list[dict[str, Any]] = []
        if to_scrape:
            articles = asyncio.run(_scrape_all(to_scrape, raw_dir, _SCRAPER_DELAY))

        classified = list(cached)
        for article in articles:
            if article.get("error"):
                continue
            res = classify(article, subject_name)
            summary["tokens_used"] += res.get("tokens_used", 0)
            nf = norm_dir / f"{_url_hash(article['url'])}.json"
            nf.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                          encoding="utf-8")
            classified.append(res)

        summary["classified"] = len(classified)
        summary["adverse_found"] = sum(
            1 for r in classified
            if r.get("is_adverse") and r.get("is_about_target_person"))

    except Exception as exc:
        summary["error"] = str(exc)

    return summary


def main() -> None:
    args = _parse_args()
    person_name: str = args.person_name
    max_queries: int = args.max_queries
    max_urls: int = args.max_urls
    output_base = Path(args.output_dir)

    slug = _slugify(person_name)
    raw_dir = output_base / "raw" / slug
    norm_dir = output_base / "normalized" / slug
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(norm_dir, exist_ok=True)

    # ── Step 1: Generate queries ──────────────────────────────────────────
    print(f"\nPipeline start: {person_name!r}")
    all_queries = generate_queries(person_name)
    queries = all_queries[:max_queries]
    print(f"Generated {len(all_queries)} queries, using top {len(queries)}")

    # ── Step 2: Search via Serper ─────────────────────────────────────────
    print("\n[Step 2] Searching Serper API...")
    all_results: list[dict[str, Any]] = []
    total_credits = 0

    try:
        for i, q in enumerate(queries, 1):
            sr = search(q)
            if sr["error"]:
                print(f"  [{i}/{len(queries)}] ERROR: {sr['error']}")
                continue
            n = len(sr["results"])
            print(f"  [{i}/{len(queries)}] Searching: {q['query']!r} → found {n} URLs")
            all_results.extend(sr["results"])
            total_credits += sr["credits_used"]
    except KeyboardInterrupt:
        print("\n  Interrupted during search — continuing with collected results.")

    # ── Step 3: Dedup and filter ──────────────────────────────────────────
    print("\n[Step 3] Deduplicating and filtering URLs...")
    seen: set[str] = set()
    filtered_urls: list[str] = []

    for r in all_results:
        url = r.get("link", "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        if not _is_blocked(url):
            filtered_urls.append(url)

    print(
        f"Found {len(all_results)} results, {len(seen)} unique URLs, "
        f"filtered to {len(filtered_urls)} after removing blocked domains"
    )

    urls_to_process = filtered_urls[:max_urls]
    print(f"Using top {len(urls_to_process)} URLs")

    # Separate cached from URLs that still need scraping
    cached_results: list[dict[str, Any]] = []
    urls_to_scrape: list[str] = []

    for url in urls_to_process:
        norm_file = norm_dir / f"{_url_hash(url)}.json"
        if norm_file.exists():
            print(f"  Skipping cached: {url}")
            cached_results.append(json.loads(norm_file.read_text(encoding="utf-8")))
        else:
            urls_to_scrape.append(url)

    # ── Step 4: Scraping ──────────────────────────────────────────────────
    articles: list[dict[str, Any]] = []

    if urls_to_scrape:
        print(f"\n[Step 4] Scraping {len(urls_to_scrape)} URLs...")
        try:
            articles = asyncio.run(
                _scrape_all(urls_to_scrape, raw_dir, _SCRAPER_DELAY)
            )
        except KeyboardInterrupt:
            print("\n  Interrupted during scraping.")
    else:
        print("\n[Step 4] All URLs cached — skipping scraping.")

    scrape_errors = sum(1 for a in articles if a.get("error"))

    # ── Step 5: Classification ────────────────────────────────────────────
    new_results: list[dict[str, Any]] = []
    total_tokens = sum(r.get("tokens_used", 0) for r in cached_results)
    classify_errors = 0
    interrupted = False

    if articles:
        print(f"\n[Step 5] Classifying {len(articles)} articles...")
        try:
            for i, article in enumerate(articles, 1):
                result = classify(article, person_name)
                total_tokens += result.get("tokens_used", 0)

                norm_file = norm_dir / f"{_url_hash(article['url'])}.json"
                norm_file.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                new_results.append(result)

                if result.get("error"):
                    classify_errors += 1
                    print(f"  [{i}/{len(articles)}] ERROR: {result['error']}")
                else:
                    print(
                        f"  [{i}/{len(articles)}] Classifying: {article['url']}\n"
                        f"    → is_adverse={result['is_adverse']}, "
                        f"severity={result['severity']}, "
                        f"category={result['category']}"
                    )
        except KeyboardInterrupt:
            interrupted = True
            print("\n  Interrupted during classification — saving partial results.")
    else:
        print("\n[Step 5] No new articles to classify.")

    # ── Step 6: Report ────────────────────────────────────────────────────
    _print_report(
        person_name=person_name,
        queries_sent=len(queries),
        urls_found=len(all_results),
        urls_after_dedup=len(filtered_urls),
        urls_processed=len(urls_to_process),
        scrape_errors=scrape_errors,
        classify_errors=classify_errors,
        total_credits=total_credits,
        total_tokens=total_tokens,
        classified=cached_results + new_results,
        norm_dir=norm_dir,
        interrupted=interrupted,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted before pipeline could start.")
        sys.exit(130)
