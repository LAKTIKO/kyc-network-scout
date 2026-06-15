"""Worker 1 "Реєстратор" — Celery task для збору даних з UA-реєстру.

ЄДРПОУ → Opendatabot /full-company → сирий JSON (evidence) → нормалізація → KYC-запис
Збереження:
  data/raw/company_{edrpou}/registry.json
  data/normalized/company_{edrpou}/registry.json

Схема Opendatabot:
  data.registry  — реєстраційні дані
  data.factors[] — ризик-сигнали (НЕуніфікована схема: різні типи мають
                   різні поля; indicator може містити U+FE0F; declarantOwner
                   приходить лише з items без text/indicator)
"""

from __future__ import annotations

import json
import logging
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workers.celery_app import app
from workers.opendatabot_client import get_company, search_companies

logger = logging.getLogger(__name__)

_OUTPUT_BASE = Path(os.getenv("OUTPUT_DIR", "data"))

# Критичні типи factors (HIGH-ризик за KYC-логікою, RF/BY-фокус).
_CRITICAL_FACTOR_TYPES = {
    "ruFounders", "byFounders", "irFounders",
    "sanction", "nbuSanctions", "amcu",   # санкції РНБО / НБУ / АМКУ
    "bankruptcy", "warTerritory",
}
_PEP_FACTOR_TYPES = {"declarantOwner", "publicOfficial"}


def _clean(val: Any) -> str:
    """Нормалізує рядок: прибирає U+FE0F та інші невидимі символи (Mn/Cf/So),
    обрізає, lowercase. Критично для indicator зі сміттєвим emoji-суфіксом."""
    if not isinstance(val, str):
        return ""
    s = "".join(c for c in val if unicodedata.category(c) not in ("Mn", "Cf", "So"))
    return s.strip().lower()


def _payload(envelope: dict[str, Any]) -> dict[str, Any]:
    """Розгортає відповідь до рівня, де лежать registry/factors.

    Клієнт кладе сире тіло API у envelope['data'], а саме тіло має форму
    {status, data: {registry, factors, ...}, forDevelopers} — тобто registry/
    factors на ОДИН рівень глибше: envelope['data']['data'].
    Толерантно: якщо проміжного 'data' немає (тіло передане напряму, як стара
    фікстура), лишаємось на рівні тіла.
    """
    body = envelope.get("data") or {}
    if not isinstance(body, dict):
        return {}
    inner = body.get("data")
    return inner if isinstance(inner, dict) else body


def _registry(envelope: dict[str, Any]) -> dict[str, Any]:
    reg = _payload(envelope).get("registry")
    return reg if isinstance(reg, dict) else {}


def _factors(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    f = _payload(envelope).get("factors")
    return f if isinstance(f, list) else []


def _company_slug(edrpou: str) -> str:
    digits = "".join(ch for ch in str(edrpou) if ch.isdigit())
    return f"company_{digits or 'unknown'}"


def _location_str(reg: dict[str, Any]) -> str | None:
    loc = reg.get("location")
    if isinstance(loc, str) and loc.strip():
        return loc
    addr = reg.get("address")
    if isinstance(addr, dict):
        return addr.get("address") or None
    return None


def _director(reg: dict[str, Any]) -> str | None:
    ceo = reg.get("ceoName")
    if isinstance(ceo, str) and ceo.strip():
        return ceo
    heads = reg.get("heads")
    if isinstance(heads, list) and heads and isinstance(heads[0], dict):
        return heads[0].get("name")
    return None


def _beneficiaries(reg: dict[str, Any]) -> list[dict[str, Any]]:
    raw = reg.get("beneficiaries")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for b in raw:
        if not isinstance(b, dict):
            out.append({"name": str(b), "code": None, "capital": None,
                        "percent": None, "country": None,
                        "role": "founder", "is_person": None})
            continue
        out.append({
            "name": b.get("name"),
            "code": b.get("code"),
            "capital": b.get("amount") or b.get("capital"),
            "percent": b.get("amountPercent") or b.get("interest"),
            "country": b.get("country"),
            "role": b.get("role") or "beneficiary",
            "is_person": b.get("person"),
        })
    return out


def _sanction_detail(f: dict[str, Any]) -> dict[str, Any]:
    """Деталі санкційного фактора (sanction / nbuSanctions)."""
    detail: dict[str, Any] = {
        "sanction_list": f.get("sanctionList"),
        "sanction_reason": f.get("sanctionReason"),
        "start_date": f.get("startDate"),
    }
    # nbuSanctions: деталі в items[] (date, sanction, reason, sanctionAmount)
    items = f.get("items")
    if isinstance(items, list) and items:
        parsed = []
        for it in items:
            if not isinstance(it, dict):
                continue
            amt = it.get("sanctionAmount")
            try:
                amt = float(amt) if amt not in (None, "") else None
            except (ValueError, TypeError):
                amt = None
            parsed.append({
                "date": it.get("date"),
                "sanction": it.get("sanction"),
                "reason": it.get("reason"),
                "amount": amt,
            })
        detail["items"] = parsed
    return {k: v for k, v in detail.items() if v}


def _risk_signals(factors: list[dict[str, Any]]) -> dict[str, Any]:
    """data.factors → структуровані ризик-сигнали для скорингу.

    Толерантно до неуніфікованої схеми: усе через .get(), indicator чиститься
    від U+FE0F, declarantOwner обробляється окремо (немає text/indicator)."""
    critical: list[dict[str, Any]] = []
    pep: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for f in factors:
        if not isinstance(f, dict):
            continue
        ftype = f.get("type")
        indicator = _clean(f.get("indicator"))
        text = f.get("text") or f.get("sanctionComment") or f.get("sanctionList")

        if ftype in _CRITICAL_FACTOR_TYPES:
            entry = {"type": ftype, "text": text}
            if ftype in ("sanction", "nbuSanctions"):
                entry.update(_sanction_detail(f))
            critical.append(entry)
        elif ftype in _PEP_FACTOR_TYPES:
            names = [it.get("pib") for it in (f.get("items") or [])
                     if isinstance(it, dict) and it.get("pib")]
            pep.append({
                "text": text or "Публічний декларант серед власників",
                "names": names,
            })
        elif indicator in ("warning", "critical", "negative") and ftype:
            warnings.append({"type": ftype, "text": text, "indicator": indicator})

    return {
        "critical": critical,
        "pep": pep,
        "warnings": warnings,
        "raw_count": len(factors),
    }


def _normalize_company(edrpou: str, envelope: dict[str, Any]) -> dict[str, Any]:
    reg = _registry(envelope)
    factors = _factors(envelope)
    cap = reg.get("authorisedCapital")
    auth_capital = cap.get("value") if isinstance(cap, dict) else reg.get("capital")

    return {
        "source": "opendatabot",
        "subject_type": "company",
        "edrpou": "".join(ch for ch in str(edrpou) if ch.isdigit()),
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "data_as_of": reg.get("lastTime"),
        "source_url": envelope.get("url"),
        "api_status": envelope.get("status_code"),
        "name": reg.get("fullName") or reg.get("shortName"),
        "name_en": reg.get("fullNameEn") or reg.get("shortNameEn"),
        "status": reg.get("status"),
        "registration_date": reg.get("registrationDate"),
        "address": _location_str(reg),
        "kved": reg.get("primaryActivity"),
        "authorized_capital": auth_capital,
        "director": _director(reg),
        "beneficiaries": _beneficiaries(reg),
        "risk_signals": _risk_signals(factors),
        "error": envelope.get("error"),
        "raw_available": bool(reg),
    }


@app.task(name="workers.tasks.registry_task")
def registry_task(company_input: str, is_edrpou: bool = True) -> dict[str, Any]:
    """Збирає дані юрособи з UA-реєстру та зберігає evidence + нормалізований запис."""
    summary: dict[str, Any] = {
        "subject_type": "company",
        "edrpou": None,
        "name": None,
        "beneficiaries_count": 0,
        "critical_signals": 0,
        "saved_to": None,
        "error": None,
    }

    try:
        edrpou = company_input.strip()
        if not is_edrpou:
            logger.info("registry_task: resolving name -> edrpou: %r", company_input)
            sr = search_companies(company_input, limit=1)
            if sr.get("error") or not sr.get("ok"):
                summary["error"] = f"search failed: {sr.get('error')}"
                return summary
            payload = sr.get("data") or {}
            data = payload.get("data") or payload
            hits = (data.get("items") or data.get("companies")
                    or data.get("results") or [])
            if not hits:
                summary["error"] = f"no company found for {company_input!r}"
                return summary
            first = hits[0] if isinstance(hits, list) else hits
            edrpou = str(first.get("code") or first.get("edrpou") or "").strip()
            if not edrpou:
                summary["error"] = "resolved hit has no edrpou code"
                return summary

        envelope = get_company(edrpou)

        slug = _company_slug(edrpou)
        raw_dir = _OUTPUT_BASE / "raw" / slug
        norm_dir = _OUTPUT_BASE / "normalized" / slug
        os.makedirs(raw_dir, exist_ok=True)
        os.makedirs(norm_dir, exist_ok=True)

        (raw_dir / "registry.json").write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")

        record = _normalize_company(edrpou, envelope)
        (norm_dir / "registry.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

        if envelope.get("error"):
            logger.warning("registry_task: API error for %s: %s",
                           edrpou, envelope["error"])
            summary["edrpou"] = edrpou
            summary["error"] = envelope["error"]
            summary["saved_to"] = str(norm_dir / "registry.json")
            return summary

        crit = len(record["risk_signals"]["critical"])
        logger.info("registry_task: %s — name=%r, %d benef, %d critical",
                    edrpou, record.get("name"),
                    len(record["beneficiaries"]), crit)
        summary.update({
            "edrpou": edrpou,
            "name": record.get("name"),
            "beneficiaries_count": len(record["beneficiaries"]),
            "critical_signals": crit,
            "saved_to": str(norm_dir / "registry.json"),
            "error": None,
        })
        return summary

    except Exception as exc:
        logger.exception("registry_task failed for %r", company_input)
        summary["error"] = f"registry_task error: {exc}"
        return summary
