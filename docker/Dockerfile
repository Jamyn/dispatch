FROM python:3.14.0-slim-trixie as sdist

LABEL maintainer="oss@netflix.com"
LABEL org.opencontainers.image.title="Dispatch PyPI Wheel"
LABEL org.opencontainers.image.description="PyPI Wheel Builder for Dispatch"
LABEL org.opencontainers.image.url="https://dispatch.io/"
LABEL org.opencontainers.image.source="https://github.com/netflix/dispatch"
LABEL org.opencontainers.image.vendor="Netflix, Inc."
LABEL org.opencontainers.image.authors="oss@netflix.com"

SHELL ["/bin/bash", "-o", "pipefail", "-o", "errexit", "-c"]

# Get and set up Node for front-end asset building
RUN apt-get update && apt-get install -y --no-install-recommends \
  # Needed for fetching stuff
  ca-certificates \
  wget \
  curl \
  && rm -rf /var/lib/apt/lists/*

# Install uv for building
RUN curl -LsSf https://astral.sh/uv/0.4.17/install.sh | sh && \
  mv /root/.cargo/bin/uv /usr/local/bin/

RUN wget --quiet -O - https://deb.nodesource.com/setup_20.x | bash - \
  && apt-get install -y nodejs --no-install-recommends

ARG SOURCE_COMMIT
ENV DISPATCH_BUILD=${SOURCE_COMMIT:-unknown}
LABEL org.opencontainers.image.revision=$SOURCE_COMMIT
LABEL org.opencontainers.image.licenses="https://github.com/netflix/dispatch/blob/${SOURCE_COMMIT:-main}/LICENSE"

ARG DISPATCH_LIGHT_BUILD
ENV DISPATCH_LIGHT_BUILD=${DISPATCH_LIGHT_BUILD}

RUN echo "DISPATCH_LIGHT_BUILD=${DISPATCH_LIGHT_BUILD}"

# Allow build time variables via --build-arg
ARG VITE_DISPATCH_AUTH_REGISTRATION_ENABLED
ARG VITE_DISPATCH_AUTHENTICATION_PROVIDER_PKCE_CLIENT_ID
ARG VITE_DISPATCH_AUTHENTICATION_PROVIDER_PKCE_OPEN_ID_CONNECT_URL
ARG VITE_DISPATCH_AUTHENTICATION_PROVIDER_SLUG
ARG VITE_DISPATCH_AUTHENTICATION_PROVIDER_USE_ID_TOKEN
ARG VITE_SENTRY_DSN
ARG VITE_SENTRY_APP_KEY
ARG VITE_SENTRY_ENABLED

# Should be replaced in your build process script
ARG VITE_DISPATCH_COMMIT_HASH
ENV VITE_DISPATCH_COMMIT_HASH="Unknown"

ARG VITE_DISPATCH_COMMIT_MESSAGE
ENV VITE_DISPATCH_COMMIT_MESSAGE="Unknown"

ARG VITE_DISPATCH_COMMIT_DATE
ENV VITE_DISPATCH_COMMIT_DATE="Unknown"

COPY . /usr/src/dispatch/
RUN YARN_CACHE_FOLDER="$(mktemp -d)" \
  && export YARN_CACHE_FOLDER \
  && pushd /usr/src/dispatch \
  # Nothing on main builds the frontend: hatchling has no build hook and no
  # dist/ is committed, so the wheel ships no UI unless vite runs here.
  && pushd src/dispatch/static/dispatch \
  && npm ci --no-audit --no-fund \
  && npm run build \
  && popd \
  # --wheel, not a bare `uv build`: that builds an sdist first and then the
  # wheel FROM it, where src/dispatch has been flattened to dispatch/ and the
  # wheel target's packages=["src/dispatch"] matches nothing, yielding a wheel
  # with no package in it at all.
  && uv build --wheel \
  && rm -r "$YARN_CACHE_FOLDER" \
  && mv /usr/src/dispatch/dist /dist

# This is the image to be run
FROM python:3.14.0-slim-trixie

LABEL maintainer="oss@dispatch.io"
LABEL org.opencontainers.image.title="Dispatch"
LABEL org.opencontainers.image.description="Dispatch runtime image"
LABEL org.opencontainers.image.url="https://github.com/netflix/dispatch"
LABEL org.opencontainers.image.documentation="https://github.com/netflix/dispatch"
LABEL org.opencontainers.image.source="https://github.com/netflix/dispatch"
LABEL org.opencontainers.image.vendor="Netflix, Inc."
LABEL org.opencontainers.image.authors="oss@netflix.com"

SHELL ["/bin/bash", "-o", "pipefail", "-o", "errexit", "-c"]

# add our user and group first to make sure their IDs get assigned consistently
RUN groupadd -r dispatch && useradd -r -m -g dispatch dispatch

# Sane defaults for pip
ENV PIP_NO_CACHE_DIR=off \
  PIP_DISABLE_PIP_VERSION_CHECK=1 \
  # Dispatch config params
  DISPATCH_CONF=/etc/dispatch

RUN apt-get update && apt-get install -y --no-install-recommends \
  # Needed for fetching stuff
  ca-certificates \
  wget gnupg curl \
  && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/0.4.17/install.sh | sh && \
  mv /root/.cargo/bin/uv /usr/local/bin/

# apt-key was removed in apt 3.0 (Debian 13), so the pgdg key is installed as a
# keyring file and bound to the repository with signed-by instead.
RUN mkdir -p /etc/apt/keyrings \
  && wget --quiet -O /etc/apt/keyrings/pgdg.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  && echo "deb [signed-by=/etc/apt/keyrings/pgdg.asc] http://apt.postgresql.org/pub/repos/apt trixie-pgdg main" > /etc/apt/sources.list.d/pgdg.list

RUN wget --quiet -O - https://deb.nodesource.com/setup_20.x | bash -

COPY --from=sdist /dist/*.whl /tmp/dist/
COPY requirements-lock.txt /tmp/requirements-lock.txt
RUN buildDeps="" \
  && apt-get update \
  && apt-get install -y --no-install-recommends "$buildDeps" \
  # pyproject.toml leaves most dependencies unconstrained, so resolving at build
  # time makes the image non-reproducible. The lockfile is the full closure, so
  # --no-deps installs it exactly as pinned -- re-resolving here would let sdists
  # that compute their own requirements (dkimpy probes for a DNS module) drift.
  && uv pip install --system --no-deps --require-hashes -r /tmp/requirements-lock.txt \
  && uv pip install --system --no-deps /tmp/dist/*.whl \
  # uv extracts installed wheels into its cache before linking them into
  # site-packages, leaving a second copy of the source tree at
  # /root/.cache/uv/archive-v0/. Unlike site-packages, trivy's secret
  # scanner walks that path as plain filesystem content and flags
  # dispatch_google/common.py's `"type": "service_account"` dict literal
  # as a GCP service-account key -- a false positive, but the underlying
  # cache copy is also just dead weight in the runtime image.
  && rm -rf /root/.cache/uv \
  && apt-get purge -y --auto-remove "$buildDeps" \
  # We install run-time dependencies strictly after
  # build dependencies to prevent accidental collusion.
  # These are also installed last as they are needed
  # during container run and can have the same deps w/
  && apt-get install -y --no-install-recommends \
  # `dispatch database dump` shells out to pg_dump, which refuses to dump from
  # a server newer than itself. Client 18 covers servers 14 through 18; client
  # 14 could not dump from the 18.x server this is deployed against.
  pkg-config postgresql-client-18 nodejs \
  # The pinned base ships openssl below the fixed 3.5.6-1~deb13u2 and nothing
  # here pulls it forward on its own, so trivy's CRITICAL/HIGH gate goes red.
  # Upgrade explicitly, before apt-get clean, so the fix is deterministic.
  && apt-get upgrade -y \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/* \
  # mjml has to be installed differently here because
  # after node 14, docker will install npm files at the
  # root directory and fail, so we have to create a new
  # directory and use it for the install then copy the
  # files to the root directory to maintain backwards
  # compatibility for email generation
  && mkdir -p /mjml_install \
  # if our workdir is /, then pushd/popd doesn't work
  # for the npm install. It still tries to install in /,
  # which npm can't do
  && cd /mjml_install \
  # Pinned to the version src/dispatch/static/dispatch/package-lock.json resolves,
  # so the templates render against the mjml they were authored for.
  && npm install --no-cache-dir mjml@4.15.3 \
  && mv node_modules / \
  && cd / \
  && rm -rf /mjml_install \
  # npm is build-time only; every CRITICAL/HIGH in this image's node tree comes
  # from its bundled dependencies. The mjml CLI needs node, not npm.
  && rm -rf /usr/lib/node_modules/npm /usr/bin/npm /usr/bin/npx

# The wheel no longer carries the frontend's node_modules, so the default
# (a path inside the package) no longer resolves. Point at the tree above.
ENV MJML_PATH=/node_modules/.bin

EXPOSE 8000
VOLUME /var/lib/dispatch/files

ENTRYPOINT ["dispatch"]
CMD ["server", "start", "dispatch.main:app", "--host=0.0.0.0"]

ARG SOURCE_COMMIT
LABEL org.opencontainers.image.revision=$SOURCE_COMMIT
LABEL org.opencontainers.image.licenses="https://github.com/netflix/dispatch/blob/${SOURCE_COMMIT:-main}/LICENSE"
