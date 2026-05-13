from __future__ import annotations

from pathlib import Path

import yaml


def generate_queries(
    person_name: str,
    config_path: str = "config/risk_keywords.yaml",
) -> list[dict]:
    """Generate Serper API search queries for adverse media discovery.

    Produces two query types per language (uk / ru / en):
      - risk_only:  "{person_name} {risk_keyword}"
      - risk_geo:   "{person_name} {risk_keyword} {geo_indicator}"

    Priority order when the result is capped at max_queries_per_person:
      1. All risk_only queries (all three languages).
      2. risk_geo queries, interleaved by geographic index so that the
         highest-priority RF/BY indicators (first entries in the YAML) are
         added before lower-priority ones.

    Args:
        person_name: Full name of the person, e.g. "Іван Петренко".
        config_path: Path to risk_keywords.yaml (relative to CWD or absolute).

    Returns:
        List of dicts, each with keys:
            query    — the search string sent to Serper
            language — "uk" / "ru" / "en"
            type     — "risk_only" / "risk_geo"
            gl       — Serper country parameter
            hl       — Serper language parameter

    Raises:
        FileNotFoundError: Config file does not exist at config_path.
        ValueError: YAML is malformed or missing required sections.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    try:
        with path.open(encoding="utf-8") as fh:
            config = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"Malformed YAML in {config_path}: {exc}") from exc

    try:
        risk_kw: dict[str, list[str]] = config["risk_keywords"]
        geo_focus: dict[str, list[str]] = config["geographic_focus"]
        settings: dict = config["search_settings"]
        locale_map: dict[str, dict[str, str]] = settings["serper_locale_mapping"]
        max_queries: int = int(settings["max_queries_per_person"])
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"Missing required section in {config_path}: {exc}"
        ) from exc

    languages: tuple[str, ...] = ("uk", "ru", "en")
    name = person_name.strip()

    def _entry(query: str, lang: str, query_type: str) -> dict:
        locale = locale_map[lang]
        return {
            "query": query,
            "language": lang,
            "type": query_type,
            "gl": locale["gl"],
            "hl": locale["hl"],
        }

    # --- Phase 1: risk_only — name + risk keyword, all languages ---
    risk_only: list[dict] = [
        _entry(f"{name} {kw}", lang, "risk_only")
        for lang in languages
        for kw in risk_kw.get(lang, [])
    ]

    # --- Phase 2: risk_geo — interleave by geo index so that index-0 terms
    # (Росія / Россия / Russia) across all languages are added before
    # index-1 terms (Білорусь / Беларусь / Belarus), and so on.
    # The YAML already lists RF/BY indicators first. ---
    max_geo_len = max((len(geo_focus.get(lang, [])) for lang in languages), default=0)
    risk_geo: list[dict] = [
        _entry(f"{name} {kw} {geo_focus[lang][geo_idx]}", lang, "risk_geo")
        for geo_idx in range(max_geo_len)
        for lang in languages
        if geo_idx < len(geo_focus.get(lang, []))
        for kw in risk_kw.get(lang, [])
    ]

    return (risk_only + risk_geo)[:max_queries]


if __name__ == "__main__":
    queries = generate_queries("Іван Петренко")
    print(f"Total queries generated: {len(queries)}\n")
    print("First 5 queries:")
    for i, q in enumerate(queries[:5], 1):
        print(f"  {i}. [{q['language']}][{q['type']:9}] {q['query']!r}")
        print(f"       gl={q['gl']!r}  hl={q['hl']!r}")
