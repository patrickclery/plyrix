# plyrix's image. Build it locally with `docker compose build plyrix`. Released images are built by
# .github/workflows/release.yml and published at ghcr.io/patrickclery/plyrix; LRCLIB's image is
# never published.
#
# Every base image is pinned by digest and supercronic by sha256 per architecture. There is no
# syntax line, as that would pull an unpinned frontend image. The build context is the allow-list
# in .dockerignore, and every COPY below names its paths.

FROM astral/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/plyrix \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
# The venv is made at /opt/plyrix, where it runs, as a venv copied from another path keeps that
# path in its scripts' shebangs.
RUN uv sync --locked --no-dev --no-editable --python /usr/local/bin/python3

# supercronic is fetched in its own stage, so curl stays out of the runtime image.
FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS fetch
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) sum=cc232108374e8e2b537045f2d5eaa1229f619db314c3ba6a14d38b35dc0aa123 ;; \
      arm64) sum=4ae22576a78223d8ccdf5f3b3660a5cbb5288416bf0ca96afc4a36d195932b94 ;; \
      *) echo "no supercronic checksum for $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /supercronic "https://github.com/aptible/supercronic/releases/download/v0.2.51/supercronic-linux-$TARGETARCH"; \
    echo "$sum  /supercronic" | sha256sum -c -; \
    chmod 0755 /supercronic

FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
# tini is PID 1: plyrix installs no SIGTERM handler, and supercronic reaps only as PID 1.
# zlib1g is upgraded from trixie-security, as the pinned base still has the build grype fails on
# (CVE-2026-76844, CVE-2026-85091); drop the upgrade once a base digest carries +deb13u1.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && apt-get install -y --only-upgrade zlib1g \
 && rm -rf /var/lib/apt/lists/*
COPY --from=fetch /supercronic /usr/local/bin/supercronic
COPY --from=build /opt/plyrix /opt/plyrix
RUN mkdir -p /config /state \
 && chown 1000:1000 /config /state
ENV PATH=/opt/plyrix/bin:$PATH \
    XDG_CONFIG_HOME=/config \
    XDG_STATE_HOME=/state \
    CRON_SCHEDULE=@weekly
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/plyrix-entrypoint
# No user-switching logic; compose's user: runs it as another uid.
USER 1000:1000
ENTRYPOINT ["/usr/bin/tini", "--", "plyrix-entrypoint"]
