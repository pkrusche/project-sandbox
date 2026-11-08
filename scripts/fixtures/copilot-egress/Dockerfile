# Squid fixture for scripts/e2e-copilot-egress.py. Same candidate Squid as the
# egress sidecar (egress-sidecar-macos design.md Decision 12): Alpine 3.22.1 + squid 6.12.
# Pins track src/project_sandbox/sidecar_image.py (scripts/update-pins.py).
FROM alpine:3.24.3@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a
RUN apk add --no-cache squid~7.6 openssl su-exec
COPY entrypoint.sh /usr/local/bin/fixture-entrypoint
RUN chmod 0755 /usr/local/bin/fixture-entrypoint
ENTRYPOINT ["/usr/local/bin/fixture-entrypoint"]
