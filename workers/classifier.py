from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

MODEL: str = "claude-sonnet-4-6"
_MAX_TOKENS: int = 1024
_ARTICLE_CHAR_LIMIT: int = 30_000
_MIN_ARTICLE_CHARS: int = 100

_SYSTEM_PROMPT = (
    "You are a KYC adverse media classifier. Your task is to analyze articles and "
    "determine if they contain adverse information about a specific person. "
    "Be strict about identity matching — same name does not mean same person. "
    "Provide structured JSON output only, no preamble."
)


def _build_user_message(
    article: dict[str, Any],
    person_name: str,
    identifying_hints: dict[str, Any] | None,
) -> str:
    if identifying_hints:
        hints_lines = [f"  {k}: {v}" for k, v in identifying_hints.items()]
        hints_block = "ADDITIONAL IDENTIFYING INFO:\n" + "\n".join(hints_lines) + "\n"
    else:
        hints_block = ""

    return f"""Analyze this article for adverse media classification.

TARGET PERSON: {person_name}
{hints_block}
ARTICLE URL: {article['url']}
ARTICLE TITLE: {article['title']}

ARTICLE TEXT:
{article['markdown'][:_ARTICLE_CHAR_LIMIT]}

Return ONLY a valid JSON object with this exact structure:
{{
  "is_about_target_person": boolean,
  "match_confidence": "high" | "medium" | "low" | "not_match",
  "match_evidence": [list of 1-3 specific reasons in Ukrainian why you think this person is/isn't the target],
  "is_adverse": boolean,
  "severity": "critical" | "high" | "medium" | "low" | "none",
  "category": "sanctions" | "corruption" | "criminal" | "litigation" | "tax" | "fraud" | "other",
  "summary": "1-2 sentence summary in Ukrainian",
  "key_quotes": [list of 1-3 short direct quotes from article, each under 15 words, in original language]
}}

Important rules:
- If the article mentions the target person only briefly without adverse content, set is_adverse=false
- If the article is about a different person with the same name, set is_about_target_person=false and match_confidence="not_match"
- Use match_confidence="low" if there's only the name match but no other identifying details
- Use match_confidence="high" if multiple identifying details match (companies, region, role, etc.)
- Quotes must be word-for-word from the article, not paraphrased
- severity="none" only if is_adverse=false
"""


def _extract_json(raw: str) -> str:
    """Strip optional ```json … ``` fencing from Claude's response."""
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
    if match:
        return match.group(1)
    match = re.search(r"\{[\s\S]+\}", raw)
    if match:
        return match.group(0)
    return raw


def _base_result(article: dict[str, Any], person_name: str) -> dict[str, Any]:
    return {
        "url": article.get("url", ""),
        "title": article.get("title", ""),
        "person_name": person_name,
        "classified_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "is_about_target_person": False,
        "match_confidence": "not_match",
        "match_evidence": [],
        "is_adverse": False,
        "severity": "none",
        "category": "other",
        "summary": "",
        "key_quotes": [],
        "tokens_used": 0,
        "error": None,
    }


def classify(
    article: dict[str, Any],
    person_name: str,
    identifying_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify an article for adverse media content about a specific person.

    Args:
        article: Dict produced by scraper.scrape() — keys: url, title, markdown,
            scraped_at, error.
        person_name: Full name of the person under review (drives identity matching).
        identifying_hints: Optional extra attributes for disambiguation, e.g.
            {"date_of_birth": "1975-03-12", "region": "Тернопільська область",
             "known_companies": ["ТОВ Х"]}.
            Typically None in Phase 1; populated from Opendatabot in Phase 2.

    Returns:
        Structured classification dict. Never raises — errors surface via ``error``.
    """
    result = _base_result(article, person_name)

    if article.get("error"):
        result["error"] = f"Skipped: article had error: {article['error']}"
        return result

    markdown = article.get("markdown", "")
    if len(markdown) < _MIN_ARTICLE_CHARS:
        result["error"] = f"Article text too short to classify (< {_MIN_ARTICLE_CHARS} chars)"
        return result

    try:
        client = anthropic.Anthropic(max_retries=5)
        user_message = _build_user_message(article, person_name, identifying_hints)

        response = client.messages.create(
            model=MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw_text: str = response.content[0].text
        tokens_used: int = response.usage.input_tokens + response.usage.output_tokens

        try:
            parsed = json.loads(_extract_json(raw_text))
        except (json.JSONDecodeError, ValueError):
            logger.error("Malformed Claude response: %s", raw_text)
            result["error"] = "Invalid JSON in Claude response"
            result["tokens_used"] = tokens_used
            return result

        result.update(
            {
                "is_about_target_person": bool(parsed.get("is_about_target_person", False)),
                "match_confidence": parsed.get("match_confidence", "not_match"),
                "match_evidence": parsed.get("match_evidence", []),
                "is_adverse": bool(parsed.get("is_adverse", False)),
                "severity": parsed.get("severity", "none"),
                "category": parsed.get("category", "other"),
                "summary": parsed.get("summary", ""),
                "key_quotes": parsed.get("key_quotes", []),
                "tokens_used": tokens_used,
            }
        )

    except anthropic.AuthenticationError:
        result["error"] = "Invalid ANTHROPIC_API_KEY"
    except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
        result["error"] = f"Network error: {exc}"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"Classifier error: {exc}"

    return result


if __name__ == "__main__":
    import asyncio
    import sys

    from workers.scraper import scrape

    TEST_URL = (
        "https://suspilne.media/947495-rnbo-zaprovadili-sankcii-proti-"
        "kolomojskogo-bogolubova-zevago-i-medvedcuka/"
    )
    TARGET = "Ігор Коломойський"

    print(f"Scraping: {TEST_URL}\n")
    article = asyncio.run(scrape(TEST_URL))

    if article["error"]:
        print(f"Scrape ERROR: {article['error']}")
        sys.exit(1)

    print(f"Scraped : {article['title']} ({article['markdown_length']} chars)\n")
    print(f"Classifying for: {TARGET}\n")

    result = classify(article, TARGET)

    if result["error"]:
        print(f"ERROR: {result['error']}")
        sys.exit(1)

    print(f"is_about_target_person : {result['is_about_target_person']}")
    print(f"match_confidence       : {result['match_confidence']}")
    print(f"is_adverse             : {result['is_adverse']}")
    print(f"severity               : {result['severity']}")
    print(f"category               : {result['category']}")
    print(f"summary                : {result['summary']}")
    print()
    print("match_evidence (перші 2):")
    for ev in result["match_evidence"][:2]:
        print(f"  • {ev}")
    print()
    print("key_quotes (перші 2):")
    for q in result["key_quotes"][:2]:
        print(f"  « {q} »")
    print()
    print(f"tokens_used            : {result['tokens_used']}")
