# Squid fixture for scripts/e2e-copilot-egress.py. Same candidate Squid as the
# egress sidecar (egress-sidecar-macos design.md Decision 12): Alpine 3.22.1 + squid 6.12.
# Pins track src/project_sandbox/sidecar_image.py (scripts/update-pins.py).
FROM alpine:3.22.1@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1
RUN apk add --no-cache squid~6.12 openssl su-exec
COPY entrypoint.sh /usr/local/bin/fixture-entrypoint
RUN chmod 0755 /usr/local/bin/fixture-entrypoint
ENTRYPOINT ["/usr/local/bin/fixture-entrypoint"]
