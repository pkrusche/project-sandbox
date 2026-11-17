# Squid fixture for scripts/e2e-copilot-egress.py. Same candidate Squid as the
# egress sidecar (egress-sidecar-macos design.md Decision 12).
# Pins track src/project_sandbox/sidecar_image.py (scripts/update-pins.py).
FROM alpine:3.24.4@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf
RUN apk add --no-cache squid~7.6 openssl su-exec
COPY entrypoint.sh /usr/local/bin/fixture-entrypoint
RUN chmod 0755 /usr/local/bin/fixture-entrypoint
ENTRYPOINT ["/usr/local/bin/fixture-entrypoint"]
