"""KYC-оркестратор — єдина точка входу для повної перевірки суб'єкта.

Зшиває всі джерела під ОДНИМ slug і генерує фінальний звіт:

  вхід (ЄДРПОУ / РНОКПП / назва)
    → визначення типу + резолвінг назви (рівень 2+3)
    → Worker 1 (реєстр компанії або фізособи)
    → Worker 2 (санкційний скринінг)
    → Worker 3 (adverse media)            [за замовчуванням, --no-media вимикає]
    → aggregate(slug) → JSON + HTML + PDF + граф + evidence ZIP

Запуск синхронний (послідовний, без Celery-черги) — так працює і локально,
і в Docker. Celery-воркери лишаються для production-режиму (масштабування).

Чесна деградація: збій будь-якого джерела не валить прогін — він
відображається у покритті джерел звіту (coverage), а не зупиняє перевірку.

Використання:
    python -m workers.run_kyc 14360570
    python -m workers.run_kyc "ПриватБанк"
    python -m workers.run_kyc 1234567890 --person --name "Шевченко О. В."
    python -m workers.run_kyc 14360570 --no-media
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
from typing import Any

logger = logging.getLogger("run_kyc")


def _looks_like_edrpou(s: str) -> bool:
    """ЄДРПОУ — 8 цифр (інколи 6). РНОКПП — 10 цифр."""
    digits = re.sub(r"\D", "", s)
    return s.strip().isdigit() and 6 <= len(digits) <= 8


def _looks_like_rnokpp(s: str) -> bool:
    digits = re.sub(r"\D", "", s)
    return s.strip().isdigit() and len(digits) == 10


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e",
    "є": "ye", "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "yi", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "shch", "ь": "", "ю": "yu", "я": "ya", "ё": "e",
    "ы": "y", "э": "e", "ъ": "",
}


def _person_slug(rnokpp: str | None, name: str | None) -> str:
    """Унікальний slug особи. РНОКПП у пріоритеті (стабільний ID); якщо його
    немає — транслітероване ім'я + короткий хеш (щоб різні особи не колізували
    в одній теці, що затирало б evidence)."""
    if rnokpp:
        return f"person_{rnokpp}"
    base = (name or "unknown").lower().strip()
    translit = "".join(_TRANSLIT.get(ch, ch) for ch in base)
    translit = re.sub(r"[^a-z0-9]+", "_", translit).strip("_")[:40] or "unknown"
    suffix = hashlib.md5((name or "").encode("utf-8")).hexdigest()[:6]
    return f"person_{translit}_{suffix}"


def run_kyc(
    subject: str,
    is_person: bool = False,
    full_name: str | None = None,
    with_media: bool = True,
) -> dict[str, Any]:
    """Повна KYC-перевірка суб'єкта. Повертає summary зі шляхом до звіту."""
    from workers.aggregator import aggregate
    from workers.input_resolver import resolve_company

    result: dict[str, Any] = {
        "input": subject, "slug": None, "subject_type": None,
        "resolved": None, "report": None, "steps": {}, "error": None,
    }

    edrpou: str | None = None
    rnokpp: str | None = None

    if is_person or _looks_like_rnokpp(subject):
        # РНОКПП тільки якщо вхід — це справді 10 цифр; інакше особа за ім'ям.
        if _looks_like_rnokpp(subject):
            rnokpp = re.sub(r"\D", "", subject)
            person_name = full_name
        else:
            rnokpp = None
            person_name = full_name or subject
        slug = _person_slug(rnokpp, person_name)
        result["subject_type"] = "person"
    elif _looks_like_edrpou(subject):
        edrpou = re.sub(r"\D", "", subject)
        slug = f"company_{edrpou}"
        result["subject_type"] = "company"
    else:
        logger.info("Резолвінг назви → ЄДРПОУ: %r", subject)
        res = resolve_company(subject)
        result["resolved"] = res
        if res["status"] == "resolved":
            edrpou = res["edrpou"]
            slug = f"company_{edrpou}"
            result["subject_type"] = "company"
            logger.info("Резолвлено однозначно → %s", edrpou)
        elif res["status"] == "ambiguous":
            result["error"] = (
                f"Неоднозначний вхід: знайдено {res['count']} компаній за "
                f"назвою {subject!r}. Уточніть ЄДРПОУ. Перші кандидати: "
                + ", ".join(c["code"] for c in res["candidates"][:5])
            )
            logger.warning(result["error"])
            return result
        elif res["status"] == "not_found":
            result["error"] = f"Компанію не знайдено за назвою {subject!r}."
            logger.warning(result["error"])
            return result
        else:
            result["error"] = f"Помилка резолвінгу: {res.get('error')}"
            return result

    result["slug"] = slug
    logger.info("Slug перевірки: %s", slug)

    # Worker 1 — реєстр
    try:
        if result["subject_type"] == "person":
            from workers.person_task import person_task
            r1 = person_task(rnokpp or "", full_name=person_name)
        else:
            from workers.registry_task import registry_task
            r1 = registry_task(edrpou, is_edrpou=True)
        result["steps"]["registry"] = r1
        subject_name = r1.get("name") or full_name or subject
    except Exception as exc:
        logger.exception("Worker 1 (реєстр) впав")
        result["steps"]["registry"] = {"error": str(exc)}
        subject_name = full_name or subject

    # Worker 2 — санкції (той самий slug)
    try:
        from workers.sanctions_task import sanctions_task
        s_type = "person" if result["subject_type"] == "person" else "organization"
        r2 = sanctions_task(
            subject_label=subject_name,
            subject_type=s_type,
            slug=slug,
            reg_number=edrpou,
            tax_id=rnokpp,
        )
        result["steps"]["sanctions"] = r2
    except Exception as exc:
        logger.exception("Worker 2 (санкції) впав")
        result["steps"]["sanctions"] = {"error": str(exc)}

    # Worker 3 — adverse media (опційно, чесна деградація)
    if with_media:
        try:
            from workers.run_pipeline import run_adverse_media  # type: ignore
            r3 = run_adverse_media(subject_name, slug=slug)
            result["steps"]["adverse_media"] = r3
        except ImportError:
            logger.info("adverse-media гілка недоступна — пропускаю")
            result["steps"]["adverse_media"] = {"skipped": "module unavailable"}
        except Exception as exc:
            logger.warning("Worker 3 (медіа) впав: %s — деградую", exc)
            result["steps"]["adverse_media"] = {"error": str(exc)}

    # Aggregate → звіт
    try:
        summary = aggregate(slug, subject_label=subject_name)
        result["report"] = summary
        logger.info("Звіт готовий: %s — %s (%s/100)",
                    summary["subject"], summary["risk_level"],
                    summary["trust_score"])
    except Exception as exc:
        logger.exception("Aggregate впав")
        result["error"] = f"aggregate error: {exc}"

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="KYC Network Scout — повна перевірка суб'єкта")
    parser.add_argument("subject", help="ЄДРПОУ, РНОКПП або назва компанії")
    parser.add_argument("--person", action="store_true",
                        help="трактувати вхід як фізособу (РНОКПП)")
    parser.add_argument("--name", default=None,
                        help="ПІБ фізособи (для PEP-пошуку)")
    parser.add_argument("--no-media", action="store_true",
                        help="не запускати adverse-media гілку")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    out = run_kyc(args.subject, is_person=args.person,
                  full_name=args.name, with_media=not args.no_media)

    if out["error"]:
        print(f"\n❌ {out['error']}")
        return 1

    rep = out.get("report") or {}
    print("\n" + "=" * 60)
    print(f"  KYC ЗВІТ: {rep.get('subject', out['input'])}")
    print("=" * 60)
    print(f"  Тип:         {out['subject_type']}")
    print(f"  Trust score: {rep.get('trust_score')}/100")
    print(f"  Рівень:      {rep.get('risk_level')}")
    cov = rep.get("coverage") or {}
    print(f"  Покриття:    " + ", ".join(f"{k}={v}" for k, v in cov.items()))
    print(f"  Звіт:        {rep.get('report_dir')}")
    arts = rep.get("artifacts") or {}
    print(f"  Артефакти:   " + ", ".join(f"{k}" for k, v in arts.items() if v))
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
