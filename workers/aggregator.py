"""Aggregator — зшиває Worker 1 (реєстр компанії), Worker 1-особи,
Worker 2 (санкції) та Worker 3 (adverse media) в єдиний KYC-звіт.

Pipeline:
  data/normalized/{slug}/  →  entity resolution  →  trust scoring
    →  граф звʼязків (networkx+pyvis)
    →  kyc_report.json + report.html
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
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workers.scoring import Entity, compute_trust_score, resolve_entities

logger = logging.getLogger(__name__)

# Абсолютні шляхи — щоб тимчасова зміна cwd в іншому потоці (граф, див.
# _GRAPH_LOCK) не перенаправила записи відносними шляхами кудись не туди.
_OUTPUT_BASE = Path(os.getenv("OUTPUT_DIR", "data")).resolve()
_REPORT_BASE = Path(os.getenv("REPORT_DIR", "output")).resolve()

# pyvis пише lib/ відносно cwd і вимагає os.chdir; cwd глобальний на процес,
# тож у багатопотоковому веб-режимі (батч) дві паралельні побудови графа
# затирали б cwd одна одній. Серіалізуємо цю секцію.
_GRAPH_LOCK = threading.Lock()


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
        # cwd глобальний на процес → серіалізуємо під _GRAPH_LOCK, щоб
        # паралельні перевірки (батч) не затирали cwd одна одній.
        with _GRAPH_LOCK:
            prev_cwd = os.getcwd()
            try:
                os.chdir(report_dir)
                net.save_graph("graph.html")
            finally:
                os.chdir(prev_cwd)
        # Робимо граф самодостатнім: інлайнимо utils.js (інакше при відкритті
        # звіту в новій вкладці / через blob вкладений lib/ не резолвиться і
        # граф не відкривається), і лагодимо зламаний pyvis CSS-URL (dist/dist).
        try:
            html = gf.read_text(encoding="utf-8")
            utils = report_dir / "lib" / "bindings" / "utils.js"
            if utils.exists():
                html = html.replace(
                    '<script src="lib/bindings/utils.js"></script>',
                    "<script>\n" + utils.read_text(encoding="utf-8") + "\n</script>")
            html = html.replace("dist/dist/vis-network.min.css",
                                "dist/vis-network.min.css")
            gf.write_text(html, encoding="utf-8")
        except Exception as exc:
            logger.warning("graph post-process skipped: %s", exc)
        return gf.name
    except Exception as exc:
        logger.warning("graph build failed: %s", exc)
        return None


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KYC звіт — {{ subject }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Onest:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root{
    --font:'Onest',-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --bg:#f1f5f9; --surface:#ffffff; --ink:#0f172a; --ink-soft:#475569;
    --muted:#94a3b8; --border:#e2e8f0; --border-soft:#eef2f6;
    --brand:#1d4ed8; --brand-deep:#0b1220; --brand-soft:#eff6ff;
    --low:#16a34a; --low-bg:#ecfdf5; --medium:#d97706; --medium-bg:#fffbeb;
    --high:#dc2626; --high-bg:#fef2f2; --inconclusive:#64748b; --inconc-bg:#f1f5f9;
    --radius:14px; --shadow:0 1px 2px rgba(15,23,42,.04),0 8px 24px rgba(15,23,42,.06);
  }
  *{box-sizing:border-box}
  body{font-family:var(--font);
       color:var(--ink);background:var(--bg);margin:0;
       font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
  .wrap{max-width:920px;margin:0 auto;padding:0 20px 64px}
  a{color:var(--brand);text-decoration:none}
  a:hover{text-decoration:underline}
  .muted{color:var(--ink-soft);font-size:13px}
  .tiny{font-size:12px;color:var(--muted)}

  /* ── Topbar ── */
  .topbar{background:var(--brand-deep);color:#fff;padding:18px 0;margin-bottom:28px}
  .topbar .wrap{padding-bottom:0;display:flex;align-items:center;justify-content:space-between;gap:16px}
  .brand{display:flex;align-items:center;gap:11px;font-weight:700;letter-spacing:.2px}
  .brand .logo{width:30px;height:30px;border-radius:8px;background:linear-gradient(135deg,#3b82f6,#1d4ed8);
       display:flex;align-items:center;justify-content:center;font-size:16px;flex:0 0 auto}
  .brand small{display:block;font-weight:500;color:#93a4c4;font-size:11px;letter-spacing:.3px}
  .topbar .meta{text-align:right;font-size:12px;color:#93a4c4;line-height:1.4}

  /* ── Hero / score card ── */
  .hero{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
        box-shadow:var(--shadow);padding:26px 28px;margin-bottom:22px;
        display:flex;gap:28px;align-items:center;flex-wrap:wrap}
  .hero .left{flex:1 1 320px;min-width:260px}
  .hero h1{font-size:23px;margin:0 0 4px;font-weight:700;line-height:1.25}
  .subtype{display:inline-block;font-size:11px;font-weight:600;text-transform:uppercase;
        letter-spacing:.6px;color:var(--brand);background:var(--brand-soft);
        padding:3px 9px;border-radius:999px;margin-bottom:10px}
  .hero .reason{color:var(--ink-soft);font-size:14px;margin:8px 0 0}
  .gauge{flex:0 0 auto;text-align:center;min-width:190px}
  .score{font-size:54px;font-weight:800;line-height:1;letter-spacing:-1px}
  .score span{font-size:20px;color:var(--muted);font-weight:600}
  .risk{display:inline-block;padding:7px 18px;border-radius:999px;font-weight:700;
        color:#fff;font-size:14px;letter-spacing:.4px;margin-top:12px}
  .risk.LOW{background:var(--low)}.risk.MEDIUM{background:var(--medium)}
  .risk.HIGH{background:var(--high)}.risk.INCONCLUSIVE{background:var(--inconclusive)}
  .bar{height:9px;border-radius:999px;background:var(--border);margin-top:16px;overflow:hidden}
  .bar > i{display:block;height:100%;border-radius:999px;background:var(--inconclusive)}
  .bar.LOW > i{background:var(--low)}
  .bar.MEDIUM > i{background:var(--medium)}
  .bar.HIGH > i{background:var(--high)}
  .bar.INCONCLUSIVE > i{background:var(--inconclusive)}
  .scalelbl{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:5px}

  /* ── Cards / sections ── */
  .card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
        box-shadow:var(--shadow);padding:20px 24px;margin-bottom:18px}
  h2{font-size:16px;margin:0 0 14px;font-weight:700;display:flex;align-items:center;gap:9px}
  h2::before{content:"";width:4px;height:18px;border-radius:3px;background:var(--brand);flex:0 0 auto}

  /* ── Callouts ── */
  .callout{border-radius:10px;padding:13px 16px;margin:0 0 14px;font-size:13.5px;
        border:1px solid transparent;display:flex;gap:10px;align-items:flex-start}
  .callout .ic{flex:0 0 auto;font-size:15px;line-height:1.4}
  .callout-info{background:var(--brand-soft);border-color:#bfdbfe;color:#1e3a8a}
  .callout-warn{background:var(--medium-bg);border-color:#fde68a;color:#92400e}
  .callout-danger{background:var(--high-bg);border-color:#fecaca;color:#991b1b}
  .callout-ok{background:var(--low-bg);border-color:#bbf7d0;color:#166534}

  /* ── Flags (ієрархія: critical / warning / context) ── */
  .flags{display:grid;gap:9px}
  .flagx{display:flex;gap:12px;align-items:flex-start;padding:11px 14px;border-radius:10px;
        border:1px solid var(--border);background:var(--surface)}
  .flagx .tag{flex:0 0 auto;font-size:10px;font-weight:800;text-transform:uppercase;
        letter-spacing:.5px;padding:4px 9px;border-radius:6px;margin-top:1px;white-space:nowrap}
  .flagx .txt{font-size:13.5px;color:var(--ink);line-height:1.5}
  .flag-high{border-left:4px solid var(--high);background:#fef6f6}
  .flag-high .tag{background:#fee2e2;color:#991b1b}
  .flag-med{border-left:4px solid var(--medium);background:#fffdf6}
  .flag-med .tag{background:#fef3c7;color:#92400e}
  .flag-info{border-left:4px solid var(--muted);background:#fafbfc}
  .flag-info .tag{background:#eef2f6;color:#64748b}
  .flag-info .txt{color:var(--ink-soft)}

  /* ── Tables ── */
  table{border-collapse:collapse;width:100%;margin:4px 0;font-size:13.5px}
  th,td{padding:9px 12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--border-soft)}
  thead th,table.kv th{color:var(--ink-soft);font-weight:600;font-size:12px;
        text-transform:uppercase;letter-spacing:.4px;background:#f8fafc}
  tbody tr:last-child td{border-bottom:none}
  table.kv th{width:200px;background:#f8fafc;text-transform:none;letter-spacing:0;font-size:13px}
  .trail td,.trail th{padding:6px 12px;font-size:12.5px}
  .neg{color:var(--high);font-weight:600}.pos{color:var(--low);font-weight:600}

  /* ── Badges ── */
  .badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;
        font-weight:700;text-transform:uppercase;letter-spacing:.3px;white-space:nowrap}
  .sev-critical{background:#fee2e2;color:#991b1b}.sev-high{background:#ffedd5;color:#9a3412}
  .sev-medium{background:#fef9c3;color:#854d0e}.sev-low{background:#e0f2fe;color:#075985}
  .cat{display:inline-block;padding:2px 9px;border-radius:6px;font-size:11px;font-weight:600;
        background:#f1f5f9;color:#475569}
  .pill{display:inline-flex;align-items:center;gap:5px;padding:3px 11px;border-radius:999px;
        font-size:12px;font-weight:600}
  .pill::before{content:"";width:7px;height:7px;border-radius:50%}
  .cov-checked{background:var(--low-bg);color:#166534}.cov-checked::before{background:var(--low)}
  .cov-not_found{background:var(--high-bg);color:#991b1b}.cov-not_found::before{background:var(--high)}
  .cov-skipped,.cov-unavailable_via_api,.cov-error,.cov-unavailable{
        background:var(--inconc-bg);color:#475569}
  .cov-skipped::before,.cov-unavailable_via_api::before,.cov-error::before,
  .cov-unavailable::before{background:var(--muted)}
  .review{color:var(--medium);font-weight:600}

  footer{text-align:center;color:var(--muted);font-size:12px;margin-top:30px;
        padding-top:18px;border-top:1px solid var(--border)}

  @media print{
    body{background:#fff;font-size:12px}
    .topbar{background:var(--brand-deep)!important;-webkit-print-color-adjust:exact;print-color-adjust:exact}
    .card,.hero{box-shadow:none;break-inside:avoid}
    .badge,.pill,.risk{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  }
</style></head><body>
  <div class="topbar"><div class="wrap">
    <div class="brand"><span class="logo">🛡️</span>
      <span>KYC Network Scout<small>OSINT Due-Diligence Engine</small></span></div>
    <div class="meta">Звіт згенеровано<br>{{ generated_at }}</div>
  </div></div>

  <div class="wrap">

  {% set risk_score = 100 - trust_score %}
  <div class="hero">
    <div class="left">
      {% if subject_type %}<span class="subtype">{{ "Компанія" if subject_type == "company" else "Фізична особа" }}</span>{% endif %}
      <h1>{{ subject }}</h1>
      <p class="reason">{{ level_reason }}</p>
      <div class="bar {{ risk_level }}"><i style="width:{{ risk_score if risk_score > 2 else 2 }}%"></i></div>
      <div class="scalelbl"><span>0 — мінімальний ризик</span><span>100 — максимальний</span></div>
    </div>
    <div class="gauge">
      <div class="score">{{ risk_score }}<span>/100</span></div>
      <span class="risk {{ risk_level }}">{{ risk_level }}{% if risk_level != "INCONCLUSIVE" %} RISK{% endif %}</span>
      <p class="tiny" style="margin:8px 0 0">Оцінка ризику</p>
    </div>
  </div>

  <div class="callout callout-info"><span class="ic">ℹ️</span>
    <span>Цей звіт — <strong>допоміжний інструмент аналітика</strong>, а не автоматичний
    вердикт. Остаточне KYC-рішення приймає людина на основі цих даних і власного судження.</span></div>

  {% if coverage.registry == "not_found" %}
  <div class="callout callout-warn"><span class="ic">⚠️</span>
    <span><strong>Суб'єкта не знайдено у відкритих реєстрах.</strong> Це <u>не</u> означає «чисто».
    Окрема категорія — <strong>оборонні / військові підприємства та об'єкти критичної
    інфраструктури</strong>: їхні дані законно закриті з держреєстрів, тому повну
    реєстрову картину по них отримати неможливо. Потрібна ручна доперевірка.</span></div>
  {% endif %}

  <div class="card">
  <h2>Покриття джерел</h2>
  <p>{% for src, st in coverage.items() %}<span class="pill cov-{{ st }}" style="margin:0 6px 6px 0">{{ src }}: {{ st }}</span>{% endfor %}</p>
  <p class="tiny" style="margin:8px 0 0">«checked» — перевірено · «not_found» — суб'єкта не знайдено
     в джерелі (НЕ «чисто») · «skipped / unavailable» — не перевірялось.</p>
  </div>

  <div class="card">
  {% if red_flags %}
  <h2>🚩 Red flags</h2>
  <p class="tiny" style="margin:-6px 0 12px">Згруповано за вагою: <span class="neg">критичні</span> —
     потребують уваги; <span style="color:var(--medium)">застереження</span> — врахувати в рішенні;
     контекст — масштабні/довідкові факти (для великих оргструктур часто це норма, а не ризик).</p>
  <div class="flags">
  {% for f in red_flags %}
    {% set fl = f|lower %}
    {% if 'санкц' in fl or 'критич' in fl or 'відмив' in fl or 'протидії л' in fl or 'кримінал' in fl or 'розшук' in fl or 'inconclusive' in fl or 'неповна перевірка' in fl %}
      {% set tier, lbl = 'high', 'критично' %}
    {% elif 'судовий реєстр' in fl or 'виконавч' in fl or 'судові процеси' in fl or 'податковий борг' in fl or 'рішень' in fl %}
      {% set tier, lbl = 'info', 'контекст' %}
    {% else %}
      {% set tier, lbl = 'med', 'застереження' %}
    {% endif %}
    <div class="flagx flag-{{ tier }}"><span class="tag">{{ lbl }}</span><span class="txt">{{ f }}</span></div>
  {% endfor %}
  </div>
  {% else %}
  <h2>Red flags</h2>
  <div class="callout callout-ok" style="margin:0"><span class="ic">✅</span>
    <span>Тривожних сигналів не виявлено{% if has_gaps %} <em>(втім, частина джерел не
    перевірена — див. покриття вище)</em>{% endif %}.</span></div>
  {% endif %}
  </div>

  <div class="card">
  <h2>Як накопичувався ризик (audit trail)</h2>
  <p class="muted" style="margin-top:-4px">Як сформувалась оцінка ризику {{ risk_score }}/100 — крок за кроком,
     із зазначенням джерела кожного фактора (для відтворюваності рішення).
     Старт — 0 (презумпція доброчесності), кожен фактор додає ризик.</p>
  <table class="trail"><thead><tr><th>Крок / фактор</th><th>+ Ризик</th><th>Ризик</th><th>Джерело</th></tr></thead><tbody>
  {% for t in audit_trail %}
    <tr><td>{% if loop.first %}Базовий рівень (презумпція доброчесності){% else %}{{ t.step }}{% endif %}</td>
        <td class="{% if t.delta < 0 %}neg{% endif %}">{% if t.delta != 0 %}+{{ -t.delta }}{% else %}—{% endif %}</td>
        <td><strong>{{ 100 - t.score_after }}</strong></td><td class="muted">{{ t.source }}</td></tr>
  {% endfor %}
  </tbody></table>
  </div>

  <div class="card">
  <h2>Дані суб'єкта</h2>
  {% if registry and registry.raw_available %}
  <table class="kv">
    <tr><th>Назва / ПІБ</th><td>{{ registry.name or "—" }}</td></tr>
    {% if registry.edrpou %}<tr><th>ЄДРПОУ</th><td>{{ registry.edrpou }}</td></tr>{% endif %}
    {% if registry.rnokpp %}<tr><th>РНОКПП</th><td>{{ registry.rnokpp }}</td></tr>{% endif %}
    {% if registry.birth_date %}<tr><th>Дата народження</th><td>{{ registry.birth_date }}</td></tr>{% endif %}
    <tr><th>Статус</th><td>{{ registry.status or "—" }}</td></tr>
    <tr><th>Адреса</th><td>{{ registry.address or "—" }}</td></tr>
    {% if registry.director %}<tr><th>Керівник</th><td>{{ registry.director }}</td></tr>{% endif %}
    {% if registry.is_pep %}<tr><th>PEP</th><td>Так — публічний діяч</td></tr>{% endif %}
    {% if data_as_of %}<tr><th>Остання зміна в реєстрі</th><td>{{ data_as_of }}</td></tr>{% endif %}
    {% if registry.edrpou %}<tr><th>Джерело</th><td><a href="https://opendatabot.ua/c/{{ registry.edrpou|e }}" target="_blank" rel="noopener">Opendatabot · профіль компанії ↗</a></td></tr>
    {% elif registry.source_url %}<tr><th>Джерело</th><td><a href="{{ registry.source_url|e }}" target="_blank" rel="noopener">{{ registry.source or "opendatabot" }} ↗</a></td></tr>{% endif %}
  </table>
  {% elif coverage.registry == "not_found" %}
  <div class="callout callout-warn" style="margin:0"><span class="ic">⚠️</span>
    <span>Суб'єкта НЕ знайдено в реєстрі. Це не означає «чисто» — можливо, іноземець,
    закрита (оборонна) компанія, або неточний запит. Потрібна додаткова перевірка.</span></div>
  {% else %}<p class="muted">Реєстрова перевірка не виконувалась.</p>{% endif %}
  </div>

  <div class="card">
  <h2>Санкційний скринінг</h2>
  {% if sanctions %}
    {% if sanctions.has_sanctions_match %}
    <div class="callout callout-danger"><span class="ic">🚫</span>
      <span><strong>Виявлено збіги</strong> у {{ sanctions.matched_lists|length }} списк(ах). Деталі нижче.</span></div>
    <table><thead><tr><th>Ім'я</th><th>Список</th><th>Підстава</th><th>Період</th></tr></thead><tbody>
    {% for h in sanctions.hits %}
      <tr><td>{{ h.name or "—" }}</td>
          <td>{{ h.sanction_list or "—" }}</td>
          <td>{{ h.reason or h.decree or "—" }}</td>
          <td>{{ h.start_date or "" }}{% if h.end_date %} – {{ h.end_date }}{% endif %}</td></tr>
    {% endfor %}</tbody></table>
    {% else %}
    <div class="callout callout-ok" style="margin:0 0 14px"><span class="ic">✅</span>
      <span>Збігів у перевірених санкційних списках не виявлено.</span></div>
    {% endif %}
    {% if sanctions.checked_lists %}
    <p class="tiny" style="margin:0 0 7px">Перевірені списки ({{ sanctions.checked_lists|length }}){% if sanctions.has_sanctions_match %} —
       <span class="neg">червоним</span> позначено ті, де знайдено збіг{% endif %}:</p>
    <p style="margin:0">
      {% for lst in sanctions.checked_lists %}{% if lst in (sanctions.matched_lists or []) %}<span class="badge sev-critical" style="margin:0 5px 6px 0;text-transform:none">⚠ {{ lst }}</span>{% else %}<span class="cat" style="margin:0 5px 6px 0">{{ lst }}</span>{% endif %}{% endfor %}
    </p>
    {% endif %}
    <p class="tiny" style="margin:12px 0 0">Джерело:
      {% if registry and registry.edrpou %}<a href="https://opendatabot.ua/c/{{ registry.edrpou|e }}" target="_blank" rel="noopener">Opendatabot · профіль і санкційні фактори ↗</a>
      {% else %}<a href="https://opendatabot.ua/sanctions" target="_blank" rel="noopener">Opendatabot · перевірка санкцій ↗</a>{% endif %}
      — агрегує переліки <strong>РНБО</strong>, <strong>OFAC</strong> (SDN / Non-SDN),
      <strong>ЄС</strong>, <strong>Велика Британія</strong>, <strong>Канада</strong>.</p>
  {% else %}<p class="muted">Санкційний скринінг не виконувався.</p>{% endif %}
  </div>

  {% if resolved %}
  <div class="card">
  <h2>Корпоративна структура / пов'язані особи</h2>
  <table><thead><tr><th>Ім'я</th><th>Роль</th><th>Код</th><th>Джерела</th><th>Статус</th></tr></thead><tbody>
  {% for e in resolved %}
    <tr><td>{{ e.name or "—" }}</td><td>{{ e.role or "—" }}</td>
        <td>{{ e.code or "—" }}</td><td class="muted">{{ e.sources|join(", ") }}</td>
        <td>{% if e.needs_review %}<span class="review">manual review ({{ e.review_score }})</span>
            {% else %}<span class="pos">✓</span>{% endif %}</td></tr>
  {% endfor %}</tbody></table>
  {% if graph_file %}<p style="margin:14px 0 0"><a href="{{ graph_file }}">↗ Інтерактивний граф зв'язків</a></p>{% endif %}
  </div>
  {% endif %}

  {# лише згадки про самого субʼєкта (не однофамільця) #}
  {% set adverse_items = adverse | selectattr("is_adverse") | selectattr("is_about_target_person") | list %}
  <div class="card">
  <h2>Негативні медіа-згадки {% if adverse_items %}<span class="cat" style="margin-left:auto">{{ adverse_items|length }}</span>{% endif %}</h2>
  {% if adverse_items %}
  <table><thead><tr><th style="width:90px">Severity</th><th>Категорія</th><th>Резюме</th><th>Джерело</th></tr></thead><tbody>
  {% for m in adverse_items %}
    {% set low_conf = (m.match_confidence or "")|lower == "low" %}
    <tr><td><span class="badge sev-{{ m.severity or 'low' }}">{{ m.severity or "low" }}</span></td>
        <td><span class="cat">{{ m.category or "—" }}</span></td>
        <td>{{ m.summary or "" }}{% if low_conf %}<br><span class="tiny" style="color:var(--medium)">⚠ низька впевненість атрибуції — показано, але не враховано в оцінці ризику</span>{% endif %}</td>
        <td>{% if m.url %}<a href="{{ m.url|e }}" target="_blank" rel="noopener">{{ m.title or "першоджерело" }} ↗</a>{% else %}—{% endif %}</td></tr>
  {% endfor %}</tbody></table>
  <p class="tiny" style="margin-top:10px">Показано лише згадки, які класифікатор зіставив саме з цим
     суб'єктом (однофамільці відсіяні). Посилання ведуть на першоджерела (нова вкладка);
     повні копії статей збережено в evidence-архіві.</p>
  {% elif coverage.adverse_media == "checked" %}
  <div class="callout callout-ok" style="margin:0"><span class="ic">✅</span>
    <span>Медіа-пошук виконано — негативних згадок саме про цього суб'єкта не виявлено
    {% if adverse %}(перевірено {{ adverse|length }} джерел(а); згадки про однофамільців, за наявності, відсіяно){% endif %}.</span></div>
  {% else %}
  <p class="muted">Медіа-пошук не виконувався.</p>
  {% endif %}
  </div>

  <div class="card">
  <h2>Відомі обмеження</h2>
  <ul style="margin:0;padding-left:20px;font-size:13.5px;color:var(--ink-soft);line-height:1.7">
    <li><strong>Закриті реєстри.</strong> Оборонні / військові підприємства та об'єкти
        критичної інфраструктури законно приховані з відкритих держреєстрів — повну
        реєстрову картину по них отримати неможливо.</li>
    <li><strong>UA-центричність.</strong> Покриття обмежене українськими реєстрами.
        Іноземний суб'єкт (Кіпр, Естонія тощо) у реєстрі не знайдеться — і це позначається
        як «не знайдено», а <u>не</u> як «чисто».</li>
    <li><strong>Єдине реєстрове джерело.</strong> Реєстрова й санкційна частини тримаються
        на Opendatabot; перехресної звірки за другим незалежним джерелом поки немає.</li>
    <li><strong>Судова статистика ≠ ризик.</strong> Велика кількість судових справ у
        великих організацій (банки, ритейл) — наслідок масштабу, а не маркер ризику
        сам по собі. Кожну справу слід оцінювати за роллю сторони, предметом і масштабом.</li>
    <li><strong>Якість adverse media.</strong> Залежить від пошукової видачі та
        класифікатора — можливі як хибнопозитивні, так і пропущені згадки.
        Класифікатор відсіює однофамільців, але остаточна верифікація — за аналітиком.</li>
    <li><strong>Тотожність особи.</strong> Збіг за іменем не гарантує тотожності;
        match_confidence показує рівень упевненості. За відсутності РНОКПП/дати народження
        система не дає «чисто» (статус INCONCLUSIVE).</li>
    <li><strong>Часовий зріз.</strong> Дані актуальні на момент генерації звіту
        ({{ generated_at }}); реєстри й санкційні списки оновлюються постійно.</li>
  </ul>
  </div>

  <footer>
    Усі первинні джерела збережено в evidence-архіві прогону.<br>
    <strong>KYC Network Scout</strong> — автоматизований OSINT pipeline · Opendatabot · Serper · Claude
  </footer>
  </div>
</body></html>"""


def _render_html(context, report_dir):
    from jinja2 import Template
    html = Template(_HTML_TEMPLATE).render(**context)
    (report_dir / "report.html").write_text(html, encoding="utf-8")
    return html


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

    # Унікальний суфікс — інакше дві перевірки, що завершились в одну секунду
    # (батч), отримали б ту саму теку й затерли б звіт одна одній.
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    report_dir = _REPORT_BASE / f"{ts}-{uuid.uuid4().hex[:8]}"
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
        "adverse_media_count": len([
            m for m in adverse
            if m.get("is_adverse") and m.get("is_about_target_person")]),
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
            # лише згадки про самого субʼєкта (не однофамільця)
            for m in adverse
            if m.get("is_adverse") and m.get("is_about_target_person")
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
    _render_html(context, report_dir)
    zip_file = _build_evidence_zip(slug, report_dir)

    summary = {
        "slug": slug, "subject": subject,
        "trust_score": scoring["trust_score"],
        "risk_level": scoring["risk_level"],
        "report_dir": str(report_dir),
        "coverage": coverage,
        "artifacts": {"json": "kyc_report.json", "html": "report.html",
                      "graph": graph_file, "evidence_zip": zip_file},
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
