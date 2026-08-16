# Agent proxy

`project-sandbox` can route Pi, OpenCode, and interactive or headless Bash
through the authenticated local
gateway maintained by [`pkrusche/agentgateway-locally`](https://github.com/pkrusche/agentgateway-locally).
Install, configure, secure, start, rotate, and stop that gateway by following
its [setup](https://github.com/pkrusche/agentgateway-locally#readme),
[backend](https://github.com/pkrusche/agentgateway-locally), and security and
request-log guidance. This project does not access that checkout or invoke its
`run.py`, and it does not reproduce the gateway YAML.

The OpenAI-compatible LLM endpoint is loopback port 4000 under `/v1`. Port
3000 is MCP, not an LLM endpoint. With the gateway running:

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
both Pi and OpenCode with the forwarded URL, gateway key, discovered models,
and selected default model, so either agent can be launched directly from the
shell without additional provider setup.

Proxy mode is gateway-only by default. The firewall omits the normal OpenAI and
Anthropic endpoint allowlist and the devcontainer's broad host-gateway rule;
only the forwarded gateway address and selected TCP port are reachable. Add
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

On Apple `container`, both agent-proxy and Ollama forwarding use
`host.docker.internal`. Configure that name once before using either feature:

```bash
sudo container system dns create host.docker.internal --localhost 203.0.113.113
```

This DNS change might disable network connectivity. Restart the container
system after creating it:

```bash
container system stop
container system start
```
