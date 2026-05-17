FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# System dependencies needed by Playwright/Chromium
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
    && rm -rf /var/lib/apt/lists/*

# ── Dependency layer (cached unless requirements.txt changes) ──────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright installs Chromium to PLAYWRIGHT_BROWSERS_PATH=/ms-playwright;
# --with-deps runs its own apt-get update internally for browser-specific libs.
RUN python -m playwright install --with-deps chromium \
    && chmod -R 755 /ms-playwright

# ── Non-root user ──────────────────────────────────────────────────────────
RUN useradd --system --create-home --shell /bin/false appuser

# ── Application code (owned by appuser from the start) ────────────────────
COPY --chown=appuser:appuser workers/ workers/
COPY --chown=appuser:appuser config/ config/

USER appuser

# docker-compose overrides CMD per service; this is a safe no-op default.
CMD ["python", "--version"]
