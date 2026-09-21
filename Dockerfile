# syntax=docker/dockerfile:1
#
# gitlab-agent runtime image
# ---------------------------------------------------------------------------
# Base ....... Debian 13 "trixie" (slim)
# Ships ...... OpenCode v2 CLI        (@opencode/cli)
#              GitLab CLI            (glab, pinned release)
#              gitlab-agent bot      (agent.py, Python)
#
# The container is the bot: `podman run --env-file .env ...` starts agent.py,
# which works the GitLab to-do list through `glab` and calls `opencode run`
# directly. No host-side tooling and no nested containers are required.
#
# All configuration and secrets (GitLab personal access token, GitLab API URL,
# model-provider API keys) are supplied through the environment at runtime.
# Nothing secret is baked into the image. See .env.example.
#
# Built for Podman (also works with Docker):
#   podman build -t localhost/gitlab-agent:latest .
# ---------------------------------------------------------------------------

FROM debian:trixie-slim

# A bare major tracks the latest OpenCode release in that series. glab is
# pinned to a release tag: Debian trixie only ships an old version.
ARG OPENCODE_VERSION=2
ARG GLAB_VERSION=1.118.0

ENV DEBIAN_FRONTEND=noninteractive \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    HOME=/root

# nodejs + npm ....... OpenCode v2
#                      (Debian trixie ships Node 20 / npm 9, satisfies >=18.17)
# python3 ............ the gitlab-agent bot (agent.py, stdlib only)
# git / openssh ...... repository access from the agent
# ripgrep ............ fast search used by OpenCode's tools
# tini ............... PID 1 that forwards signals and reaps zombies
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      git \
      gnupg \
      nodejs \
      npm \
      openssh-client \
      python3 \
      ripgrep \
      tar \
      tini \
      unzip \
 && rm -rf /var/lib/apt/lists/*

# Install OpenCode v2 (npm package @opencode/cli).
RUN npm install -g --no-audit --no-fund "@opencode/cli@${OPENCODE_VERSION}" \
 && npm cache clean --force \
 && /usr/local/bin/opencode --version

# Install the pinned glab release for this architecture.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) glab_arch=amd64 ;; \
      arm64) glab_arch=arm64 ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL \
      "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_${glab_arch}.tar.gz" \
      -o /tmp/glab.tar.gz; \
    tar -xzf /tmp/glab.tar.gz -C /tmp; \
    install -m 0755 /tmp/bin/glab /usr/local/bin/glab; \
    rm -rf /tmp/glab.tar.gz /tmp/bin; \
    glab --version

# Point git at glab's credential helper so authenticated clone/push work from
# GITLAB_TOKEN/GITLAB_HOST alone (no `glab auth login`), and give commits an
# identity. Both can be overridden at runtime.
RUN git config --system credential.helper '!glab auth git-credential' \
 && git config --system user.name 'gitlab-agent' \
 && git config --system user.email 'gitlab-agent@localhost' \
 && git config --system init.defaultBranch main

# Non-interactive glab defaults: HTTPS for git (the image has no SSH keys), no
# update checks and no prompts. The instance and token come from the
# environment at runtime.
RUN glab config set git_protocol https --global \
 && glab config set no_prompt true --global \
 && glab config set check_update false --global \
 && glab --version

# Global OpenCode config, and the bot itself.
COPY opencode.json /root/.config/opencode/opencode.json
COPY agent.py /usr/local/bin/gitlab-agent
RUN chmod 0755 /usr/local/bin/gitlab-agent

WORKDIR /workspace

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/gitlab-agent"]
