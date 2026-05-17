# AI-Assisted Development Workflow

> Документація використання Claude Code для розробки KYC Network Scout

## Мета документу

KYC Network Scout розроблено з активним використанням Claude Code як AI-pair-programmer. Цей документ описує:
1. Як саме Claude Code допомагав на різних етапах
2. Які типи задач делегувались AI, а які залишались на ручне рішення
3. Один детальний приклад AI-assisted workflow з реальної розробки

## Контекст автора

**Background:** юрист, KYC/OSINT-аналітик без Python-фону. До цього проєкту — нуль досвіду з Python, Docker, Celery, Redis. Базове розуміння bash, git, технічних концепцій з юридичної практики (working with APIs, structured data).

**Виклик:** реалізувати distributed system з AI-інтеграцією за advanced рівнем курсу.

**Рішення:** використати Claude Code як інструмент для генерації коду + Claude (через web/desktop) як архітектурного консультанта і вчителя.

## Розподіл задач — людина vs AI

| Задача | Хто робив |
|---|---|
| Архітектурні рішення (які компоненти, як вони спілкуються) | Людина через діалог з Claude |
| Генерація Python коду (модулі `workers/`) | Claude Code за специфікаціями людини |
| Prompt engineering для AI-класифікатора | Людина (хто краще розуміє KYC domain?) |
| Bash команди для git, Docker, terminal | Людина під керівництвом Claude |
| Debug помилок (PermissionError, queue mismatch, rate limits) | Людина + Claude разом, ітеративно |
| Domain expertise (RF/BY focus, risk keywords UA/RU/EN) | Людина (юрист) |
| Документація (README, CLAUDE.md) | Claude Code за специфікаціями людини |
| Тестування на реальних особах (Коломойський, Єрмак) | Людина |

## Patterns використання Claude Code

### Pattern 1: Specification-driven generation

**Не:** "Напиши скрейпер"  
**Так:** структуровані промпти з ВХОДОМ, ВИХОДОМ, ОБРОБКОЮ ПОМИЛОК, EDGE CASES, TEST DATA

Приклад промпту для `workers/classifier.py`:
- Точна сигнатура функції (вхідні параметри, тип повернення)
- Структура prompt для Claude API (system prompt + user message template)
- Експлікований формат JSON output (9 полів зі своїми типами і значеннями)
- Правила класифікації (як визначати `match_confidence`, `severity`)
- Список exceptions для обробки (`RateLimitError`, `AuthenticationError`, JSON parse errors)
- Тестовий case для `__main__` блоку

Результат: Claude Code генерував код, який працював з першої спроби в 80% випадків. Решта 20% — дрібні правки.

### Pattern 2: Iterative debugging

При проблемах не вгадувати — діагностувати через `docker compose logs`, `redis-cli`, file checks. Кожен крок:
1. Людина копіює помилку у Claude чат
2. Claude дає 2-3 гіпотези
3. Людина виконує діагностичні команди
4. Claude обирає правильну гіпотезу і дає мінімальну зміну

Приклад: `PermissionError` для Crawl4AI у `/home/appuser` — діагностовано через 3 повідомлення в Claude чаті.

### Pattern 3: Domain expertise stays with human

Claude НЕ вирішував:
- Які risk keywords використовувати (UA: "санкції, кримінал, шахрайство", RU: "санкции, мошенничество, отмывание")
- Який `gl` параметр для Serper (`gl="ua"` для пошуку RF-новин про українців)
- Чому Sonnet 4.6 кращий за Sonnet 4.5 (фактчек через web search у Claude)
- Що означає `severity: critical` у KYC контексті

Це залишається експертизою людини. Claude — інструмент, не замінник.

## Один детальний приклад: Rate Limit виправлення

Реальний випадок з розробки.

### Симптом

Під час `docker compose up`, при обробці ~10 URL про Коломойського, у логах:

```
HTTP/1.1 429 Too Many Requests
Retrying request to /v1/messages in 23 seconds
classified ... is_adverse=False severity=none
succeeded ... 'error': 'Rate limit exceeded, retry later'
```

Половина задач завершалась з `error="Rate limit exceeded"` — статті не класифікувались.

### Hypothesis (через діалог з Claude)

**Людина:** скинула логи у Claude чат  
**Claude:** "Anthropic SDK сам має retry-механізм через параметр `max_retries`. Але ваш код перехоплює `anthropic.RateLimitError` одразу, не даючи SDK зробити retry."

### Solution

Два точкових зміни у `workers/classifier.py`:

**До:**
```python
client = anthropic.Anthropic()
...
except anthropic.RateLimitError:
    result["error"] = "Rate limit exceeded, retry later"
except anthropic.AuthenticationError:
    ...
```

**Після:**
```python
client = anthropic.Anthropic(max_retries=5)
...
# RateLimitError видалено — SDK сам retry'ить з exponential backoff
except anthropic.AuthenticationError:
    ...
```

### Implementation через Claude Code

Промпт людини:
> У workers/classifier.py треба покращити обробку rate limits. Зараз код перехоплює anthropic.RateLimitError одразу, не даючи SDK зробити вбудований retry. Виправлення: додай max_retries=5 у клієнт, видали except RateLimitError block.

Claude Code:
1. Прочитав файл (без додаткових питань — знав структуру з попереднього контексту)
2. Зробив 2 точкові правки через `str_replace`
3. Показав diff перед записом
4. Очікував підтвердження перед збереженням

### Verification

```bash
docker compose build && docker compose up -d
```

У логах після деплою:
```
HTTP/1.1 200 OK
classified ... is_adverse=True severity=critical
succeeded ... 'error': None
```

### Час до виправлення

| Етап | Час |
|---|---|
| Симптом → діагноз | 3 хв діалогу з Claude |
| Правки → деплой | 2 хв через Claude Code |
| **Разом** | **5 хв** vs ~30 хв ручного debug'у з документацією Anthropic SDK |

## Інші виправлені проблеми

| Проблема | Симптом | Fix |
|---|---|---|
| Queue mismatch | Workers не бачили задачі | Видалено `--queues=default` — Celery default queue `"celery"` |
| PermissionError (Beat) | `celerybeat-schedule` не створювався | Додано `--schedule=/tmp/celerybeat-schedule` |
| PermissionError (Crawl4AI) | Cache db в `/home/appuser/` | `useradd --no-create-home` → `--create-home` у Dockerfile |

## Інструменти, що використовувались

| Інструмент | Призначення |
|---|---|
| **Claude Code (CLI)** | Генерація і редагування коду у проєкті |
| **Claude (web/desktop)** | Архітектурні консультації, debug, learning |
| **Anthropic Claude API (Sonnet 4.6)** | AI-класифікатор у production коді |
| **GitHub** | Version control, public artifact для здачі |
| **Docker Desktop** | Local distributed system testing |

Практична нотатка: rate limits у Claude (web/desktop) подекуди гальмували темп розробки — в моменти інтенсивних консультацій доводилось робити паузи і повертатись до задачі пізніше.

## Висновок

Claude Code — це не "генератор коду", це **partner**. Найкращі результати приходять коли:
- Людина дає чіткі специфікації (не "напиши Y", а "Y з такими параметрами, виходом, edge cases")
- AI генерує structured output (код з типами, обробкою помилок, тестовим блоком)
- Людина переглядає, підтверджує або ітеративно виправляє
- Domain expertise залишається на людині

Проєкт KYC Network Scout — приклад того, як юрист без Python-фону може за тиждень побудувати production-якісну distributed систему завдяки AI-assisted development.

---

*Робочий контекст для Claude Code збережено у `docs/claude-development-context.md`*
