"""Веб-міст KYC Network Scout.

Перетворює статичний лендинг на робочу точку входу: форма (назва+ЄДРПОУ /
ПІБ+РНОКПП) → фоновий запуск `run_kyc` → готовий HTML-звіт у браузері.

  uvicorn webapp.server:app --host 0.0.0.0 --port 8000

Архітектура навмисне проста: `run_kyc` синхронний і самодостатній (не вимагає
Celery-воркерів), тож кожна перевірка крутиться у власному фоновому потоці,
а статус опитується через polling. Звіти віддаються прямо з output/<slug-run>.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("webapp")

ROOT = Path(__file__).resolve().parent.parent
LANDING = ROOT / "landing" / "index.html"
_REPORT_BASE = Path(os.getenv("REPORT_DIR", "output")).resolve()
_JOBS_DIR = _REPORT_BASE / ".jobs"

# Basic-auth: вмикається лише якщо задано KYC_WEB_PASSWORD (інакше відкрито —
# зручно локально). Для команди задай пароль у .env → доступ за логіном/паролем.
_WEB_USER = os.getenv("KYC_WEB_USER", "kyc")
_WEB_PASSWORD = os.getenv("KYC_WEB_PASSWORD")

app = FastAPI(title="KYC Network Scout")


def _check_basic(auth_header: str | None) -> bool:
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(auth_header[6:]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, _, pwd = raw.partition(":")
    return (secrets.compare_digest(user, _WEB_USER)
            and secrets.compare_digest(pwd, _WEB_PASSWORD or ""))


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Пароль на весь застосунок (крім healthz), коли заданий KYC_WEB_PASSWORD."""
    if _WEB_PASSWORD and request.url.path != "/healthz":
        if not _check_basic(request.headers.get("authorization")):
            return Response(status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="KYC Network Scout"'})
    return await call_next(request)

# статика лендингу/прикладів — відносні шляхи лендингу (assets/…, ../examples/…)
# резолвляться у /assets/… та /examples/… відносно кореня.
app.mount("/assets", StaticFiles(directory=str(ROOT / "landing" / "assets")), name="assets")
app.mount("/examples", StaticFiles(directory=str(ROOT / "examples")), name="examples")

# ── сховище задач (in-memory) ────────────────────────────────────────────────
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=2)


_PERSIST_KEYS = ("state", "report_dir", "subject", "subject_type",
                 "trust_score", "risk_level", "report_url")


def _set(job_id: str, **kw: Any) -> None:
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).update(kw)


def _get(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


def _persist(job_id: str) -> None:
    """Зберігаємо завершену задачу на диск, щоб лінки на звіти переживали
    рестарт сервера (in-memory мапа інакше втрачається)."""
    j = _get(job_id)
    if not j:
        return
    try:
        _JOBS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {k: j.get(k) for k in _PERSIST_KEYS if j.get(k) is not None}
        (_JOBS_DIR / f"{job_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("job persist skipped (%s): %s", job_id, exc)


def _load_jobs() -> None:
    """Відновлюємо завершені задачі з диску при старті."""
    if not _JOBS_DIR.exists():
        return
    restored = 0
    for p in _JOBS_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        rd = d.get("report_dir")
        if d.get("state") == "done" and rd and (Path(rd) / "report.html").exists():
            _jobs[p.stem] = d
            restored += 1
    if restored:
        logger.info("відновлено %d звіт(ів) з диску", restored)


def _run_job(job_id: str, subject: str, is_person: bool, full_name: str | None) -> None:
    from workers.run_kyc import run_kyc
    _set(job_id, state="running", message="Перевіряю реєстри, санкції та медіа… (зазвичай 30–90 с)")
    try:
        out = run_kyc(subject, is_person=is_person, full_name=full_name, with_media=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("job %s впав", job_id)
        _set(job_id, state="error", error=f"Внутрішня помилка: {exc}")
        return

    if out.get("error"):
        _set(job_id, state="error", error=out["error"])
        return

    rep = out.get("report") or {}
    report_dir = rep.get("report_dir")
    if not report_dir or not (Path(report_dir) / "report.html").exists():
        _set(job_id, state="error", error="Звіт не згенеровано (немає report.html).")
        return

    _set(job_id, state="done", report_dir=report_dir,
         subject=rep.get("subject"),
         subject_type=out.get("subject_type"),
         trust_score=rep.get("trust_score"),
         risk_level=rep.get("risk_level"),
         report_url=f"/report/{job_id}/")
    _persist(job_id)
    logger.info("job %s готово: %s (%s/100)", job_id,
                rep.get("risk_level"), rep.get("trust_score"))


# ── API ──────────────────────────────────────────────────────────────────────
class CheckRequest(BaseModel):
    type: str                 # "company" | "person"
    name: str | None = None
    code: str | None = None   # ЄДРПОУ або РНОКПП


@app.post("/api/check")
def api_check(req: CheckRequest) -> JSONResponse:
    name = (req.name or "").strip()
    code = (req.code or "").strip()
    is_person = req.type == "person"

    if not name and not code:
        return JSONResponse({"error": "Вкажіть назву/ПІБ або код."}, status_code=400)

    # субʼєкт для run_kyc: код у пріоритеті (стабільний ID), інакше назва.
    subject = code or name
    full_name = name or None

    job_id = uuid.uuid4().hex
    _set(job_id, state="queued", message="У черзі…")
    _pool.submit(_run_job, job_id, subject, is_person, full_name)
    return JSONResponse({"job_id": job_id})


@app.get("/api/status/{job_id}")
def api_status(job_id: str) -> JSONResponse:
    j = _get(job_id)
    if not j:
        return JSONResponse({"error": "Невідома задача"}, status_code=404)
    payload = {k: j.get(k) for k in (
        "state", "message", "error", "subject", "subject_type",
        "trust_score", "risk_level", "report_url") if j.get(k) is not None}
    return JSONResponse(payload)


@app.get("/report/{job_id}/")
def report_index(job_id: str) -> Response:
    j = _get(job_id)
    if not j or j.get("state") != "done":
        return HTMLResponse("<h1>Звіт ще не готовий</h1>", status_code=404)
    return FileResponse(Path(j["report_dir"]) / "report.html")


@app.get("/report/{job_id}/{fname:path}")
def report_asset(job_id: str, fname: str) -> Response:
    """Супутні файли звіту (graph.html та вкладені lib/… ресурси графа) —
    лише в межах теки звіту (захист від path-traversal)."""
    j = _get(job_id)
    if not j or j.get("state") != "done":
        return Response(status_code=404)
    base = Path(j["report_dir"]).resolve()
    target = (base / fname).resolve()
    if base != target and base not in target.parents:
        return Response(status_code=404)
    if not target.is_file():
        return Response(status_code=404)
    return FileResponse(target)


# ── лендинг із вбудованим клієнтським містком ────────────────────────────────
_CLIENT_BRIDGE = """
<script>
(function(){
  var ov=document.createElement('div'); ov.id='kyc-ov';
  ov.style.cssText='position:fixed;inset:0;z-index:999;display:none;align-items:center;'+
    'justify-content:center;background:rgba(2,6,23,.72);backdrop-filter:blur(4px)';
  ov.innerHTML='<div id="kyc-ov-card" style="background:#fff;border-radius:16px;max-width:560px;'+
    'width:92%;max-height:86vh;overflow:auto;padding:26px;box-shadow:0 30px 70px rgba(2,6,23,.5);'+
    'font-family:inherit"></div>';
  document.body.appendChild(ov);
  var card=ov.querySelector('#kyc-ov-card');
  var st=document.createElement('style');
  st.textContent='@keyframes kspin{to{transform:rotate(360deg)}}'+
    '.kspin{display:inline-block;width:15px;height:15px;border:2px solid #e2e8f0;'+
    'border-top-color:#1d4ed8;border-radius:50%;animation:kspin .9s linear infinite;vertical-align:middle}';
  document.head.appendChild(st);
  function show(){ov.style.display='flex';}
  ov.addEventListener('click',function(e){ if(e.target===ov) ov.style.display='none'; });

  var COLORS={LOW:'#16a34a',MEDIUM:'#d97706',HIGH:'#dc2626',INCONCLUSIVE:'#64748b'};
  var CLOSE='<button onclick="document.getElementById(\\'kyc-ov\\').style.display=\\'none\\'" '+
    'style="background:#1d4ed8;color:#fff;border:none;padding:10px 20px;border-radius:10px;'+
    'font-weight:600;cursor:pointer">Закрити</button>';

  function rowHtml(it){
    var right;
    if(it.state==='done'){
      var risk=(it.trust_score==null)?'—':(100-it.trust_score);
      var c=COLORS[it.risk_level]||'#64748b';
      right='<span style="display:inline-block;padding:3px 10px;border-radius:999px;color:#fff;'+
        'font-weight:700;font-size:11px;background:'+c+'">'+it.risk_level+' · '+risk+'/100</span>'+
        ' <a href="'+it.report_url+'" target="_blank" style="font-weight:600;font-size:13px;'+
        'color:#1d4ed8;text-decoration:none;margin-left:6px">Звіт ↗</a>';
    } else if(it.state==='error'){
      right='<span style="color:#dc2626;font-size:12.5px">⚠ '+(it.error||'помилка')+'</span>';
    } else {
      right='<span class="kspin"></span> <span style="color:#94a3b8;font-size:12.5px">перевіряю…</span>';
    }
    return '<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;'+
      'padding:11px 0;border-bottom:1px solid #eef2f6">'+
      '<span style="font-size:14px;color:#0f172a;font-weight:600;overflow:hidden;'+
      'text-overflow:ellipsis;white-space:nowrap;max-width:52%">'+(it.label||'—')+'</span>'+
      '<span style="text-align:right;white-space:nowrap">'+right+'</span></div>';
  }
  function render(items){
    var done=items.filter(function(i){return i.state==='done'||i.state==='error';}).length;
    card.innerHTML='<div style="display:flex;justify-content:space-between;align-items:center;'+
      'margin-bottom:4px"><div style="font-weight:700;font-size:17px;color:#0f172a">'+
      'Перевірка контрагентів</div><div style="font-size:13px;color:#94a3b8">'+done+' / '+items.length+'</div></div>'+
      '<div style="font-size:13px;color:#475569;margin-bottom:8px">Перевірки виконуються '+
      'паралельно — звіти зʼявляються в міру готовності.</div>'+
      items.map(rowHtml).join('')+
      '<div style="text-align:right;margin-top:16px">'+CLOSE+'</div>';
  }

  function pollItem(items,i){
    fetch('/api/status/'+items[i].job).then(function(r){return r.json();}).then(function(j){
      if(j.state==='done'){ items[i].state='done'; items[i].risk_level=j.risk_level;
        items[i].trust_score=j.trust_score; items[i].report_url=j.report_url;
        if(j.subject) items[i].label=j.subject; render(items); }
      else if(j.state==='error'){ items[i].state='error'; items[i].error=j.error; render(items); }
      else { setTimeout(function(){pollItem(items,i);},2000); }
    }).catch(function(){ setTimeout(function(){pollItem(items,i);},2500); });
  }
  function startBatch(subs){
    var items=subs.map(function(s){ return {label:(s.name||s.code), state:'queued'}; });
    render(items); show();
    subs.forEach(function(s,i){
      fetch('/api/check',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(s)})
        .then(function(r){return r.json();})
        .then(function(j){ if(j.error){ items[i].state='error'; items[i].error=j.error; render(items); }
          else { items[i].job=j.job_id; items[i].state='running'; render(items); pollItem(items,i); } })
        .catch(function(){ items[i].state='error'; items[i].error='Сервер недоступний'; render(items); });
    });
  }

  // демо-обробник форми → реальна батч-перевірка всіх заповнених рядків
  window.runDemo=function(e){
    if(e&&e.preventDefault)e.preventDefault();
    var subs=(window.collectSubjects?collectSubjects():[]);
    if(!subs.length){
      card.innerHTML='<div style="text-align:center"><div style="font-size:28px">⚠️</div>'+
        '<div style="color:#475569;font-size:14px;margin:8px 0 16px">Додайте хоча б одного контрагента.</div>'+
        CLOSE+'</div>'; show(); return false;
    }
    startBatch(subs); return false;
  };
})();
</script>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = LANDING.read_text(encoding="utf-8")
    # вбудовуємо клієнтський місток перед закриттям body
    html = html.replace("</body>", _CLIENT_BRIDGE + "\n</body>", 1)
    return HTMLResponse(html)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# відновлюємо збережені звіти при старті процесу
_load_jobs()
