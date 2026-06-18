"""Збирає односторінковий самодостатній HTML лендингу:
CSS вже inline; прев'ю-зображення → base64 data-URI; приклад звіту та JSON
зашиваються у файл і відкриваються через Blob (нова вкладка) — без зовнішніх
файлів/папок. Результат можна переслати одним вкладенням.

  python landing/build_standalone.py [output.html]
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LANDING = ROOT / "landing" / "index.html"
PREVIEW = ROOT / "landing" / "assets" / "report-preview.png"
REPORT = ROOT / "examples" / "example_report.html"
REPORT_JSON = ROOT / "examples" / "example_report.json"
GRAPH = ROOT / "examples" / "graph.html"
GRAPH_UTILS = ROOT / "examples" / "lib" / "bindings" / "utils.js"


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _b64_str(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _selfcontained_graph() -> str:
    """Граф із вбудованим utils.js (інакше через blob вкладений lib/ не
    резолвиться) і полагодженим pyvis CSS-URL."""
    if not GRAPH.exists():
        return ""
    g = GRAPH.read_text(encoding="utf-8")
    if GRAPH_UTILS.exists():
        g = g.replace('<script src="lib/bindings/utils.js"></script>',
                      "<script>\n" + GRAPH_UTILS.read_text(encoding="utf-8") + "\n</script>")
    g = g.replace("dist/dist/vis-network.min.css", "dist/vis-network.min.css")
    return g


def build(out: Path) -> Path:
    html = LANDING.read_text(encoding="utf-8")

    # 1) прев'ю-картинка → data-URI
    img_uri = "data:image/png;base64," + _b64(PREVIEW)
    html = html.replace("assets/report-preview.png", img_uri)

    # 2) у звіт вшиваємо самодостатній граф (blob) і перемикаємо на нього
    #    посилання «Інтерактивний граф зв'язків»
    report_html = REPORT.read_text(encoding="utf-8")
    graph = _selfcontained_graph()
    if graph:
        graph_opener = (
            '<script>\n'
            f'  var _GRAPH_B64="{_b64_str(graph)}";\n'
            '  function openGraph(){var bin=atob(_GRAPH_B64);'
            'var bytes=Uint8Array.from(bin,function(c){return c.charCodeAt(0);});'
            "window.open(URL.createObjectURL(new Blob([bytes],{type:'text/html'})),'_blank');}\n"
            '</script>')
        report_html = report_html.replace(
            'href="graph.html"', 'href="#" onclick="openGraph();return false"')
        report_html = report_html.replace("</body>", graph_opener + "\n</body>", 1)

    # 3) звіт і JSON → base64 у JS, відкриття через Blob
    report_b64 = _b64_str(report_html)
    json_b64 = _b64(REPORT_JSON) if REPORT_JSON.exists() else ""

    bridge = f"""
  const _REPORT_B64="{report_b64}";
  const _JSON_B64="{json_b64}";
  function _b64ToBlob(b64, type){{
    const bin=atob(b64); const bytes=Uint8Array.from(bin,c=>c.charCodeAt(0));
    return new Blob([bytes],{{type:type}});
  }}
  function openReport(){{ window.open(URL.createObjectURL(_b64ToBlob(_REPORT_B64,'text/html')),'_blank'); }}
  function openJson(){{ if(!_JSON_B64) return; window.open(URL.createObjectURL(_b64ToBlob(_JSON_B64,'application/json')),'_blank'); }}
"""

    # вставляємо міст одразу після відкриття <script>
    html = html.replace("<script>", "<script>\n" + bridge, 1)

    # перенаправляємо всі посилання на приклад звіту / JSON на Blob-функції
    html = html.replace(
        '<a class="btn" href="../examples/example_report.html" target="_blank">Відкрити звіт ↗</a>',
        '<a class="btn" href="#" onclick="openReport();return false">Відкрити звіт ↗</a>')
    html = html.replace(
        '<a class="btn ghost" href="../examples/example_report.json" target="_blank">JSON ↗</a>',
        '<a class="btn ghost" href="#" onclick="openJson();return false">JSON ↗</a>')
    html = html.replace(
        '<a href="../examples/example_report.html" target="_blank">\n          <img',
        '<a href="#" onclick="openReport();return false">\n          <img')
    html = html.replace(
        "window.open('../examples/example_report.html','_blank');",
        "openReport();")

    out.write_text(html, encoding="utf-8")
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "landing" / "kyc-network-scout.html"
    p = build(target)
    print(f"standalone written: {p}  ({p.stat().st_size/1024:.0f} KB)")
