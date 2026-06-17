"""Worker 1 (фізособи) "Реєстратор осіб" — Celery task для перевірки фізособи.

РНОКПП → Opendatabot (/fop + /person-sanctions + /pep + /companies/{founder})
  → сирий JSON (evidence) → нормалізація → KYC-запис особи

Збереження:
  data/raw/person_{rnokpp}/{fop,sanctions,pep,companies}.json
  data/normalized/person_{rnokpp}/registry.json   (зведений запис особи)

Структура /fop майже ідентична /full-company (data.registry + data.factors),
тому перевикористовуємо _clean / _risk_signals із registry_task.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workers.celery_app import app
from workers.opendatabot_client import (
    get_fop,
    get_pep,
    get_person_sanctions,
)
from workers.registry_task import _risk_signals

logger = logging.getLogger(__name__)

_OUTPUT_BASE = Path(os.getenv("OUTPUT_DIR", "data"))


def _person_slug(rnokpp: str) -> str:
    digits = "".join(ch for ch in str(rnokpp) if ch.isdigit())
    return f"person_{digits or 'unknown'}"


def _reg(envelope: dict[str, Any]) -> dict[str, Any]:
    data = envelope.get("data") or {}
    reg = data.get("registry")
    return reg if isinstance(reg, dict) else (data if isinstance(data, dict) else {})


def _location(reg: dict[str, Any]) -> str | None:
    loc = reg.get("location")
    if isinstance(loc, str) and loc.strip():
        return loc
    addr = reg.get("address")
    if isinstance(addr, dict):
        return addr.get("address")
    return None


def _normalize_person(
    rnokpp: str,
    fop_env: dict[str, Any],
    sanc_env: dict[str, Any] | None,
    pep_env: dict[str, Any] | None,
    comp_env: dict[str, Any] | None,
) -> dict[str, Any]:
    reg = _reg(fop_env)
    factors = (fop_env.get("data") or {}).get("factors") or []
    risk = _risk_signals(factors if isinstance(factors, list) else [])

    # Банкрутство ФОП — окремий обʼєкт у registry (не factor) → у critical.
    bankruptcy = reg.get("bankruptcy")
    if isinstance(bankruptcy, dict) and bankruptcy.get("stateText"):
        risk["critical"].append({
            "type": "bankruptcy",
            "text": f"ФОП: {bankruptcy.get('stateText')} "
                    f"({bankruptcy.get('docNumber') or ''})".strip(),
        })

    # Припинення діяльності — інформаційний сигнал.
    termination = reg.get("termination")
    terminated = (isinstance(termination, dict)
                  and termination.get("stateText") == "припинено")

    # Санкції особи (/person-sanctions).
    person_sanctions: list[dict[str, Any]] = []
    if sanc_env and not sanc_env.get("error"):
        sdata = sanc_env.get("data") or {}
        raw_s = (sdata.get("data") or sdata.get("items")
                 or sdata.get("sanctions") or [])
        if isinstance(raw_s, dict):
            raw_s = [raw_s]
        if isinstance(raw_s, list):
            for s in raw_s:
                if isinstance(s, dict):
                    person_sanctions.append({
                        "list": s.get("sanctionList") or s.get("list"),
                        "reason": s.get("sanctionReason") or s.get("reason"),
                        "start_date": s.get("startDate") or s.get("date"),
                    })
        if person_sanctions:
            risk["critical"].append({
                "type": "sanction",
                "text": f"Особа у санкційному списку РНБО "
                        f"({len(person_sanctions)} запис(ів))",
                "items": person_sanctions,
            })

    # PEP (/pep) — реальна схема: data[] з isPep, events, relatives, riskCriteria.
    is_pep = False
    pep_matches: list[str] = []
    pep_relatives: list[dict[str, Any]] = []
    pep_risk: str | None = None
    if pep_env and not pep_env.get("error"):
        pdata = pep_env.get("data") or {}
        raw_p = pdata.get("data") if isinstance(pdata, dict) else pdata
        if isinstance(raw_p, dict):
            raw_p = [raw_p]
        if isinstance(raw_p, list):
            for p in raw_p:
                if not isinstance(p, dict):
                    continue
                if p.get("isPep"):
                    is_pep = True
                if p.get("riskCriteria"):
                    pep_risk = p.get("riskCriteria")
                nm = p.get("fullName") or p.get("name")
                if nm:
                    pep_matches.append(nm)
                for rel in (p.get("relatives") or []):
                    if isinstance(rel, dict) and rel.get("relativeName"):
                        pep_relatives.append({
                            "name": rel.get("relativeName"),
                            "type": rel.get("relativeType"),
                        })
        if is_pep:
            risk["pep"].append({
                "text": "Особа у реєстрі національних публічних діячів (PEP)"
                        + (f" — рівень ризику: {pep_risk}" if pep_risk else ""),
                "names": [n for n in pep_matches if n],
                "relatives": pep_relatives,
                "risk_criteria": pep_risk,
            })

    # Повʼязані компанії (/companies/{founder}) — коло 1.
    related_companies: list[dict[str, Any]] = []
    companies_status = "ok"
    if comp_env:
        if comp_env.get("tariff_gated"):
            companies_status = "tariff_gated"  # недоступно на тарифі (403)
        elif comp_env.get("error"):
            companies_status = "error"
        else:
            cdata = comp_env.get("data") or {}
            raw_c = (cdata.get("data") or cdata.get("items")
                     or cdata.get("companies") or [])
            if isinstance(raw_c, dict):
                raw_c = [raw_c]
            if isinstance(raw_c, list):
                for c in raw_c:
                    if isinstance(c, dict):
                        related_companies.append({
                            "name": c.get("fullName") or c.get("name"),
                            "edrpou": c.get("code") or c.get("edrpou"),
                            "role": c.get("role"),
                        })
    else:
        companies_status = "unavailable_via_api"

    return {
        "source": "opendatabot",
        "subject_type": "person",
        "rnokpp": "".join(ch for ch in str(rnokpp) if ch.isdigit()),
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "data_as_of": reg.get("lastDate"),
        "name": reg.get("fullName"),
        "birth_date": reg.get("birthDate"),
        "sex": reg.get("sex"),
        "status": reg.get("status"),
        "address": _location(reg),
        "country": reg.get("country"),
        "is_fop": bool(reg.get("code")),
        "fop_terminated": terminated,
        "fop_activity": reg.get("primaryActivity"),
        "registration_date": reg.get("registrationDate"),
        "is_pep": is_pep,
        "person_sanctions": person_sanctions,
        "related_companies": related_companies,
        "related_companies_status": companies_status,
        "risk_signals": risk,
        "error": fop_env.get("error"),
        "raw_available": bool(reg),
    }


@app.task(name="workers.tasks.person_task")
def person_task(rnokpp: str, full_name: str | None = None,
                with_companies: bool = True) -> dict[str, Any]:
    """Повна перевірка фізособи: ФОП + санкції + PEP + повʼязані компанії."""
    summary: dict[str, Any] = {
        "subject_type": "person",
        "rnokpp": None,
        "name": None,
        "is_pep": False,
        "sanctioned": False,
        "related_companies_count": 0,
        "critical_signals": 0,
        "saved_to": None,
        "error": None,
    }

    try:
        slug = _person_slug(rnokpp)
        raw_dir = _OUTPUT_BASE / "raw" / slug
        norm_dir = _OUTPUT_BASE / "normalized" / slug
        os.makedirs(raw_dir, exist_ok=True)
        os.makedirs(norm_dir, exist_ok=True)

        fop_env = get_fop(rnokpp)
        (raw_dir / "fop.json").write_text(
            json.dumps(fop_env, ensure_ascii=False, indent=2), encoding="utf-8")

        sanc_env = get_person_sanctions(rnokpp)
        (raw_dir / "sanctions.json").write_text(
            json.dumps(sanc_env, ensure_ascii=False, indent=2), encoding="utf-8")

        pep_env = None
        name_for_pep = full_name or _reg(fop_env).get("fullName")
        if name_for_pep:
            pep_env = get_pep(name_for_pep)
            (raw_dir / "pep.json").write_text(
                json.dumps(pep_env, ensure_ascii=False, indent=2), encoding="utf-8")

        # 4. Повʼязані компанії конкретної особи.
        # УВАГА: Opendatabot не надає "компанії особи" по РНОКПП —
        # ендпоінт /companies приймає лише категорії ruFounders/byFounders/
        # irFounders (масовий RF/BY-список), не код особи. Тож обхід кола 1
        # через це API недоступний → передаємо None, статус буде
        # "unavailable_via_api" (чесна деградація, не помилка).
        comp_env = None

        record = _normalize_person(rnokpp, fop_env, sanc_env, pep_env, comp_env)
        (norm_dir / "registry.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

        crit = len(record["risk_signals"]["critical"])
        summary.update({
            "rnokpp": record["rnokpp"],
            "name": record.get("name"),
            "is_pep": record["is_pep"],
            "sanctioned": bool(record["person_sanctions"]),
            "related_companies_count": len(record["related_companies"]),
            "critical_signals": crit,
            "saved_to": str(norm_dir / "registry.json"),
            "error": record.get("error"),
        })
        logger.info("person_task: %s — name=%r, pep=%s, sanctioned=%s, %d companies",
                    rnokpp, record.get("name"), record["is_pep"],
                    bool(record["person_sanctions"]),
                    len(record["related_companies"]))
        return summary

    except Exception as exc:
        logger.exception("person_task failed for %r", rnokpp)
        summary["error"] = f"person_task error: {exc}"
        return summary
