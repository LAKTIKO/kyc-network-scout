"""Worker 2 "Санкційний контролер" — Celery task для санкційного скринінгу.

Джерело: Opendatabot (НЕ OpenSanctions — окремий ключ не потрібен).

Розгалуження за типом субʼєкта (архітектурно чисте):
  • особи    → /person-sanctions, LIST-COMPLETE (кожен критичний список
               окремим запитом, щоб OFAC/SDN не випав за межі вікна).
               Списки: РНБО, США (SDN/Non-SDN), ЄС, Велика Британія, Канада.
  • компанії → санкційні factors (sanction/nbuSanctions) з реєстру, який
               уже зібрав Worker 1 (registry.json). Не запитуємо персональний
               ендпоінт — санкції юрособи живуть у реєстрових факторах.

Збереження (той самий slug, що Worker 1):
  data/raw/{slug}/sanctions.json
  data/normalized/{slug}/sanctions.json
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workers.celery_app import app
from workers.opendatabot_client import screen_person_sanctions

logger = logging.getLogger(__name__)

_OUTPUT_BASE = Path(os.getenv("OUTPUT_DIR", "data"))


def _build_verdict(subject: str, hits: list[dict[str, Any]],
                   checked_lists: list[str], matched_lists: list[str],
                   slug: str | None, source: str,
                   error: str | None) -> dict[str, Any]:
    return {
        "source": source,
        "slug": slug,
        "subject": subject,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "checked_lists": checked_lists,
        "matched_lists": matched_lists,
        "has_sanctions_match": bool(hits),
        "total_hits": len(hits),
        "hits": hits,
        "error": error,
    }


def _company_sanctions_from_registry(slug: str) -> dict[str, Any]:
    """Витягує санкційні factors компанії з уже зібраного registry.json.

    Санкції юрособи приходять як factors (sanction / nbuSanctions) у
    відповіді /full-company. Worker 1 кладе їх у risk_signals.critical.
    Тут перетворюємо їх на санкційні hits для санкційної секції звіту.
    """
    reg_path = _OUTPUT_BASE / "normalized" / slug / "registry.json"
    hits: list[dict[str, Any]] = []
    if not reg_path.exists():
        return {"hits": hits, "checked_lists": [], "matched_lists": []}

    try:
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
    except Exception:
        return {"hits": hits, "checked_lists": [], "matched_lists": []}

    rs = reg.get("risk_signals") or {}
    matched: list[str] = []
    for c in rs.get("critical", []):
        if c.get("type") == "sanction":
            label = c.get("sanction_list") or "РНБО/реєстр"
            hits.append({
                "name": reg.get("name"),
                "code": reg.get("edrpou"),
                "sanction_list": label,
                "reason": c.get("text") or c.get("sanction_reason"),
                "start_date": c.get("start_date"),
                "details": c.get("items"),
            })
            if label not in matched:
                matched.append(label)

    return {
        "hits": hits,
        "checked_lists": ["реєстрові санкційні factors (Opendatabot)"],
        "matched_lists": matched,
    }


@app.task(name="workers.tasks.sanctions_task")
def sanctions_task(
    subject_label: str,
    subject_type: str = "organization",
    slug: str | None = None,
    country: str | None = None,
    reg_number: str | None = None,
    birth_date: str | None = None,
    tax_id: str | None = None,
) -> dict[str, Any]:
    """Санкційний скринінг субʼєкта. Розгалужується за subject_type:
       person → /person-sanctions list-complete; organization → factors реєстру.
    """
    summary: dict[str, Any] = {
        "subject": subject_label,
        "subject_type": subject_type,
        "has_sanctions_match": False,
        "total_hits": 0,
        "matched_lists": [],
        "saved_to": None,
        "error": None,
    }

    try:
        if subject_type == "person":
            screen = screen_person_sanctions(subject_label, search_keys="name")
            data = screen.get("data") or {}
            hits = data.get("hits") or []
            checked = data.get("checked_lists") or []
            matched = data.get("matched_lists") or []
            error = screen.get("error")
            raw_to_save = screen

            code = tax_id or reg_number
            if code:
                by_code = screen_person_sanctions(str(code), search_keys="code")
                cdata = by_code.get("data") or {}
                existing = {(h.get("name"), h.get("sanction_list")) for h in hits}
                for h in cdata.get("hits") or []:
                    key = (h.get("name"), h.get("sanction_list"))
                    if key not in existing:
                        hits.append(h)
                        existing.add(key)
                for ml in cdata.get("matched_lists") or []:
                    if ml not in matched:
                        matched.append(ml)
            source = "opendatabot (/person-sanctions)"
        else:
            if not slug:
                raise ValueError("slug потрібен для санкцій компанії (factors реєстру)")
            comp = _company_sanctions_from_registry(slug)
            hits = comp["hits"]
            checked = comp["checked_lists"]
            matched = comp["matched_lists"]
            error = None
            raw_to_save = {"data": comp}
            source = "opendatabot (реєстрові factors)"

        verdict = _build_verdict(subject_label, hits, checked, matched,
                                 slug, source, error)

        save_slug = slug or f"subject_{subject_label[:30]}"
        raw_dir = _OUTPUT_BASE / "raw" / save_slug
        norm_dir = _OUTPUT_BASE / "normalized" / save_slug
        os.makedirs(raw_dir, exist_ok=True)
        os.makedirs(norm_dir, exist_ok=True)

        (raw_dir / "sanctions.json").write_text(
            json.dumps(raw_to_save, ensure_ascii=False, indent=2),
            encoding="utf-8")
        (norm_dir / "sanctions.json").write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")

        summary.update({
            "has_sanctions_match": verdict["has_sanctions_match"],
            "total_hits": verdict["total_hits"],
            "matched_lists": matched,
            "saved_to": str(norm_dir / "sanctions.json"),
            "error": error,
        })
        logger.info("sanctions_task: %s (%s) — match=%s, lists=%s",
                    subject_label, subject_type,
                    verdict["has_sanctions_match"], matched)
        return summary

    except Exception as exc:
        logger.exception("sanctions_task failed for %r", subject_label)
        summary["error"] = f"sanctions_task error: {exc}"
        return summary
