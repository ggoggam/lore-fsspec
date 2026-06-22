# Self-contained loreserver image for the integration tests (driven by
# testcontainers from tests/conftest.py). Replaces the old "download the binary
# and run it on the host" mise script.
#
# We pin linux/amd64 deliberately: the published arm64 release asset is a
# Neoverse/SVE-512 build (AWS Graviton) that raises SIGILL on Apple Silicon, so
# the x86_64 binary is the only portable option. It runs natively on the CI
# runners and under emulation locally.
#
# Single stage on purpose: the final image needs curl + ca-certificates for the
# HEALTHCHECK regardless, so a builder stage would not save anything.
#
# The platform is pinned by the caller via `docker build --platform linux/amd64`
# (conftest.py does this); we keep the base image plain so the build doesn't warn
# about a constant FROM --platform.
#
# Keep LORE_SERVER_VERSION in lockstep with the lore-vcs (liblore) wheel pinned
# in pyproject.toml; conftest.py passes it through as a build arg.
FROM debian:bookworm-slim
ARG LORE_SERVER_VERSION=0.8.3

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && url="https://github.com/EpicGames/lore/releases/download/v${LORE_SERVER_VERSION}/loreserver-v${LORE_SERVER_VERSION}-x86_64-unknown-linux-gnu.tar.gz" \
 && curl -fsSL "$url" -o /tmp/lore.tar.gz \
 && tar -xzf /tmp/lore.tar.gz -C /tmp \
 && install -m 0755 "$(find /tmp -type f -name loreserver | head -n1)" /usr/local/bin/loreserver \
 && rm -rf /tmp/lore.tar.gz /tmp/loreserver*

# gRPC + QUIC share 41337 (TCP and UDP); HTTP health check is on 41339.
EXPOSE 41337/tcp 41337/udp 41339/tcp

HEALTHCHECK --interval=2s --timeout=3s --start-period=2s --retries=30 \
  CMD curl -fsS http://127.0.0.1:41339/health_check || exit 1

# Zero-config: env=local, auth disabled, ephemeral self-signed cert + temp
# store, all listeners bound to 0.0.0.0.
CMD ["loreserver"]
