# AgentBox — browser MCP server (Playwright + Chromium)
#
# Base image note: Playwright/Chromium does not run on musl libc, so the
# original python:3.11-alpine base cannot work. We use Debian slim, the
# same family Hill90's Dockerfile already proves out. Pinned to bookworm
# because that is where the apt package names below (notably libasound2)
# are valid.

FROM python:3.12-slim-bookworm

WORKDIR /app

# ==============================================================================
# PLAYWRIGHT / CHROMIUM SYSTEM DEPENDENCIES
# Package list copied verbatim from SPEC.md §4 (validated by Hill90).
# ==============================================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libxshmfence1 \
    libxfixes3 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# The git tool (SPEC §8) shells out to the real git binary, which the
# slim base image does not ship. Kept as its own layer so the Chromium
# package list above stays the verbatim list from SPEC §4.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# tmux and zsh back the terminal (SPEC §15). tmux matters beyond taste:
# `new-session -A` reattaches, so a dropped WebSocket resumes the session
# instead of losing it. Installed unconditionally; the terminal route is
# still gated by AGENTBOX_ENABLE_TERMINAL.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tmux \
    zsh \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

# Install the Chromium browser binary
ENV PLAYWRIGHT_BROWSERS_PATH=/data/browsers
RUN playwright install chromium

# Copy application source
COPY src/ src/

# Screenshots are written here (mounted as a named volume in compose)
RUN mkdir -p /workspace/screenshots

# CRITICAL: Unbuffered output for streaming
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["python", "src/mcp_server.py"]
