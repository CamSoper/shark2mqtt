FROM python:3.12-slim-bookworm

# Install Patchright/Chromium system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    xvfb \
    xauth \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create non-root user and data directory early so Chromium installs to the right path
RUN useradd -m shark \
    && mkdir -p /data \
    && chown -R shark:shark /app /data

COPY --chown=shark:shark requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Patchright Chromium browser as the shark user
USER shark
RUN patchright install chromium

COPY --chown=shark:shark src/ ./src/

ENV PYTHONUNBUFFERED=1

# Start Xvfb in the background and run the app directly.
# A virtual display is needed for headed Chromium to bypass Cloudflare Turnstile.
ENTRYPOINT ["/bin/sh", "-c", "Xvfb :99 -screen 0 1024x768x16 &\nexport DISPLAY=:99\nexec python -m src.main \"$@\"", "--"]
