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

# Jumpbox tooling. `less`, `procps` and `openssh-client` are correctness
# gaps rather than conveniences: git pages through less, an agent that
# cannot run `ps` cannot see its own processes (we had to use `docker top`
# from outside all along), and a jumpbox that cannot ssh is a strange
# jumpbox. The rest is the ordinary kit an agent reaches for.
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    less \
    procps \
    jq \
    vim \
    wget \
    unzip \
    ripgrep \
    ca-certificates \
    iputils-ping \
    dnsutils \
    netcat-openbsd \
    rsync \
    && rm -rf /var/lib/apt/lists/*

# tmux and zsh back the terminal (SPEC §15). tmux matters beyond taste:
# `new-session -A` reattaches, so a dropped WebSocket resumes the session
# instead of losing it. Installed unconditionally; the terminal route is
# still gated by AGENTBOX_ENABLE_TERMINAL.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tmux \
    zsh \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. The terminal (SPEC §15) hands out a real shell, and a
# root shell is a materially worse thing to expose than an unprivileged
# one — Hill90's agentbox drops to `agentuser` for exactly this reason.
# Everything the server touches at runtime is owned by this user.
RUN useradd --create-home --shell /bin/zsh --uid 1000 agentbox

# FiraCode Nerd Font, for terminal rendering in xterm.js — same font and
# same source as hill90-app's agentbox. The tmux Tokyo Night status bar
# draws powerline separators and icons from the Nerd Font private-use
# area; without this they render as tofu.
# curl and xz-utils are needed for the download itself and are not in the
# slim base. No `|| true` on the end: an earlier version had one, the
# download failed silently because curl was missing, and the image
# shipped with an empty font directory that nothing complained about.
RUN apt-get update && apt-get install -y --no-install-recommends \
        fontconfig curl xz-utils && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /usr/share/fonts/nerd-fonts && \
    curl -fsSL -o /tmp/firacode.tar.xz https://github.com/ryanoasis/nerd-fonts/releases/download/v3.3.0/FiraCode.tar.xz && \
    tar -xf /tmp/firacode.tar.xz -C /usr/share/fonts/nerd-fonts && \
    rm /tmp/firacode.tar.xz && \
    fc-cache -f && \
    fc-list | grep -qi firacode

# tmux Tokyo Night theme (SPEC §15.5), matching hill90-app's agentbox and
# Jon's dotfiles. The plugin is pinned to the same commit as both, so the
# status bar here looks like the status bar there rather than drifting
# with upstream.
RUN git clone --depth=1 https://github.com/tmux-plugins/tpm /home/agentbox/.tmux/plugins/tpm && \
    git clone https://github.com/fabioluciano/tmux-tokyo-night /home/agentbox/.tmux/plugins/tmux-tokyo-night && \
    cd /home/agentbox/.tmux/plugins/tmux-tokyo-night && git checkout -q fcfde9a
# Powerlevel10k, system-wide, plus oh-my-zsh for the app user — the same
# prompt as the operator's dotfiles (os_icon, dir, vcs, prompt_char).
#
# gitstatusd is pre-fetched at build time. hill90-app dropped the `vcs`
# segment because the daemon was not bundled, which loses the git branch
# and status icons; fetching it here keeps them. Without the daemon p10k
# would try to download it on first prompt, at runtime, with no network.
RUN git clone --depth=1 https://github.com/romkatv/powerlevel10k.git /usr/share/powerlevel10k && \
    su agentbox -s /bin/sh -c '/usr/share/powerlevel10k/gitstatus/install -f' && \
    su agentbox -s /bin/sh -c 'sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended'

COPY theme/tmux.conf /home/agentbox/.tmux.conf
# A .zshrc must exist or zsh runs its first-run setup wizard, which eats
# the opening keystrokes of every new terminal session.
COPY theme/zshrc /home/agentbox/.zshrc
COPY theme/p10k.zsh /home/agentbox/.p10k.zsh
RUN chown -R agentbox:agentbox /home/agentbox
# Pre-install plugins at build time so the first terminal session does not
# pay for it (and works with no network at runtime).
RUN su agentbox -s /bin/sh -c 'tmux start-server && tmux new-session -d -s build-init && \
    /home/agentbox/.tmux/plugins/tpm/bin/install_plugins && tmux kill-server' || true

# Install Python dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

# Install the Chromium browser binary
ENV PLAYWRIGHT_BROWSERS_PATH=/data/browsers
RUN playwright install chromium

# Copy application source
COPY src/ src/

# Screenshots are written here (mounted as a named volume in compose).
# Ownership is set in the image so a FRESH volume inherits it; the
# entrypoint fixes volumes that pre-date the non-root switch.
RUN mkdir -p /workspace/screenshots && \
    chown -R agentbox:agentbox /workspace /data/browsers

# HOME/USER are read by the terminal when it builds the shell's
# environment, so they must describe the user the server actually runs as.
ENV HOME=/home/agentbox
ENV USER=agentbox

COPY docker-entrypoint.sh /usr/local/bin/agentbox-entrypoint
COPY git-credential-helper.sh /usr/local/bin/agentbox-git-credential
RUN chmod +x /usr/local/bin/agentbox-entrypoint /usr/local/bin/agentbox-git-credential

# CRITICAL: Unbuffered output for streaming
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

ENTRYPOINT ["/usr/local/bin/agentbox-entrypoint"]
CMD ["python", "src/mcp_server.py"]
