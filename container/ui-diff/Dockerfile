# The image the UI diff's run step uses with visual_outline.ui_diff.container
# (docs/operations.md, "UI diff (ui-diff)"): Playwright's browsers, git (ui-diff
# makes worktrees of the clone), CJK fonts, and ui-diff at a pinned commit.
#
#   docker build -t rol-ui-diff:3075b4c container/ui-diff
#
# The Playwright version must be the one ui-diff's package-lock.json resolves,
# so its browsers are the ones baked in here.
ARG PLAYWRIGHT_VERSION=1.65.1
FROM mcr.microsoft.com/playwright:v${PLAYWRIGHT_VERSION}-noble

ARG UI_DIFF_REPOSITORY=https://github.com/imuratalpay/ui-diff.git
ARG UI_DIFF_COMMIT=3075b4c8bbbf1a352404829f84f64c26cba291e5

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*

RUN git clone "$UI_DIFF_REPOSITORY" /opt/ui-diff \
 && git -C /opt/ui-diff checkout --detach "$UI_DIFF_COMMIT" \
 && cd /opt/ui-diff/skills/ui-diff \
 && npm ci --no-audit --no-fund \
 && npm cache clean --force

# The run is not root (it gets the daemon's own ids): the clone goes to /work and
# a new cache volume mounted at /cache starts from this directory, so both are
# open to any user (sticky, like /tmp).
RUN mkdir -p /work /cache && chmod 1777 /work /cache
WORKDIR /work
