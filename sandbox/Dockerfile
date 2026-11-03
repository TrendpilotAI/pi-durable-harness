# ---- Stage 1: Builder ----
FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml .
COPY app/ app/

RUN apt-get update \
    && apt-get install -y --no-install-recommends libseccomp-dev gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "uv==0.11.26" \
    && uv pip compile pyproject.toml --extra runtime -o /tmp/requirements.txt \
    && pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt \
    && pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

# ---- Stage 1b: Node runtime for the harness (hyp-90f.2 spike) ----
# Pinned Node 22 (pi needs >=22.19). Copied into the Python image below rather
# than apt-installed, so the version is explicit and the toolchain small.
FROM node:22-bookworm-slim AS node

# ---- Stage 2: Runtime ----
FROM python:3.11-slim

# util-linux provides `unshare` for the best-effort network namespace layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends util-linux wget libseccomp2 \
    && rm -rf /var/lib/apt/lists/*

# Harness runtime baked into the image (hyp-90f.2): node + pi + the fetch
# shim under /opt/harness, world-readable, root-owned so no session uid can
# modify it. /workspace/_shared/harness/ stays reserved for roll-out without
# rebuilds (production build-out, hyp-90f.3).
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
#
# pi is pinned exact in harness/package.json; a bump requires
# test_pi_completes_via_unix_socket_proxy to pass in the image
# (sandbox/scripts/test-in-image.sh); it fails loudly if the fetch bypass
# the shim relies on stops holding.
#
# Prune (hyp-90f.6): the CLI the sandbox runs, dist/bundle/cli.js, is an
# esbuild bundle with every provider SDK (openai, @anthropic-ai/sdk,
# @google/genai, @aws-sdk/client-bedrock-runtime) inlined. The copies under
# pi's nested node_modules only serve pi's library entry (dist/index.js),
# which nothing in the sandbox imports. pi ships its own npm-shrinkwrap.json,
# so `--omit`, `overrides` and `npm prune` cannot reach that tree; the list
# below is the provider SDKs plus their exclusive transitive closure, computed
# from package-lock.json. Names absent after a bump are ignored; new deps just
# stay until the list is refreshed. @silvia-odwyer/photon-node and jiti stay:
# the bundle loads both by name at runtime.
COPY harness/package.json harness/package-lock.json harness/shim.mjs /opt/harness/
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && cd /opt/harness \
    && npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && npm cache clean --force \
    && cd /opt/harness/node_modules/@earendil-works/pi-coding-agent/node_modules \
    && rm -rf @anthropic-ai @aws @aws-crypto @aws-sdk @babel @google @nodable @protobufjs \
        @smithy @stablelib @types \
        agent-base base64-js bignumber.js bowser buffer-equal-constant-time \
        data-uri-to-buffer debug ecdsa-sig-formatter extend fast-sha256 fast-xml-builder \
        fast-xml-parser fetch-blob formdata-polyfill gaxios gcp-metadata google-auth-library \
        google-logging-utils http-proxy-agent https-proxy-agent json-bigint json-schema-to-ts \
        jwa jws long ms node-domexception node-fetch openai p-retry partial-json \
        path-expression-matcher protobufjs retry safe-buffer standardwebhooks strnum \
        ts-algebra tslib undici-types web-streams-polyfill ws xml-naming \
    && chmod -R a+rX /opt/harness \
    && mkdir -p /run/hypebase && chmod 711 /run/hypebase

# No dedicated runtime user: the service stays container-root and drops each
# child to a numeric per-session uid (>= 100000, no passwd entry) that owns
# that session's directory. See app/executor.py for the trade-off.

WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels hypebase-sandbox pandas numpy matplotlib \
    && rm -rf /wheels
COPY app/ app/

RUN mkdir -p /workspace && chmod 755 /workspace

EXPOSE 8100
# app.serve reads PORT (8100 default) and binds one dual-stack socket: Railway's
# private network is IPv6-only in legacy environments (production) while its
# health checks and edge arrive over IPv4, and `uvicorn --host ::` would be
# IPv6-only (asyncio sets IPV6_V6ONLY). See docs/DEPLOY.md.
CMD ["python", "-m", "app.serve"]
