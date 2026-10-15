# Agent proxy

`project-sandbox` can route Pi, OpenCode, and interactive or headless Bash
through an authenticated local or remote HTTP/HTTPS gateway. One local option is [`pkrusche/agentgateway-locally`](https://github.com/pkrusche/agentgateway-locally).
Install, configure, secure, start, rotate, and stop that gateway by following
its [setup](https://github.com/pkrusche/agentgateway-locally#readme),
[backend](https://github.com/pkrusche/agentgateway-locally), and security and
request-log guidance. This project does not access that checkout or invoke its
`run.py`, and it does not reproduce the gateway YAML.

The OpenAI-compatible LLM endpoint is loopback port 4000 under `/v1`. Port
3000 is MCP, not an LLM endpoint. Always include the `/v1` suffix in
`--agent-proxy`; omitting it sends model discovery and provider requests to
the wrong path and they will fail. With the gateway running:

```bash
project-sandbox . python:3.14 --agent pi --agent-proxy http://127.0.0.1:4000/v1 \
  --model gpt-5-mini --prompt-text 'Reply with OK'
project-sandbox . python:3.14 --agent opencode --agent-proxy http://127.0.0.1:4000/v1 \
  --model agent-proxy/gpt-5-mini --prompt-text 'Reply with OK'
project-sandbox . python:3.14 --agent bash --agent-proxy http://127.0.0.1:4000/v1 \
  --model gpt-5-mini
project-sandbox . python:3.14 --agent bash --agent-proxy http://127.0.0.1:4000/v1 \
  --model gpt-5-mini --prompt-text 'python scripts/use_openai_client.py'
```

Bash receives `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL`. The same
environment is available interactively and to commands executed by headless
`--prompt` / `--prompt-text` sessions. The API-key value is the narrower gateway
bearer key, not an OpenAI provider key. Bash proxy sessions also pre-configure
both Pi and OpenCode with the effective sandbox URL, gateway key, discovered models,
and selected default model, so either agent can be launched directly from the
shell without additional provider setup. OpenCode's configured proxy models
use the `@ai-sdk/openai` provider mapping so requests use the gateway's
Responses API rather than chat completions. Images that include OpenCode also
install an exact, OpenCode-compatible version of that provider package.

Proxy mode is gateway-only by default. The firewall omits the normal OpenAI and
Anthropic endpoint allowlist and the devcontainer's broad host-gateway rule;
only the gateway’s admitted addresses and selected TCP port are reachable. Add
other destinations deliberately with `--extra-domain DOMAIN` or
`--allow-github`. No provider endpoint is added automatically, including when a
headless Bash command invokes another CLI.

The gateway key is resolved from the first non-empty source: `pass show
agentgateway-api-key`, the variable selected by `--agent-proxy-key-env`
(default `AGENTGATEWAY_API_KEY`), then `--agent-proxy-key`. The raw option is a
last resort: although project-sandbox redacts its output, the invoking shell,
history, and process listings can already expose argv. Prefer `pass` or the
environment. Rotate the key using the gateway's procedure. HTTP 401/403 means
the listener rejected it; update the pass entry/export, then retry.

The CLI authenticates to `/models` and validates the selected model. For Bash
proxy sessions it configures both Pi and OpenCode; for other sessions it
configures the selected agent. `--dry-run` performs no pass/environment lookup,
network access, file write, or container start.

Run the non-billable isolation check with a real container runtime:

```bash
uv run python scripts/e2e-agent-proxy-isolation.py --runtime docker
```

It checks that the gateway remains reachable while OpenAI, Anthropic, GitHub,
and an unrelated public host are unreachable. It also checks that only the
injected gateway key is present as credential-like environment state, that Pi
and OpenCode contain the expected proxy URL, model, and key, and that known host
credential files or extra mounted secret files are absent. For a real,
billable two-agent check run `scripts/check-agent-proxy.py`; it makes one
minimal LLM request with each supported agent.

For local HTTP services on Apple `container`, agent-proxy and Ollama forwarding use
`host.docker.internal`. Configure that name once before using either feature:

```bash
sudo container system dns create host.docker.internal --localhost 203.0.113.113
```

This DNS/PF change can disrupt container Internet access until runtime
networking is rebuilt and might disable Private Relay. Restart the container
system after creating it:

```bash
container system stop
container system start
```

If the gateway later becomes unreachable with a curl *timeout* rather than a
refusal, the alias is registered but no longer redirecting. Re-running `dns
create` does not fix that; delete and re-create the domain, then restart the
container system. See [Apple container host alias](apple-container-dns.md).

## Remote gateways

```bash
project-sandbox --agent bash --agent-proxy https://models.corp.example/v1 --model example-model --runtime docker . python:3.12-slim
project-sandbox --agent bash --agent-proxy https://models.corp.example/api/v1 --model example-model --ca-cert /path/to/corporate-ca.pem --runtime docker . python:3.12-slim
```

Remote hosts retain their scheme, hostname, effective port (443 here), and API
path. Only local HTTP endpoints use forwarding; HTTPS loopback is rejected.
Gateway discovery ignores ambient host proxies and rejects redirects. Both host
and sandbox TLS checks verify trust and endpoint identity before agent startup.

The Internet proxy is optional. When both are set, gateway requests bypass it,
and stopping either service leaves the other independently reachable. Remote
names are pinned by the sandbox for the session; restart after DNS changes. The
firewall grants IP/port access, which also covers other virtual hosts on that
same pair. Agent-only `--no-firewall` has ordinary DNS and no pinning guarantees.
See the [URL matrix, CA scope, and client compatibility](proxy-endpoints.md).
