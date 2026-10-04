# =============================================================================
# agent-runner
# -----------------------------------------------------------------------------
# Debian trixie image with everything the poller needs:
#   * glab     - GitLab CLI, used for every GitLab API call
#   * opencode - the coding agent (V2)
#   * python3  - runs the poller
# The container entrypoint is agent.py, which polls GitLab and runs opencode.
# =============================================================================
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/root \
    PATH="/root/.opencode/bin:${PATH}" \
    PYTHONUNBUFFERED=1

# Base tooling + glab (Debian ships it in trixie).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      glab \
      openssh-client \
      python3 \
      tini \
 && rm -rf /var/lib/apt/lists/*

# OpenCode CLI (V2 native binary, installed into $HOME/.opencode/bin).
RUN curl -fsSL https://opencode.ai/v2/install | bash -s -- --no-modify-path \
 && opencode --version

COPY agent.py /usr/local/bin/agent.py
RUN chmod +x /usr/local/bin/agent.py

RUN mkdir -p /app /workspace

WORKDIR /workspace

# tini reaps orphaned children and forwards signals to the poller.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/agent.py"]
