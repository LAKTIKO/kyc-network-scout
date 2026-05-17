# KYC Network Scout — Runbook

> Інструкція для перевірки роботи pipeline після setup'у.

## Передумови

- Repo склоновано
- `.env` створено з валідними `ANTHROPIC_API_KEY` та `SERPER_API_KEY`
- Docker Desktop запущено

## Smoke Test 1: Build і запуск

```bash
docker compose build
docker compose up -d
docker compose ps
```

**Очікуваний результат:** 4 контейнери зі статусом `Up`:
- `kyc-redis` (healthy)
- `kyc-scheduler`
- `kyc-scraper`
- `kyc-search`

Якщо контейнер у статусі `Restarting` або `Exit` — переходь до **Troubleshooting**.

## Smoke Test 2: Workers готові

```bash
docker compose logs --tail=30 worker-search worker-scraper scheduler
```

**Очікувані ключові рядки:**
- `worker-search@... ready.`
- `worker-scraper@... ready.`
- `Scheduler: Sending due task demo-heartbeat`

## Smoke Test 3: Запуск реальної KYC-задачі

```bash
docker compose exec worker-search python -c "from workers.tasks import search_task; result = search_task.delay('Ігор Коломойський', max_queries=2); print('Task queued! ID:', result.id)"
```

**Очікуваний результат:** виведення `Task queued! ID: <uuid>`.

## Smoke Test 4: Worker обробляє задачу

```bash
docker compose logs -f worker-search worker-scraper
```

Чекай 2-3 хвилини. **Очікувані рядки у логах:**
- `search_task: 'Ігор Коломойський' — 2 queries`
- `search_task: ... — queued N URLs` (N=10-25 зазвичай)
- `scrape_and_classify_task[...] received`
- `scraped https://... — N chars, error=None`
- `HTTP Request: POST https://api.anthropic.com/v1/messages "HTTP/1.1 200 OK"`
- `classified https://... — is_adverse=True severity=critical`
- `Task ... succeeded in N.NNs: {'url': '...', 'is_adverse': True, 'severity': 'critical', ...}`

Натисни Ctrl+C щоб зупинити перегляд (контейнери далі працюють у фоні).

## Smoke Test 5: Результати збережено на диск

```bash
ls data/normalized/ihor_kolomoyskyy/
```

**Очікуваний результат:** список з 5-20 JSON-файлів (по одному на статтю).

## Smoke Test 6: Перевір вміст одного результату

```bash
ls data/normalized/ihor_kolomoyskyy/ | head -1 | xargs -I {} cat data/normalized/ihor_kolomoyskyy/{} | python -m json.tool
```

**Очікуваний JSON має містити:**
- `is_about_target_person: true`
- `match_confidence: "high"` (для відомих публічних осіб)
- `is_adverse: true` (для adverse media)
- `severity` з категорії `critical`/`high`/`medium`/`low`/`none`
- `category` з sanctions/corruption/criminal/litigation/tax/fraud/other
- `summary` — короткий summary українською
- `key_quotes` — список цитат з оригінальної статті
- `error: null`

## Зупинка системи

```bash
docker compose stop
```

Контейнери зупинено, але не видалено. Для повного видалення:

```bash
docker compose down -v
```

(`-v` видаляє Redis volume — кеш черги стирається)

## Troubleshooting

### Контейнер scheduler падає з PermissionError: 'celerybeat-schedule'

Перевір `docker-compose.yaml` — у сервісі `scheduler` команда має містити `--schedule=/tmp/celerybeat-schedule`.

### Workers не обробляють задачі (LLEN celery > 0)

```bash
docker compose exec redis redis-cli LLEN celery
```

Якщо число > 0 і не зменшується — workers не слухають правильну чергу. Перевір що у `docker-compose.yaml` команди workers не містять `--queues=default` (Celery default queue — це `"celery"`).

### PermissionError при scraping

```
PermissionError: [Errno 13] Permission denied: '/home/appuser'
```

У `Dockerfile` у команді `useradd` має бути `--create-home` (не `--no-create-home`). Перебудуй образ через `docker compose build`.

### Rate limit errors у класифікації

```
HTTP/1.1 429 Too Many Requests
'error': 'Rate limit exceeded, retry later'
```

У `workers/classifier.py` має бути:
```python
client = anthropic.Anthropic(max_retries=5)
```

(без окремого `except anthropic.RateLimitError` block — SDK retry'ить сам).

## CLI-режим (без Docker, для розробки)

```bash
source .venv/bin/activate
python -m workers.run_pipeline "Ігор Коломойський" --max-queries 3 --max-urls 3
```

**Очікуваний результат:** structured report у stdout + JSON-файли у `data/normalized/ihor_kolomoyskyy/`.
