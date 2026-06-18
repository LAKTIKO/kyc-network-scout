"""Aggregator — зшиває Worker 1 (реєстр компанії), Worker 1-особи,
Worker 2 (санкції) та Worker 3 (adverse media) в єдиний KYC-звіт.

Pipeline:
  data/normalized/{slug}/  →  entity resolution  →  trust scoring
    →  граф звʼязків (networkx+pyvis)
    →  kyc_report.json + report.html + report.pdf
    →  evidence ZIP усіх первинних джерел
  усе складається в  output/YYYY-MM-DD_HH-MM-SS/

Підтримує і компанії (registry.json з ЄДРПОУ), і фізосіб (registry.json з
РНОКПП). Звіт чесно показує покриття джерел (що перевірено / що ні),
відрізняє "не знайдено" від "чисто".
"""

from __future__ import annotations

import json
import logging
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workers.scoring import Entity, compute_trust_score, resolve_entities

logger = logging.getLogger(__name__)

_OUTPUT_BASE = Path(os.getenv("OUTPUT_DIR", "data"))
_REPORT_BASE = Path(os.getenv("REPORT_DIR", "output"))


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("cannot read %s: %s", path, exc)
        return None


def _load_adverse_media(slug: str) -> list[dict[str, Any]]:
    norm_dir = _OUTPUT_BASE / "normalized" / slug
    if not norm_dir.exists():
        return []
    items: list[dict[str, Any]] = []
    for p in norm_dir.glob("*.json"):
        if p.name in ("registry.json", "sanctions.json", "kyc_report.json"):
            continue
        data = _load_json(p)
        if data:
            items.append(data)
    return items


def _collect_entities(registry: dict[str, Any] | None,
                      sanctions: dict[str, Any] | None) -> list[Entity]:
    ents: list[Entity] = []
    if registry:
        if registry.get("name"):
            ents.append(Entity(
                name=registry.get("name"),
                code=registry.get("edrpou") or registry.get("rnokpp"),
                dob=registry.get("birth_date"),
                address=registry.get("address"),
                role="subject",
                sources={"registry"},
            ))
        director = registry.get("director")
        if director:
            ents.append(Entity(
                name=director if isinstance(director, str) else director.get("name"),
                role="director", sources={"registry"}))
        for b in registry.get("beneficiaries") or []:
            ents.append(Entity(
                name=b.get("name"), code=b.get("code"),
                country=b.get("country"),
                role=b.get("role") or "beneficiary", sources={"registry"}))
    if sanctions:
        for h in sanctions.get("hits") or []:
            if h.get("name"):
                ents.append(Entity(
                    name=h.get("name"), code=h.get("code"),
                    role="sanctioned_entity",
                    sources={"sanctions"}))
    return ents


def _source_coverage(registry: dict[str, Any] | None,
                     sanctions: dict[str, Any] | None,
                     adverse: list[dict[str, Any]]) -> dict[str, str]:
    """Чесне покриття джерел: checked / not_found / unavailable / skipped.

    Розрізняє "не знайдено" і "не перевірено" — критично, щоб "чистий"
    звіт не означав "половина джерел не відповіла"."""
    cov: dict[str, str] = {}

    if registry is None:
        cov["registry"] = "skipped"
    elif registry.get("not_found"):
        cov["registry"] = "not_found"
    elif registry.get("error"):
        cov["registry"] = "error"
    elif not registry.get("raw_available"):
        cov["registry"] = "not_found"
    else:
        cov["registry"] = "checked"

    if sanctions is None:
        cov["sanctions"] = "skipped"
    elif sanctions.get("error"):
        cov["sanctions"] = "error"
    else:
        cov["sanctions"] = "checked"

    cov["adverse_media"] = "checked" if adverse else "skipped"

    if registry and registry.get("subject_type") == "person":
        cov["related_companies"] = registry.get(
            "related_companies_status", "unavailable_via_api")
    return cov


def _build_graph(slug, registry, resolved, report_dir):
    try:
        import networkx as nx
        from pyvis.network import Network
        G = nx.DiGraph()
        center = (registry or {}).get("name") or slug
        G.add_node(center, group="company", title="Контрагент")
        for ent in resolved:
            role = ent.get("role") or "related"
            name = ent.get("name")
            if not name or name == center:
                continue
            group = {"director": "director", "beneficiary": "beneficiary",
                     "sanctioned_entity": "sanction"}.get(role, "related")
            flags = []
            if ent.get("needs_review"):
                flags.append("⚠ ручна перевірка")
            if "sanctions" in (ent.get("sources") or []):
                flags.append("🚫 санкція")
            title = role + (" | " + "; ".join(flags) if flags else "")
            G.add_node(name, group=group, title=title)
            G.add_edge(center, name, title=role)
        net = Network(height="600px", width="100%", directed=True,
                      bgcolor="#ffffff", font_color="#222")
        net.from_nx(G)
        net.set_options('{"physics":{"barnesHut":{"gravitationalConstant":-8000}}}')
        gf = report_dir / "graph.html"
        # pyvis пише vis-network lib/ ВІДНОСНО cwd (за замовч. /app, куди
        # appuser не має прав запису → Permission denied). Тимчасово
        # переходимо в report_dir, де права є, і зберігаємо локальним ім'ям.
        prev_cwd = os.getcwd()
        try:
            os.chdir(report_dir)
            net.save_graph("graph.html")
        finally:
            os.chdir(prev_cwd)
        return gf.name
    except Exception as exc:
        logger.warning("graph build failed: %s", exc)
        return None


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="uk"><head><meta charset="utf-8">
<title>KYC звіт — {{ subject }}</title>
<style>
  body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#1a1a1a;
       max-width:900px;margin:40px auto;padding:0 24px}
  h1{font-size:24px;border-bottom:2px solid #333;padding-bottom:8px}
  h2{font-size:18px;margin-top:28px}
  .risk{display:inline-block;padding:6px 16px;border-radius:6px;font-weight:700;
        color:#fff;font-size:18px}
  .risk.LOW{background:#2e7d32}.risk.MEDIUM{background:#ed6c02}
  .risk.HIGH{background:#c62828}.risk.INCONCLUSIVE{background:#607d8b}
  .score{font-size:40px;font-weight:800}
  .flag{background:#fff3e0;border-left:4px solid #ed6c02;padding:8px 12px;margin:6px 0}
  table{border-collapse:collapse;width:100%;margin:12px 0}
  th,td{border:1px solid #ddd;padding:8px 10px;text-align:left;font-size:14px}
  th{background:#f5f5f5}
  .muted{color:#777;font-size:13px}
  .disclaimer{background:#e3f2fd;border:1px solid #2196f3;padding:10px 14px;
              border-radius:4px;margin:12px 0;font-size:13px}
  .cov-checked{color:#2e7d32}.cov-not_found{color:#c62828}
  .cov-skipped,.cov-unavailable_via_api,.cov-error{color:#999}
  .review{color:#ed6c02;font-weight:600}
  .trail{font-size:13px}.trail td{padding:4px 8px}
  .neg{color:#c62828}
</style></head><body>
  <h1>KYC Due Diligence — {{ subject }}</h1>
  <p class="muted">Згенеровано: {{ generated_at }} · KYC Network Scout
     {% if subject_type %}· тип: {{ subject_type }}{% endif %}</p>

  <div><span class="score">{{ trust_score }}/100</span> &nbsp;
       <span class="risk {{ risk_level }}">{{ risk_level }}{% if risk_level != "INCONCLUSIVE" %} RISK{% endif %}</span></div>

  <p class="muted">{{ level_reason }}</p>

  <div class="disclaimer">ℹ Цей звіт — <strong>допоміжний інструмент аналітика</strong>,
     а не автоматичний вердикт. Остаточне KYC-рішення приймає людина на
     основі цих даних та власного судження.</div>

  <h2>Покриття джерел</h2>
  <table><tr><th>Джерело</th><th>Статус</th></tr>
  {% for src, st in coverage.items() %}
    <tr><td>{{ src }}</td><td class="cov-{{ st }}">{{ st }}</td></tr>
  {% endfor %}
  </table>
  <p class="muted">«checked» — перевірено; «not_found» — суб'єкт не знайдено
     в джерелі (НЕ означає «чисто»); «skipped/unavailable» — не перевірялось.</p>

  {% if red_flags %}
  <h2>🚩 Red flags</h2>
  {% for f in red_flags %}<div class="flag">{{ f }}</div>{% endfor %}
  {% else %}<h2>Red flags</h2><p>Тривожних сигналів не виявлено
     {% if has_gaps %}(втім, частина джерел не перевірена — див. покриття){% endif %}.</p>
  {% endif %}

  <h2>Розклад балів (audit trail)</h2>
  <p class="muted">Як сформувався бал {{ trust_score }}/100 — крок за кроком,
     із зазначенням джерела кожного фактора (для відтворюваності рішення).</p>
  <table class="trail"><tr><th>Крок</th><th>Δ</th><th>Бал</th><th>Джерело</th></tr>
  {% for t in audit_trail %}
    <tr><td>{{ t.step }}</td>
        <td class="{% if t.delta < 0 %}neg{% endif %}">{% if t.delta != 0 %}{{ t.delta }}{% else %}—{% endif %}</td>
        <td>{{ t.score_after }}</td><td>{{ t.source }}</td></tr>
  {% endfor %}
  </table>
  <p class="muted"><strong>Підсумок:</strong> {{ level_reason }}</p>

  <h2>Дані суб'єкта</h2>
  {% if registry and registry.raw_available %}
  <table>
    <tr><th>Назва / ПІБ</th><td>{{ registry.name or "—" }}</td></tr>
    {% if registry.edrpou %}<tr><th>ЄДРПОУ</th><td>{{ registry.edrpou }}</td></tr>{% endif %}
    {% if registry.rnokpp %}<tr><th>РНОКПП</th><td>{{ registry.rnokpp }}</td></tr>{% endif %}
    {% if registry.birth_date %}<tr><th>Дата народження</th><td>{{ registry.birth_date }}</td></tr>{% endif %}
    <tr><th>Статус</th><td>{{ registry.status or "—" }}</td></tr>
    <tr><th>Адреса</th><td>{{ registry.address or "—" }}</td></tr>
    {% if registry.director %}<tr><th>Керівник</th><td>{{ registry.director }}</td></tr>{% endif %}
    {% if registry.is_pep %}<tr><th>PEP</th><td>Так — публічний діяч</td></tr>{% endif %}
    {% if data_as_of %}<tr><th>Остання зміна в реєстрі</th><td>{{ data_as_of }}</td></tr>{% endif %}
    {% if registry.edrpou %}<tr><th>Джерело</th><td><a href="https://opendatabot.ua/c/{{ registry.edrpou|e }}" target="_blank" rel="noopener">Opendatabot · профіль компанії</a></td></tr>
    {% elif registry.source_url %}<tr><th>Джерело</th><td><a href="{{ registry.source_url|e }}" target="_blank" rel="noopener">{{ registry.source or "opendatabot" }}</a></td></tr>{% endif %}
  </table>
  {% elif coverage.registry == "not_found" %}
  <p class="flag">Суб'єкта НЕ знайдено в реєстрі. Це не означає «чисто» —
     можливо, іноземець, або неточний запит. Потрібна додаткова перевірка.</p>
  {% else %}<p class="muted">Реєстрова перевірка не виконувалась.</p>{% endif %}

  <h2>Санкційний скринінг</h2>
  {% if sanctions and sanctions.has_sanctions_match %}
    <p>⚠ <strong>Виявлено збіги</strong> у санкційних списках:
       {{ sanctions.matched_lists|join(", ") }}</p>
    <table><tr><th>Ім'я</th><th>Список</th><th>Підстава</th><th>Період</th></tr>
    {% for h in sanctions.hits %}
      <tr><td>{{ h.name or "—" }}</td>
          <td>{{ h.sanction_list or "—" }}</td>
          <td>{{ h.reason or h.decree or "—" }}</td>
          <td>{{ h.start_date or "" }}{% if h.end_date %} – {{ h.end_date }}{% endif %}</td></tr>
    {% endfor %}</table>
    <p class="muted">Перевірені списки: {{ sanctions.checked_lists|join(", ") }}</p>
  {% elif sanctions and sanctions.checked_lists %}
    <p>✓ Збігів не виявлено. Перевірені списки:
       {{ sanctions.checked_lists|join(", ") }}.</p>
  {% elif sanctions %}<p>✓ Збігів у санкційних списках не виявлено.</p>
  {% else %}<p class="muted">Санкційний скринінг не виконувався.</p>{% endif %}

  {% if resolved %}
  <h2>Корпоративна структура / пов'язані особи</h2>
  <table><tr><th>Ім'я</th><th>Роль</th><th>Код</th><th>Джерела</th><th>Статус</th></tr>
  {% for e in resolved %}
    <tr><td>{{ e.name or "—" }}</td><td>{{ e.role or "—" }}</td>
        <td>{{ e.code or "—" }}</td><td>{{ e.sources|join(", ") }}</td>
        <td>{% if e.needs_review %}<span class="review">manual review ({{ e.review_score }})</span>
            {% else %}✓{% endif %}</td></tr>
  {% endfor %}</table>
  {% if graph_file %}<p><a href="{{ graph_file }}">↗ Інтерактивний граф зв'язків</a></p>{% endif %}
  {% endif %}

  {% if adverse %}
  <h2>Негативні медіа-згадки</h2>
  <table><tr><th>Severity</th><th>Категорія</th><th>Резюме</th><th>Джерело</th></tr>
  {% for m in adverse if m.is_adverse %}
    <tr><td>{{ m.severity }}</td><td>{{ m.category }}</td><td>{{ m.summary or "" }}</td>
        <td>{% if m.url %}<a href="{{ m.url|e }}" target="_blank" rel="noopener">{{ m.title or "першоджерело" }}</a>{% else %}—{% endif %}</td></tr>
  {% endfor %}</table>
  <p class="muted">Посилання ведуть на першоджерела (відкриваються в новій
     вкладці). Повні копії статей на момент перевірки збережено в evidence-архіві.</p>
  {% endif %}

  <p class="muted">Усі первинні джерела збережено в evidence-архіві прогону.
     KYC Network Scout — автоматизований OSINT pipeline.</p>
</body></html>"""


def _render_html(context, report_dir):
    from jinja2 import Template
    html = Template(_HTML_TEMPLATE).render(**context)
    (report_dir / "report.html").write_text(html, encoding="utf-8")
    return html


def _render_pdf(html, report_dir):
    try:
        from weasyprint import HTML
        HTML(string=html).write_pdf(str(report_dir / "report.pdf"))
        return "report.pdf"
    except Exception as exc:
        logger.warning("PDF generation skipped: %s", exc)
        return None


def _build_evidence_zip(slug, report_dir):
    try:
        zip_path = report_dir / "evidence.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for sub in ("raw", "normalized"):
                d = _OUTPUT_BASE / sub / slug
                if not d.exists():
                    continue
                for f in d.rglob("*"):
                    if f.is_file():
                        zf.write(f, arcname=f"{sub}/{f.name}")
        return zip_path.name
    except Exception as exc:
        logger.warning("evidence zip failed: %s", exc)
        return None


def aggregate(slug: str, subject_label: str | None = None) -> dict[str, Any]:
    """Зшиває всі джерела по slug у фінальний KYC-звіт."""
    norm_dir = _OUTPUT_BASE / "normalized" / slug
    registry = _load_json(norm_dir / "registry.json")
    sanctions = _load_json(norm_dir / "sanctions.json")
    adverse = _load_adverse_media(slug)

    subject = (subject_label or (registry or {}).get("name")
               or (sanctions or {}).get("subject") or slug)
    subject_type = (registry or {}).get("subject_type")

    entities = _collect_entities(registry, sanctions)
    resolved = resolve_entities(entities)
    coverage = _source_coverage(registry, sanctions, adverse)
    scoring = compute_trust_score(sanctions, adverse, registry, coverage)

    data_as_of = (registry or {}).get("data_as_of")
    has_gaps = any(v in ("skipped", "error", "not_found")
                   for v in coverage.values())

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    report_dir = _REPORT_BASE / ts
    os.makedirs(report_dir, exist_ok=True)

    graph_file = _build_graph(slug, registry, resolved, report_dir)

    report = {
        "subject": subject, "subject_type": subject_type, "slug": slug,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trust_score": scoring["trust_score"],
        "risk_level": scoring["risk_level"],
        "level_reason": scoring["level_reason"],
        "inconclusive": scoring["inconclusive"],
        "red_flags": scoring["red_flags"],
        "audit_trail": scoring["audit_trail"],
        "source_coverage": coverage,
        "data_as_of": data_as_of,
        "registry": registry, "sanctions": sanctions,
        "resolved_entities": resolved,
        "adverse_media_count": len([m for m in adverse if m.get("is_adverse")]),
        "adverse_media": [
            {
                "url": m.get("url"),
                "title": m.get("title"),
                "severity": m.get("severity"),
                "category": m.get("category"),
                "summary": m.get("summary"),
                "match_confidence": m.get("match_confidence"),
                "source": "serper+classifier",
            }
            for m in adverse if m.get("is_adverse")
        ],
    }
    (report_dir / "kyc_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (norm_dir / "kyc_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    context = {
        "subject": subject, "subject_type": subject_type,
        "generated_at": report["generated_at"],
        "trust_score": scoring["trust_score"],
        "risk_level": scoring["risk_level"],
        "level_reason": scoring["level_reason"],
        "red_flags": scoring["red_flags"],
        "audit_trail": scoring["audit_trail"],
        "coverage": coverage, "has_gaps": has_gaps,
        "data_as_of": data_as_of,
        "registry": registry, "sanctions": sanctions,
        "resolved": resolved, "adverse": adverse, "graph_file": graph_file,
    }
    html = _render_html(context, report_dir)
    pdf_file = _render_pdf(html, report_dir)
    zip_file = _build_evidence_zip(slug, report_dir)

    summary = {
        "slug": slug, "subject": subject,
        "trust_score": scoring["trust_score"],
        "risk_level": scoring["risk_level"],
        "report_dir": str(report_dir),
        "coverage": coverage,
        "artifacts": {"json": "kyc_report.json", "html": "report.html",
                      "pdf": pdf_file, "graph": graph_file,
                      "evidence_zip": zip_file},
        "error": None,
    }
    logger.info("aggregate done: %s — %s (%d/100)", subject,
                scoring["risk_level"], scoring["trust_score"])
    return summary


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    slug = sys.argv[1] if len(sys.argv) > 1 else "company_14360570"
    print(json.dumps(aggregate(slug), ensure_ascii=False, indent=2))
