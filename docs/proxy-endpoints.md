# Proxy endpoints

`--agent-proxy` and `--internet-proxy` are independent. Either can be used alone,
or both together. The agent option selects an authenticated model gateway; the
Internet option routes proxy-aware Internet requests and requires the firewall.
Internet-only sessions retain ordinary provider credentials and need no gateway
key or model discovery.

| Endpoint | Accepted | Sandbox route |
| --- | --- | --- |
| `http://127.0.0.1:4000` or `http://localhost:4000` | Yes | Forwarded through `host.docker.internal` |
| `http://[::1]:4000` | Yes | Same forwarding, IPv6 host listener |
| HTTPS on any loopback spelling | No | Local TLS forwarding is unsupported |
| `http://proxy.corp.example` | Yes, port 80 | Direct from sandbox |
| `https://proxy.corp.example` | Yes, port 443 | Direct, verified TLS |
| `https://10.20.30.40:8443` or `https://[2001:db8::1]:8443` | Yes | Direct; certificate must match the IP |

Explicit ports override defaults. Agent API paths such as `/api/v1` are retained;
Internet proxy paths must be empty or `/`. Userinfo (including empty userinfo),
queries, fragments, malformed hostnames, ambiguous numeric addresses, scoped IPv6,
unspecified/wildcard addresses, and multicast are rejected. Canonical localhost
(case-insensitive, optional trailing dot), the IPv4 loopback range, IPv6 loopback,
and mapped IPv4 loopback all use local forwarding. Private unicast addresses are
valid remote endpoints. The sandbox must already have a route to those endpoints;
no particular network provider or VPN setup is required by Project Sandbox.

Only local services need runtime host alias setup. Remote services can share a
port; two local forwarding listeners cannot. Remote-only Apple sessions do not
need the administrator-managed localhost DNS entry.

## DNS and firewall lifetime

Before closing DNS, the sandbox resolves each endpoint through its own resolver,
validates all returned addresses, and pins the admitted addresses in `/etc/hosts`.
Each endpoint gets only its own TCP address/port rules, including usable IPv4 and
IPv6 answers. Names resolving to loopback, unspecified, or multicast addresses
fail startup. Numeric endpoints need no DNS. Failure to enforce IPv6 denial in
Internet mode aborts startup; an unusable IPv6-only endpoint also aborts.

Pins last for the session. Restart to pick up DNS rotation. General outbound DNS
stays blocked. The firewall authorizes an **IP address and port**, so another
virtual host on that same pair shares the exception. TLS checks server identity
for clients using TLS; it does not turn the firewall into a hostname filter.
Agent-only `--no-firewall` uses ordinary resolution without pinning or egress
guarantees. Explicit domain additions remain available in agent-only mode;
Internet mode rejects them, `--allow-github`, chroot, and `--no-firewall`.

Gateway traffic bypasses the Internet proxy via identical `NO_PROXY`/`no_proxy`
values containing the gateway hostname (original and canonical spellings, both
bare and bracketed, for IPv6 client compatibility). Local gateway or Ollama traffic includes
`host.docker.internal` once. Upper/lowercase proxy variables match, and policy
values take precedence over generic environment injection. Disabling either
service does not change the other's route or open direct Internet fallback.

## TLS and trust

Both host preflight and final sandbox checks verify TLS certificate chains and
names. HTTPS Internet preflight is a bounded TLS handshake, without a public test
destination. HTTP uses a bounded TCP check. Gateway discovery makes a direct,
authenticated `/models` request regardless of ambient host proxy variables and
never follows redirects. After firewall setup, the gateway probe uses the same
scheme, host, port, and API path. Host success does not guarantee sandbox success;
a failed final check prevents agent startup.

Use repeatable `--ca-cert PATH` with either proxy to add public CA certificates.
The host loads these into connection-specific TLS contexts without changing its
trust store. Generated sandbox and devcontainer images install them into system
trust; Node receives `NODE_USE_SYSTEM_CA=1`. Inputs must be individual CA PEMs;
private keys, bundles, malformed data, and leaf certificates are rejected.
Certificates are readable public image contents and expand image-wide trust.
Private client trust stores may need separate configuration.

Rebuild images and devcontainers after changing CA inputs. `--no-build` retains
the existing image trust, even though host preflight uses the new CA inputs.
Certificates are installed after build steps and do not change build-time trust.

TLS **to the proxy** protects the sandbox-to-proxy connection. TLS **to the
destination** authenticates the requested origin through a CONNECT tunnel. A CA
input alone does not enable TLS interception. There is no verification bypass,
HTTP downgrade, automatic direct fallback, client-certificate support, or Internet
proxy authentication. HTTP 407 fails the request without obtaining credentials or
forwarding the gateway bearer key. Remote HTTP is an explicit unencrypted choice,
including for gateway bearer traffic; use HTTPS when that transport needs protection.

## Client compatibility

Controlled fixtures exercise curl HTTP requests and CONNECT through both HTTP
and HTTPS proxies, and Node built-in fetch with `NODE_USE_ENV_PROXY=1`. Gateway
fixtures exercise direct authenticated Python discovery and Node fetch bypass.
See [development](development.md#proxy-endpoint-verification) for tested versions
and runtime limitations. Clients with their own proxy/trust implementation need
verification; environment variables alone cannot make an unsupported client work.
The firewall denies unsupported direct attempts instead of providing a fallback.
