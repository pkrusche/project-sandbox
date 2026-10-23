# TODO - outstanding items for next release

- Consider authenticated Internet proxies without exposing credentials or forwarding gateway keys; this release intentionally rejects URL credentials and has no authentication negotiation.
- Consider privileged DNS refresh for long sessions; current endpoint pins require a session restart after DNS changes.
- Run the opt-in `proxy-endpoints` suite on Docker, Podman, and Apple container, including its native IPv6 scenario. Separately verify pinned bundled Pi/OpenCode versions, DNS-name pinning, and IPv6-disabled fallback. The implementation environment has no container runtime or network-administration capabilities.
- Run the opt-in `build-proxy` suite on Apple container behind a VPN-only proxy to confirm the host-loopback build proxy forwarder end to end, including an `https://` proxy URL.
- Confirm on Docker BuildKit, Apple container's builder, and Podman/buildah that the undeclared predefined proxy build-args reach `RUN` steps (including apt's env-based HTTPS proxy and `no_proxy`) and are excluded from the layer cache, so a changed proxy URL or forwarder port reuses cached layers.
