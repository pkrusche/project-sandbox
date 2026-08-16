# Agent proxy

`project-sandbox` can route Pi and OpenCode through the authenticated local
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
```

The gateway key is resolved from the first non-empty source: `pass show
agentgateway-api-key`, the variable selected by `--agent-proxy-key-env`
(default `AGENTGATEWAY_API_KEY`), then `--agent-proxy-key`. The raw option is a
last resort: although project-sandbox redacts its output, the invoking shell,
history, and process listings can already expose argv. Prefer `pass` or the
environment. Rotate the key using the gateway's procedure. HTTP 401/403 means
the listener rejected it; update the pass entry/export, then retry.

The CLI authenticates to `/models`, validates the selected model, and configures
only the selected agent. `--dry-run` performs no pass/environment lookup,
network access, file write, or container start. For a real, billable two-agent
check run `scripts/check-agent-proxy.py`; it makes one minimal LLM request with
each supported agent.
