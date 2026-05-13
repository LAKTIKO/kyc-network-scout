# KYC Network Scout

Distributed pipeline для автоматизації Enhanced Due Diligence у KYC-практиці.

## Що це робить

Інструмент отримує на вхід ПІБ особи і автоматично:
1. Через Opendatabot API знаходить її компанії, директорів, акціонерів, бенефіціарів — поточних та історичних
2. Перевіряє знайдених осіб на міжнародні санкційні списки (OpenSanctions)
3. Шукає негативні згадки в медіа (Brave Search API + Crawl4AI + AI-класифікація)
4. Повертає структурований KYC-звіт

## Стек

- **Opendatabot API** — український реєстр юросіб та осіб
- **OpenSanctions bulk dataset** — міжнародні санкції (OFAC, EU, UK, UN)
- **Brave Search API** — adverse media discovery
- **Crawl4AI** — скрейпинг знайдених статей
- **Anthropic Claude API** — AI-класифікація adverse media
- **Celery + Redis** — distributed task queue
- **PostgreSQL** — структуровані дані
- **Prometheus + Grafana** — моніторинг
- **Docker Compose** — оркестрація

## Архітектура

3 workers + інфраструктура. Деталі додаються в процесі розробки.

## Статус

🚧 У розробці
