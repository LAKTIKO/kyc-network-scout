"""Opendatabot API client — Worker 1 "Реєстратор".

Тонкий клієнт до Opendatabot REST API. Повертає сирий JSON реєстру
без інтерпретації — нормалізація відбувається в registry_task.py.
Ключ читається з env OPENDATABOT_API_KEY — жодного хардкоду секретів.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_API_BASE = os.getenv("OPENDATABOT_API_BASE", "https://opendatabot.com/api")
_API_VERSION = os.getenv("OPENDATABOT_API_VERSION", "v3")
_TIMEOUT = float(os.getenv("OPENDATABOT_TIMEOUT", "30"))
_MAX_RETRIES = int(os.getenv("OPENDATABOT_MAX_RETRIES", "3"))


class OpendatabotError(Exception):
    """Помилка звернення до Opendatabot API."""


def _api_key() -> str:
    key = os.getenv("OPENDATABOT_API_KEY", "").strip()
    if not key:
        raise OpendatabotError(
            "OPENDATABOT_API_KEY не заданий. Додай ключ у .env"
        )
    return key


def _request(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = dict(params or {})
    params["apiKey"] = _api_key()

    url = f"{_API_BASE}/{_API_VERSION}/{path.lstrip('/')}"
    safe_url = f"{_API_BASE}/{_API_VERSION}/{path.lstrip('/')}"

    envelope: dict[str, Any] = {
        "ok": False,
        "status_code": None,
        "url": safe_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data": None,
        "not_found": False,
        "error": None,
    }

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = httpx.get(url, params=params, timeout=_TIMEOUT)
            envelope["status_code"] = resp.status_code

            if resp.status_code == 200:
                envelope["ok"] = True
                envelope["data"] = resp.json()
                return envelope

            # 404 — субʼєкта НЕМАЄ в реєстрі (не помилка, а факт відсутності).
            # Для KYC це принципово відрізняється від збою API: неіснуючий
            # код — це red flag (фейк/шелл/одруківка), а не "повтори пізніше".
            if resp.status_code == 404:
                envelope["not_found"] = True
                return envelope

            # Інші 4xx (крім 429) — не ретраїмо, це наша помилка (ключ/запит).
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                envelope["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
                return envelope

            last_exc = OpendatabotError(
                f"HTTP {resp.status_code} (attempt {attempt}/{_MAX_RETRIES})"
            )
            logger.warning("opendatabot %s — %s", safe_url, last_exc)

        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            logger.warning(
                "opendatabot network error %s (attempt %d/%d): %s",
                safe_url, attempt, _MAX_RETRIES, exc,
            )

    envelope["error"] = f"Failed after {_MAX_RETRIES} retries: {last_exc}"
    return envelope


def get_company(edrpou: str) -> dict[str, Any]:
    """Повна інформація юрособи за кодом ЄДРПОУ (8 цифр).

    Ендпоінт /full-company/{code}: реєстраційні дані (data.registry) +
    ризик-фактори (data.factors) — санкції, RF/BY-власники, банкрутство,
    податковий борг, бойові території тощо.
    """
    edrpou = str(edrpou).strip()
    logger.info("opendatabot get_company: %s", edrpou)
    return _request(f"full-company/{edrpou}")


def get_fop(rnokpp: str) -> dict[str, Any]:
    """Дані ФОП за РНОКПП (ІПН)."""
    rnokpp = str(rnokpp).strip()
    logger.info("opendatabot get_fop: %s", rnokpp)
    return _request(f"fop/{rnokpp}")


def search_companies(query: str, field: str = "full_name",
                     limit: int = 10, partial: bool = True,
                     active_only: bool = True) -> dict[str, Any]:
    """Пошук компаній за назвою (резолв назва → ЄДРПОУ).

    Ендпоінт /search/companies-by-field. Повертає data.items[] з code.
    """
    params: dict[str, Any] = {
        "field": field,
        "q": query,
        "limit": limit,
        "partial": "true" if partial else "false",
    }
    if active_only:
        params["active"] = "true"
    logger.info("opendatabot search_companies: field=%s q=%r", field, query)
    return _request("search/companies-by-field", params=params)


def get_person_sanctions(query: str, search_keys: str = "name,code",
                         sanction_lists: list[str] | None = None,
                         limit: int = 50) -> dict[str, Any]:
    """Санкції фізособи за ПІБ або кодом. Ендпоінт /person-sanctions.

    Реальна схема: searchKeys (name/code) + searchQuery + опційно sanctionList.
    Повертає міжнародні списки: РНБО, ЄС, Велика Британія, Канада, США (SDN/Non-SDN).
    """
    params: dict[str, Any] = {
        "searchKeys": search_keys,
        "searchQuery": query,
        "limit": limit,
    }
    if sanction_lists:
        params["sanctionList"] = ",".join(sanction_lists)
    logger.info("opendatabot get_person_sanctions: %r (keys=%s)", query, search_keys)
    return _request("person-sanctions", params=params)


# Критичні санкційні списки для KYC (порядок = пріоритет показу).
SANCTION_LISTS_CRITICAL = [
    "РНБО",
    "Санкційний список США (SDN)",
    "Санкційний список ЄС",
    "Санкційний список Великобританії",
    "Санкційний список Канади",
    "Санкційний список США (Non-SDN)",
]


def screen_person_sanctions(query: str,
                            search_keys: str = "name") -> dict[str, Any]:
    """LIST-COMPLETE санкційний скринінг фізособи.

    КРИТИЧНО: запитує КОЖЕН критичний список ОКРЕМО, щоб жоден не випав за
    межі вікна limit (інакше OFAC/SDN тихо губиться серед РНБО-записів).
    """
    all_hits: list[dict[str, Any]] = []
    checked: list[str] = []
    matched: list[str] = []
    last_error = None

    for slist in SANCTION_LISTS_CRITICAL:
        env = get_person_sanctions(query, search_keys=search_keys,
                                   sanction_lists=[slist], limit=50)
        checked.append(slist)
        if env.get("error"):
            last_error = env["error"]
            continue
        data = env.get("data") or {}
        items = data.get("data") if isinstance(data, dict) else None
        if items is None and isinstance(data, list):
            items = data
        items = items or []
        if items:
            matched.append(slist)
            for it in items:
                if isinstance(it, dict):
                    all_hits.append({
                        "name": it.get("name"),
                        "code": it.get("code"),
                        "sanction_list": it.get("sanctionList") or slist,
                        "decree": it.get("decree"),
                        "reason": it.get("sanctionReason"),
                        "start_date": it.get("startDate"),
                        "end_date": it.get("endDate"),
                        "citizenship": it.get("citizenship"),
                        "birth_date": it.get("birthDate"),
                        "details": it.get("sanctionDetails"),
                    })

    return {
        "ok": True,
        "error": last_error if not all_hits and last_error else None,
        "data": {
            "checked_lists": checked,
            "matched_lists": matched,
            "hits": all_hits,
        },
    }


def get_pep(pib: str) -> dict[str, Any]:
    """Пошук національних публічних діячів (PEP) за ПІБ. Ендпоінт /pep.

    Повертає isPep, events (місце роботи/посада) та relatives (родичі PEP).
    Параметр — pib (ПІБ повністю).
    """
    logger.info("opendatabot get_pep: %r", pib)
    return _request("pep", params={"pib": pib})


def get_foreign_owned_companies(founder_type: str = "ruFounders",
                                limit: int = 50, offset: int = 0,
                                status: str | None = None) -> dict[str, Any]:
    """Компанії з іноземними власниками. Ендпоінт /companies/{founder}.

    УВАГА: НЕ повертає компанії конкретної особи. Параметр founder приймає
    лише ruFounders / byFounders / irFounders — масовий RF/BY-список.
    Може бути недоступний на тарифі (HTTP 403) → tariff_gated=True.
    """
    assert founder_type in ("ruFounders", "byFounders", "irFounders")
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    logger.info("opendatabot get_foreign_owned_companies: %s", founder_type)
    env = _request(f"companies/{founder_type}", params=params)
    if env.get("status_code") == 403:
        env["tariff_gated"] = True
    return env
