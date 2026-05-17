# KYC Network Scout

> Distributed adverse media pipeline для KYC Due Diligence з AI-класифікацією через Anthropic Claude

## Огляд

KYC Network Scout — це distributed система для автоматизованого пошуку, скрейпингу та AI-класифікації негативних згадок у медіа про осіб, що проходять KYC-перевірку. Phase 1 реалізує Worker 3 ("Журналіст") — повний end-to-end pipeline від ПІБ до структурованого JSON-звіту з severity, категорією та цитатами. Розроблено як реальний інструмент для практики Enhanced Due Diligence з фокусом на RF/BY-пов'язані ризики.

## Ключові особливості

- **Тримовний пошук** — UA/RU/EN з гео-фокусом на RF/BY/окуповані території
- **AI-класифікація через Claude Sonnet 4.6** — structured JSON output з полями `is_about_target_person`, `match_confidence`, `severity` (critical/high/medium/low), `category` (sanctions/corruption/criminal/litigation/tax/fraud/other), `summary`, `key_quotes`
- **Distributed архітектура** — Redis broker + 2 Celery workers (search + scraper) + Celery Beat scheduler
- **Idempotent processing** — кешування у `data/normalized/` запобігає повторним API-викликам
- **Resilient rate limiting** — Anthropic SDK retry з exponential backoff (`max_retries=5`)
- **Production-ready Docker** — non-root user, healthchecks, layer caching
- **Structured metrics у логах** — `total_credits_used`, `total_tokens_used`, `scrape_errors`, `classify_errors`

## Архітектура

```text
┌──────────────────────────────────────────────────────────┐
│                  KYC Network Scout Phase 1                │
└──────────────────────────────────────────────────────────┘

   User (CLI / docker compose exec)
            │
            ▼
   ┌──────────────────┐
   │  search_task     │ ◄── Celery task in queue
   │  (worker-search) │
   └────────┬─────────┘
            │
            │ generates 30 queries (UA/RU/EN with RF/BY focus)
            │ sends to Serper API → returns ~50 URLs
            │
            │ for each unique URL → apply_async()
            ▼
   ┌─────────────────────────────────────┐
   │  Redis Queue (broker)               │
   │  ╔═══════════════════════════════╗  │
   │  ║ scrape_and_classify_task * N  ║  │
   │  ╚═══════════════════════════════╝  │
   └──────────┬──────────────────────────┘
              │
              ▼
   ┌─────────────────────┐
   │ scrape_and_classify │ ◄── workers process in parallel
   │ (worker-scraper)    │      (concurrency=1, scraper-heavy)
   └────────┬────────────┘
            │
            │ 1. Crawl4AI + Playwright → markdown
            │ 2. Anthropic Claude Sonnet 4.6 → structured JSON
            │ 3. Save to data/normalized/{slug}/{hash}.json
            ▼
   ┌─────────────────────────────────────┐
   │ Output: data/normalized/{slug}/     │
   │  • {hash}.json (verdict)            │
   │  • severity, category, summary      │
   │  • match_evidence, key_quotes       │
   └─────────────────────────────────────┘

   ┌───────────────┐
   │ Beat Scheduler│ → heartbeat every 10 minutes (demo)
   └───────────────┘
```

Скриншоти працюючої системи: див. `docs/01-bootup-and-scheduler.png`, `docs/02-search-task-and-classification.png`, `docs/03-cache-and-fresh-scraping.png`.

## Вимоги

- Docker Desktop 27+ та Docker Compose v2+
- Python 3.12+ (для CLI режиму без Docker)
- API ключі:
  - Anthropic API key для класифікації: https://console.anthropic.com
  - Serper API key для пошуку: https://serper.dev (2500 безкоштовних запитів)

## Quick Start — Docker

```bash
# 1. Клонувати репозиторій
git clone https://github.com/LAKTIKO/kyc-network-scout
cd kyc-network-scout

# 2. Налаштувати змінні середовища
cp .env.example .env
# Відредагуй .env — додай ANTHROPIC_API_KEY та SERPER_API_KEY

# 3. Зібрати образ (перший раз ~5 хв через Playwright)
docker compose build

# 4. Запустити всі сервіси
docker compose up -d

# 5. Перевірити статус
docker compose ps

# 6. Запустити KYC pipeline
docker compose exec worker-search python -c "
from workers.tasks import search_task
result = search_task.delay('Ігор Коломойський', max_queries=5)
print('Task queued! ID:', result.id)
"

# 7. Стежити за логами
docker compose logs -f

# 8. Переглянути результати
ls data/normalized/ihor_kolomoyskyy/
cat data/normalized/ihor_kolomoyskyy/<hash>.json | python -m json.tool
```

## CLI Mode (без Docker)

Для розробки та одноразових перевірок:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
python -m workers.run_pipeline "Ігор Коломойський" --max-queries 5 --max-urls 5
```

**CLI vs Docker:** CLI-режим виконує весь pipeline послідовно в одному скрипті — зручно для розробки та швидких перевірок. Docker Compose запускає distributed workers паралельно через Redis — для production з чергами, кешуванням між запусками та горизонтальним масштабуванням.

## Приклад виводу

```json
{
  "url": "https://suspilne.media/947495-rnbo-zaprovadili-sankcii-proti-kolomojskogo-bogolubova-zevago-i-medvedcuka/",
  "title": "РНБО запровадила санкції проти Коломойського, Медведчука та інших — Суспільне Новини",
  "person_name": "Ігор Коломойський",
  "classified_at": "2026-05-16T11:01:39+00:00",
  "model": "claude-sonnet-4-6",
  "is_about_target_person": true,
  "match_confidence": "high",
  "match_evidence": [
    "Стаття прямо називає Ігоря Коломойського бізнесменом та ексспіввласником ПриватБанку",
    "Згадується перебування під вартою у справі про замовне вбивство та підозра у заволодінні 9,2 мільярда гривень ПриватБанку"
  ],
  "is_adverse": true,
  "severity": "critical",
  "category": "sanctions",
  "summary": "РНБО України 12 лютого 2025 року запровадила санкції проти Ігоря Коломойського, заблокувавши його активи. Коломойський перебуває під вартою у справі про замовне вбивство та отримав підозру у заволодінні понад 9 млрд гривень ПриватБанку.",
  "key_quotes": [
    "Ігор Коломойський зараз знаходиться під вартою у справі про замовне вбивство",
    "Коломойський отримав підозру в заволодінні 9,2 мільярда гривень ПриватБанку"
  ],
  "tokens_used": 13590,
  "error": null
}
```

## Як це працює

| Модуль | Роль |
|---|---|
| `workers/search_queries.py` | Генерує 30 трилінгвальних запитів з RF/BY фокусом на основі `config/risk_keywords.yaml` |
| `workers/serper_client.py` | Викликає Serper API, повертає до 10 URLs на запит |
| `workers/scraper.py` | Crawl4AI + Playwright + PruningContentFilter — скрейпить статтю у markdown |
| `workers/classifier.py` | Anthropic Claude Sonnet 4.6 для structured JSON classification |
| `workers/run_pipeline.py` | CLI-режим: повний end-to-end в одному скрипті з кешуванням |
| `workers/celery_app.py` | Celery configuration: Redis broker, Beat schedule, timezone Europe/Kyiv |
| `workers/tasks.py` | Distributed task definitions: `search_task`, `scrape_and_classify_task`, `heartbeat` |

## Phase 1 vs Phase 2

Проєкт вийшов за межі академічного завдання. Після виконання Phase 1 я вирішив довести його до повноцінного інструменту для перевірки контрагентів у реальній практиці — завдання надихнуло настільки, що повернутись і допрацювати стало природним рішенням.

**Phase 1 — курсове завдання (поточна здача):**
- ✅ Worker 3 "Журналіст" — adverse media pipeline
- ✅ Distributed через Celery + Redis + Docker Compose
- ✅ AI structured extraction через Claude Sonnet 4.6
- ✅ Тримовний пошук з RF/BY гео-фокусом
- ✅ Local logging metrics

**Phase 2 — production-ready KYC tool (в процесі):**

Phase 2 — особистий проєкт, що виріс із цього завдання. Мета — інтегрувати pipeline у реальний KYC Due Diligence workflow: отримати зручний інструмент для перевірки контрагентів, яким можна користуватись щодня, а не лише для академічних цілей.

- ⏳ Worker 1 "Реєстратор" — Opendatabot API (компанії, директори, бенефіціари UA реєстру)
- ⏳ Worker 2 "Санкційний контролер" — OpenSanctions local dump (OFAC, EU, UK, UN) з fuzzy matching
- ⏳ Aggregator — об'єднання всіх 3 workers у єдиний структурований KYC-звіт
- ⏳ Integration як SKILL у Claude Desktop для роботи безпосередньо з чату
- ⏳ Prometheus + Grafana — production monitoring (навмисно відкладено: потрібен після стабілізації основного pipeline)

## Чому без Prometheus + Grafana

Метрики Phase 1 реалізовані через structured logging у Celery worker logs та summary report у CLI-режимі (`_print_report()`). Цього достатньо для поточного масштабу.

Prometheus + Grafana — свідомо відкладено на Phase 2. Коли pipeline запрацює в production-режимі для регулярних перевірок, monitoring stack стане необхідним: alerting на помилки класифікації, трекінг витрат токенів у часі, SLA на час обробки одного контрагента. На Phase 1 це overhead без практичної потреби — результати видно безпосередньо з логів і JSON-файлів.

## Worker-JS — коли був би потрібен

Поточний worker-scraper використовує Crawl4AI + headless Chromium через Playwright, що покриває більшість українських та міжнародних медіа-сайтів (Suspilne, BBC Ukrainian, Forbes UA, Pravda, RBC, Censor, Parlament, Ukrinform). Окремий worker-js був би потрібен, якби сайт є повністю SPA з контентом тільки після API calls (рідко у нашому медіа просторі), використовує WebSocket для динамічного оновлення, або має складну 2FA/captcha верифікацію. Для нашого use case Crawl4AI з base configuration достатньо — Playwright всередині нього вже рендерить JavaScript.

## Tech Stack

| Технологія | Версія | Роль |
|---|---|---|
| Crawl4AI | 0.8.6 | Async web scraping з Playwright headless Chromium |
| Anthropic Claude Sonnet 4.6 | — | AI classification ($3/1M tokens) |
| Serper API | — | Google search proxy (2500 free queries) |
| Celery | 5.6 | Distributed task queue |
| Redis | 7-alpine | Message broker + result backend |
| Docker Compose | v2 | Orchestration of 4 services |
| Python | 3.12 | Runtime |

## Структура проєкту

```text
kyc-network-scout/
├── workers/
│   ├── __init__.py
│   ├── search_queries.py    # Query generation
│   ├── serper_client.py     # Serper API client
│   ├── scraper.py           # Crawl4AI scraper
│   ├── classifier.py        # Claude AI classifier
│   ├── run_pipeline.py      # CLI pipeline runner
│   ├── celery_app.py        # Celery configuration
│   └── tasks.py             # Distributed task definitions
├── config/
│   └── risk_keywords.yaml   # Trilingual keyword dictionary (UA/RU/EN)
├── data/
│   ├── raw/                 # Scraped markdown (gitignored)
│   └── normalized/          # Classified JSON results (gitignored)
├── docs/
│   └── architecture.txt
├── Dockerfile
├── .dockerignore
├── docker-compose.yaml
├── requirements.txt
├── .env.example
├── CLAUDE.md
└── README.md
```

## Автор

**Illia Onyshchuk** — KYC/OSINT analyst  
Курс: OSINT/AI Course (advanced level submission, May 2026)
