"""Нормалізація та варіанти входу — рівень 2+3 резолвінгу імен.

Проблема: на вході часто не код (ЄДРПОУ/РНОКПП), а імʼя/назва, ще й у різних
написаннях (англ. транслітерація, лапки, орг-форми, регістр). Цей модуль
готує вхід до пошуку і резолвить назву компанії в ЄДРПОУ.

1. clean_company_name — прибирає орг-форми (ТОВ/LLC/...), лапки, пробіли.
2. name_variants — генерує варіанти написання імені (транслітерація).
3. resolve_company — назва → ЄДРПОУ через Opendatabot search, з асиметричним
   принципом: якщо кандидатів кілька — НЕ вгадуємо, повертаємо список.

ВАЖЛИВО: ми НЕ перекладаємо назви за змістом (LLC Sunrise → ТОВ Sunrise,
а не ТОВ Світанок). Тільки транслітерація і чищення форми.
"""

from __future__ import annotations

import re

_ORG_FORMS = [
    "товариство з обмеженою відповідальністю",
    "приватне акціонерне товариство",
    "публічне акціонерне товариство",
    "акціонерне товариство",
    "приватне підприємство",
    "державне підприємство",
    "комунальне підприємство",
    "тов", "пат", "прат", "ат", "пп", "дп", "кп", "фоп",
    "общество с ограниченной ответственностью", "ооо", "оао", "зао",
    "limited liability company", "llc", "ltd", "limited",
    "joint stock company", "jsc", "inc", "incorporated", "corp",
    "corporation", "gmbh", "co",
]

_LAT2CYR = [
    ("shch", "щ"), ("sch", "щ"), ("kh", "х"), ("zh", "ж"), ("ch", "ч"),
    ("sh", "ш"), ("ts", "ц"), ("ya", "я"), ("yu", "ю"), ("ye", "є"),
    ("yi", "ї"), ("ii", "ій"), ("iy", "ій"),
    ("a", "а"), ("b", "б"), ("v", "в"), ("h", "г"), ("g", "ґ"),
    ("d", "д"), ("e", "е"), ("z", "з"), ("y", "и"), ("i", "і"),
    ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"),
    ("p", "п"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"),
    ("f", "ф"), ("c", "ц"), ("j", "й"),
]


def clean_company_name(name: str | None) -> str:
    """Прибирає орг-форми, лапки, зайві пробіли. Лишає «ядро» назви."""
    if not name:
        return ""
    s = str(name).strip()
    s = re.sub(r"[\"'«»''`,.]", " ", s)
    low = s.lower()
    for form in sorted(_ORG_FORMS, key=len, reverse=True):
        low = re.sub(rf"\b{re.escape(form)}\b", " ", low)
    low = re.sub(r"\s+", " ", low).strip()
    return low


def _looks_latin(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    latin = sum(1 for c in letters if ord(c) < 128)
    return latin / len(letters) > 0.5


def _translit_lat2cyr(text: str) -> str:
    s = text.lower()
    for lat, cyr in _LAT2CYR:
        s = s.replace(lat, cyr)
    return s


def name_variants(name: str | None) -> list[str]:
    """Генерує варіанти написання для пошуку: оригінал + транслітерація."""
    if not name:
        return []
    name = str(name).strip()
    variants: list[str] = [name]
    if _looks_latin(name):
        cyr = _translit_lat2cyr(name)
        variants.append(" ".join(w.capitalize() for w in cyr.split()))
        variants.append(cyr)
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        v = v.strip()
        key = v.lower()
        if v and key not in seen:
            seen.add(key)
            out.append(v)
    return out


def resolve_company(name: str, max_candidates: int = 10) -> dict:
    """Резолвить назву компанії в ЄДРПОУ через Opendatabot search.

    Асиметричний принцип — НЕ вгадуємо:
      0 кандидатів → status="not_found"
      1 кандидат   → status="resolved" + edrpou
      >1           → status="ambiguous" + список (потрібне уточнення)
    """
    from workers.opendatabot_client import search_companies

    cleaned = clean_company_name(name) or name.strip()
    result: dict = {
        "status": "error", "query": cleaned, "edrpou": None,
        "candidates": [], "count": 0, "error": None,
    }

    env = search_companies(cleaned, field="full_name",
                           limit=max_candidates, partial=True)
    if env.get("error"):
        result["error"] = env["error"]
        return result

    # Відповідь двічі вкладена: env["data"] = {"status","data":{count,items}}.
    payload = env.get("data") or {}
    data = payload.get("data") or payload  # спускаємось на рівень items/count
    items = data.get("items") or []
    try:
        total = int(data.get("count") or len(items))
    except (ValueError, TypeError):
        total = len(items)

    candidates = []
    for it in items:
        if isinstance(it, dict) and it.get("code"):
            candidates.append({
                "code": str(it["code"]),
                "field": it.get("field"),
                "value": it.get("value"),
            })

    result["candidates"] = candidates
    result["count"] = total

    if not candidates:
        result["status"] = "not_found"
    elif len(candidates) == 1:
        result["status"] = "resolved"
        result["edrpou"] = candidates[0]["code"]
    else:
        result["status"] = "ambiguous"

    return result
