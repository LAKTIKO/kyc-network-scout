FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# System dependencies для Playwright/Chromium (скрейпінг adverse media)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    fonts-liberation \
    libnss3 \
    libxss1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libgbm1 \
    libgtk-3-0 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

# ── Dependency layer (cached unless requirements.txt changes) ──────────────
# --timeout/--retries: crawl4ai тягне важкі колеса (torch тощо); на повільному
# з'єднанні дефолтний pip падає на ReadTimeoutError з files.pythonhosted.org.
COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 --retries 10 -r requirements.txt

# Playwright installs Chromium to PLAYWRIGHT_BROWSERS_PATH=/ms-playwright;
# --with-deps runs its own apt-get update internally for browser-specific libs.
RUN python -m playwright install --with-deps chromium \
    && chmod -R 755 /ms-playwright

# ── Non-root user ──────────────────────────────────────────────────────────
RUN useradd --system --create-home --shell /bin/false appuser

# ── Application code (owned by appuser from the start) ────────────────────
COPY --chown=appuser:appuser workers/ workers/
COPY --chown=appuser:appuser config/ config/
# webapp/ + landing/ + examples/ потрібні веб-сервісу (лендинг, форма, приклади)
COPY --chown=appuser:appuser webapp/ webapp/
COPY --chown=appuser:appuser landing/ landing/
COPY --chown=appuser:appuser examples/ examples/

USER appuser

# docker-compose overrides CMD per service; this is a safe no-op default.
CMD ["python", "--version"]
