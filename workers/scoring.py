"""Entity resolution + скоринг надійності — серце агрегатора.

Дві задачі:

1. ENTITY RESOLUTION — дедуплікація осіб/компаній, що приходять із різних
   джерел (реєстр, санкції, adverse media) під різними написаннями.
   Скоринг подібності за специфікацією:
       ІПН/ЄДРПОУ збіг   +100
       DOB збіг          +40
       адреса збіг       +20
       імʼя (схоже)      +15  (масштабується за similarity 0..1)
   Рішення:
       score > 100  → auto-merge
       score < 20   → auto-split (різні сутності)
       20..100      → manual review (сіра зона)
   АСИМЕТРИЧНИЙ ПРИНЦИП: хибне злиття небезпечніше за хибний розкол.
   У сірій зоні та при будь-якому сумніві — НЕ зливаємо.

2. TRUST SCORING — інтегральний скоринг ризику контрагента на основі
   санкційних збігів, негативних медіа та повноти реєстрових даних.
   Вихід: 0..100 (вище = надійніше) + рівень ризику + список red flags.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

# ── ваги скорингу подібності (специфікація користувача) ──
W_ID = 100      # ІПН/ЄДРПОУ
W_DOB = 40      # дата народження
W_ADDRESS = 20  # адреса
W_NAME = 15     # імʼя (макс, масштабується за схожістю)

MERGE_THRESHOLD = 100   # > → auto-merge
SPLIT_THRESHOLD = 20    # < → auto-split
# 20..100 → manual review


# ── нормалізація ────────────────────────────────────────────────────────────

def normalize_name(name: str | None) -> str:
    """Нормалізує імʼя для порівняння: lowercase, без діакритики,
    впорядковані токени (щоб 'Іван Петренко' == 'Петренко Іван')."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    tokens = sorted(t for t in s.split() if len(t) > 1)
    return " ".join(tokens)


def normalize_id(code: str | None) -> str:
    """Лишає тільки цифри з ІПН/ЄДРПОУ."""
    if not code:
        return ""
    return "".join(ch for ch in str(code) if ch.isdigit())


def normalize_address(addr: str | None) -> str:
    if not addr:
        return ""
    s = unicodedata.normalize("NFKD", str(addr)).lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ── сутність для резолюції ───────────────────────────────────────────────────

@dataclass
class Entity:
    """Кандидат на резолюцію (особа або організація)."""
    name: str | None = None
    code: str | None = None          # ІПН або ЄДРПОУ
    dob: str | None = None           # YYYY-MM-DD
    address: str | None = None
    country: str | None = None
    role: str | None = None
    sources: set[str] = field(default_factory=set)

    def norm_name(self) -> str:
        return normalize_name(self.name)

    def norm_code(self) -> str:
        return normalize_id(self.code)

    def norm_address(self) -> str:
        return normalize_address(self.address)


@dataclass
class MatchResult:
    score: float
    decision: str   # "merge" | "split" | "review"
    breakdown: dict[str, float]


def similarity_score(a: Entity, b: Entity) -> MatchResult:
    """Рахує score подібності двох сутностей за специфікацією.

    Асиметрія закладена в decision: сіра зона → 'review' (НЕ merge)."""
    breakdown: dict[str, float] = {}
    score = 0.0

    ca, cb = a.norm_code(), b.norm_code()
    if ca and cb:
        if ca == cb:
            score += W_ID
            breakdown["id"] = W_ID
        else:
            breakdown["id_conflict"] = -W_ID
            score -= W_ID

    if a.dob and b.dob:
        if str(a.dob).strip() == str(b.dob).strip():
            score += W_DOB
            breakdown["dob"] = W_DOB
        else:
            breakdown["dob_conflict"] = -W_DOB
            score -= W_DOB

    aa, ab = a.norm_address(), b.norm_address()
    if aa and ab:
        addr_sim = fuzz.token_sort_ratio(aa, ab) / 100.0
        if addr_sim >= 0.85:
            score += W_ADDRESS
            breakdown["address"] = W_ADDRESS
        elif addr_sim >= 0.6:
            partial = round(W_ADDRESS * addr_sim, 1)
            score += partial
            breakdown["address_partial"] = partial

    na, nb = a.norm_name(), b.norm_name()
    name_identical = False
    if na and nb:
        name_sim = fuzz.token_sort_ratio(na, nb) / 100.0
        name_pts = round(W_NAME * name_sim, 1)
        score += name_pts
        breakdown["name"] = name_pts
        name_identical = name_sim >= 0.99

    has_conflict = (
        breakdown.get("id_conflict") is not None
        or breakdown.get("dob_conflict") is not None
    )
    if score > MERGE_THRESHOLD:
        decision = "merge"
    elif has_conflict:
        decision = "split"
    elif name_identical and score < SPLIT_THRESHOLD:
        decision = "review"
    elif score < SPLIT_THRESHOLD:
        decision = "split"
    else:
        decision = "review"

    return MatchResult(score=round(score, 1), decision=decision,
                       breakdown=breakdown)


def resolve_entities(entities: list[Entity]) -> list[dict[str, Any]]:
    """Дедуплікує список сутностей жадібним кластеруванням.

    При merge — зливає; у сірій зоні (review) — НЕ зливає (асиметрія),
    але позначає needs_review."""
    clusters: list[dict[str, Any]] = []

    for ent in entities:
        best_idx = -1
        best: MatchResult | None = None

        for idx, cl in enumerate(clusters):
            rep: Entity = cl["representative"]
            res = similarity_score(ent, rep)
            if best is None or res.score > best.score:
                best, best_idx = res, idx

        if best is not None and best.decision == "merge":
            cl = clusters[best_idx]
            cl["members"].append(_entity_dict(ent))
            cl["sources"] |= ent.sources
            rep = cl["representative"]
            for f in ("code", "dob", "address", "country", "role"):
                if not getattr(rep, f) and getattr(ent, f):
                    setattr(rep, f, getattr(ent, f))
        else:
            review_flag = (best is not None and best.decision == "review")
            clusters.append({
                "representative": ent,
                "members": [_entity_dict(ent)],
                "sources": set(ent.sources),
                "needs_review": review_flag,
                "review_score": best.score if (review_flag and best) else None,
            })

    out: list[dict[str, Any]] = []
    for cl in clusters:
        rep: Entity = cl["representative"]
        out.append({
            **_entity_dict(rep),
            "sources": sorted(cl["sources"]),
            "member_count": len(cl["members"]),
            "needs_review": cl["needs_review"],
            "review_score": cl["review_score"],
        })
    return out


def _entity_dict(e: Entity) -> dict[str, Any]:
    return {
        "name": e.name, "code": e.code, "dob": e.dob,
        "address": e.address, "country": e.country, "role": e.role,
    }


# ── trust scoring ────────────────────────────────────────────────────────────

# Початковий бал — презумпція надійності, далі віднімаємо за ризики.
_BASE_TRUST = 100

# Штрафи за severity негативних медіа (Worker 3).
_SEVERITY_PENALTY = {"critical": 40, "high": 25, "medium": 12, "low": 4}

# Регуляторні стягнення (НБУ/АМКУ): зважений штраф за свіжістю/типом/тематикою.
_REG_CAP = 20            # стеля сумарного штрафу (ніколи не блокуюче)
_REG_FINE_BASE = 8       # грошовий штраф
_REG_WARNING_BASE = 3    # застереження/припис (без суми)
_REG_TOPIC_BONUS = 2     # AML / санкційна / фінансова тематика
_REG_TOPIC_KEYWORDS = ("відмив", "aml", "фінанс", "санкц", "терор", "пвк")


def _years_ago(date_str: str | None) -> float | None:
    """Скільки років тому подія. None, якщо дата невідома/непарситься."""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y"):
        try:
            d = datetime.strptime(str(date_str)[:10], fmt).replace(
                tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - d
            return delta.days / 365.25
        except (ValueError, TypeError):
            continue
    return None


def _freshness_mult(years: float | None) -> float:
    """Множник свіжості: свіже важить повну вагу, старе — менше."""
    if years is None:
        return 0.6
    if years < 1:
        return 1.0
    if years < 3:
        return 0.5
    return 0.25


def _regulatory_penalty(regulatory: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """Зважений штраф за регуляторну історію (НБУ/АМКУ).

    Кожне стягнення зважується: база (штраф/застереження) + тематичний бонус,
    помножені на множник свіжості. Сума обмежена _REG_CAP — регуляторка
    ніколи не блокуюча (на відміну від санкції).
    """
    total = 0.0
    descs: list[str] = []
    for item in regulatory:
        sub_items = item.get("items") or [item]
        for s in sub_items:
            if not isinstance(s, dict):
                continue
            amount = s.get("amount")
            reason = (s.get("reason") or s.get("text") or "").lower()
            date = s.get("date") or s.get("start_date")

            base = _REG_FINE_BASE if amount else _REG_WARNING_BASE
            if any(kw in reason for kw in _REG_TOPIC_KEYWORDS):
                base += _REG_TOPIC_BONUS

            years = _years_ago(date)
            weight = base * _freshness_mult(years)
            total += weight

            when = f"{date}" if date else "дата невідома"
            amt = f", {amount:,.0f} грн" if amount else ""
            descs.append(f"{when}{amt}: {(s.get('reason') or s.get('sanction') or 'стягнення')[:60]}")

    penalty = -min(_REG_CAP, round(total))
    return penalty, descs


def compute_trust_score(
    sanctions: dict[str, Any] | None,
    adverse_media: list[dict[str, Any]] | None,
    registry: dict[str, Any] | None,
    coverage: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Інтегральний скоринг надійності 0..100 + рівень ризику + red flags
    + audit_trail (покроковий розклад балів для відтворюваності).

    coverage: словник покриття джерел {registry, sanctions, ...} зі статусами
      checked/not_found/skipped/error. Якщо реєстр АБО санкції не "checked" —
      рівень не може бути LOW (стає INCONCLUSIVE): неповна перевірка не дає
      підстав назвати контрагента надійним.
    """
    score = _BASE_TRUST
    red_flags: list[str] = []
    # audit trail: кожен запис — (крок, дельта, бал_після, джерело).
    trail: list[dict[str, Any]] = [
        {"step": "Початковий бал (презумпція надійності)",
         "delta": 0, "score_after": score, "source": "—"}
    ]

    def _apply(reason: str, delta: int, source: str, flag: str | None = None):
        nonlocal score
        score += delta
        trail.append({"step": reason, "delta": delta,
                      "score_after": max(0, min(100, score)), "source": source})
        if flag:
            red_flags.append(flag)

    # 1. Санкції (OpenSanctions).
    if sanctions and sanctions.get("has_sanctions_match"):
        n = sanctions.get("total_hits", 0)
        lists = sanctions.get("matched_lists") or []
        lists_str = ", ".join(lists) if lists else "санкційні списки"
        _apply(f"Санкційний збіг: {n} запис(ів) у [{lists_str}]",
               -60, "Opendatabot (санкції)",
               f"Санкційний збіг: {n} запис(ів). Списки: {lists_str}.")

    # 2. Негативні медіа. Рахуємо ЛИШЕ згадки про самого субʼєкта (не
    #    однофамільця: is_about_target_person) і лише з достатньою впевненістю
    #    атрибуції (match_confidence high/medium). Інакше негативна стаття про
    #    тезку безпідставно знижувала б оцінку — критичний false-positive.
    media = adverse_media or []
    relevant = [m for m in media
                if m.get("is_adverse") and m.get("is_about_target_person")]
    scored = [m for m in relevant
              if (m.get("match_confidence") or "").lower() in ("high", "medium")]
    if scored:
        by_sev: dict[str, int] = {}
        total_pen = 0
        for m in scored:
            sev = (m.get("severity") or "low").lower()
            pen = _SEVERITY_PENALTY.get(sev, 4)
            total_pen += pen
            by_sev[sev] = by_sev.get(sev, 0) + 1
        _apply("Негативні медіа-згадки", -total_pen, "adverse_media",
               "Негативні згадки у медіа: "
               + ", ".join(f"{k}×{v}" for k, v in sorted(by_sev.items())))

    # 3. Реєстр.
    registry_critical = False
    registry_pep = False
    if registry:
        if registry.get("error") or not registry.get("raw_available"):
            _apply("Реєстрові дані недоступні/неповні", -10, "registry",
                   "Реєстрові дані недоступні/неповні (непрозорість).")
        elif not registry.get("beneficiaries"):
            _apply("Бенефіціари не розкриті", -5, "registry",
                   "Бенефіціари не розкриті в реєстрі.")

        rs = registry.get("risk_signals") or {}
        for c in rs.get("critical", []):
            ctype = c.get("type")
            registry_critical = True
            label = {
                "ruFounders": "🇷🇺 Власники з РФ (реєстр)",
                "byFounders": "🇧🇾 Власники з РБ (реєстр)",
                "irFounders": "🇮🇷 Власники з Ірану (реєстр)",
                "sanction": "Санкція у реєстрі",
                "nbuSanctions": "Санкція НБУ",
                "bankruptcy": "Процедура банкрутства",
                "warTerritory": "Реєстрація на території бойових дій",
            }.get(ctype, ctype)
            txt = c.get("text") or ""
            _apply(f"Критичний сигнал реєстру: {label}", -30, "registry (factors)",
                   f"{label}: {txt}".strip(": "))
        # Регуляторна історія (стягнення НБУ/АМКУ) — НЕ санкція, не блокуюче.
        reg_items = rs.get("regulatory") or []
        if reg_items:
            pen, descs = _regulatory_penalty(reg_items)
            n = len(descs)
            _apply(f"Регуляторна історія: {n} стягнення (НБУ/АМКУ)",
                   pen, "registry (regulatory)",
                   "Регуляторні стягнення (адмін. штрафи/застереження, "
                   "не санкційний список): " + "; ".join(descs[:5]))

        for p in rs.get("pep", []):
            registry_pep = True
            names = ", ".join(p.get("names") or [])
            trail.append({"step": f"PEP-зв'язок: {p.get('text')}", "delta": 0,
                          "score_after": max(0, min(100, score)),
                          "source": "registry (PEP)"})
            red_flags.append(f"PEP-зв'язок: {p.get('text')}"
                             + (f" ({names})" if names else ""))
        for w in rs.get("warnings", []):
            _apply(f"Реєстр: {w.get('text') or w.get('type')}", -5,
                   "registry (factors)",
                   f"Реєстр: {w.get('text') or w.get('type')}")
    else:
        _apply("Реєстрова перевірка не виконувалась", -8, "—",
               "Реєстрова перевірка не виконувалась.")

    score = max(0, min(100, score))

    # Базовий рівень за балом.
    if score >= 70:
        risk = "LOW"
    elif score >= 40:
        risk = "MEDIUM"
    else:
        risk = "HIGH"

    level_reason = (f"Оцінка ризику {100 - score}/100 → {risk} "
                    f"(LOW ≤30 · MEDIUM ≤60 · HIGH >60)")

    # Ескалація до HIGH.
    if (sanctions and sanctions.get("has_sanctions_match")) or registry_critical:
        risk = "HIGH"
        level_reason = "Ескалація до HIGH: санкційний збіг або критичний сигнал реєстру"
    elif registry_pep and risk == "LOW":
        risk = "MEDIUM"
        level_reason = "Ескалація до MEDIUM: PEP-зв'язок"

    # INCONCLUSIVE: LOW можливий лише якщо реєстр І санкції перевірені.
    inconclusive_reason = None
    if risk == "LOW" and coverage is not None:
        reg_ok = coverage.get("registry") == "checked"
        san_ok = coverage.get("sanctions") == "checked"
        if not (reg_ok and san_ok):
            missing = []
            if not reg_ok:
                missing.append(f"реєстр ({coverage.get('registry')})")
            if not san_ok:
                missing.append(f"санкції ({coverage.get('sanctions')})")
            risk = "INCONCLUSIVE"
            inconclusive_reason = (
                "Неповна перевірка — не можна підтвердити надійність. "
                "Не перевірено: " + ", ".join(missing)
            )
            level_reason = inconclusive_reason
            red_flags.insert(0, "⚠ " + inconclusive_reason)

    return {
        "trust_score": score,
        "risk_level": risk,
        "red_flags": red_flags,
        "audit_trail": trail,
        "level_reason": level_reason,
        "inconclusive": inconclusive_reason is not None,
    }
