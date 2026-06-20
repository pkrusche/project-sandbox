"""Render a human-readable markdown transcript from a Claude stream-json log.

Headless claude runs emit newline-delimited JSON (one event per line) via
`claude -p --output-format stream-json --verbose`. The session log also holds
plain-text preamble from the container entrypoint and firewall, so the parser
tolerates and skips any line that is not a JSON object.
"""

import json
from pathlib import Path

# Tool results (and to a lesser extent tool inputs) can be enormous. Cap them so
# the transcript stays readable; the full payload remains in the .log sidecar.
_MAX_BLOCK_CHARS = 2000


def log_to_markdown(log_path: Path) -> Path | None:
    """Parse a stream-json session log and write a markdown sidecar next to it.

    Returns the markdown path on success, or None if the log held no parseable
    Claude events (e.g. a non-claude agent or a run that never started).
    """
    events = _parse_events(log_path.read_text(encoding="utf-8", errors="replace"))
    if not _has_claude_events(events):
        return None
    md_path = log_path.with_suffix(".md")
    md_path.write_text(render_markdown(events), encoding="utf-8")
    return md_path


def _parse_events(text: str) -> list[dict]:
    events: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "type" in obj:
            events.append(obj)
    return events


def _has_claude_events(events: list[dict]) -> bool:
    return any(e.get("type") in ("system", "assistant", "result") for e in events)


def render_markdown(events: list[dict]) -> str:
    parts: list[str] = ["# Claude session transcript", ""]
    tool_names: dict[str, str] = {}

    for event in events:
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            parts.extend(_render_init(event))
        elif etype == "assistant":
            parts.extend(_render_assistant(event, tool_names))
        elif etype == "user":
            parts.extend(_render_user(event, tool_names))
        elif etype == "result":
            parts.extend(_render_result(event))

    # Collapse runs of blank lines and guarantee a trailing newline.
    text = "\n".join(parts)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip() + "\n"


def _render_init(event: dict) -> list[str]:
    rows = []
    if event.get("session_id"):
        rows.append(f"- **Session:** `{event['session_id']}`")
    if event.get("model"):
        rows.append(f"- **Model:** {event['model']}")
    if event.get("cwd"):
        rows.append(f"- **Working dir:** `{event['cwd']}`")
    if not rows:
        return []
    return [*rows, "", "---", ""]


def _render_assistant(event: dict, tool_names: dict[str, str]) -> list[str]:
    out: list[str] = []
    for block in _content_blocks(event):
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "").strip()
            if text:
                out.extend(["## Assistant", "", text, ""])
        elif btype == "thinking":
            thinking = block.get("thinking", "").strip()
            if thinking:
                out.extend(["## Assistant (thinking)", "", _blockquote(thinking), ""])
        elif btype == "tool_use":
            name = block.get("name", "tool")
            tool_id = block.get("id", "")
            if tool_id:
                tool_names[tool_id] = name
            out.extend(_render_tool_use(name, block.get("input", {})))
    return out


def _render_tool_use(name: str, tool_input: object) -> list[str]:
    rendered = json.dumps(tool_input, indent=2, ensure_ascii=False, sort_keys=True)
    return [
        f"### 🔧 {name}",
        "",
        *_code_block(_truncate(rendered), "json"),
        "",
    ]


def _render_user(event: dict, tool_names: dict[str, str]) -> list[str]:
    out: list[str] = []
    for block in _content_blocks(event):
        if block.get("type") != "tool_result":
            continue
        name = tool_names.get(block.get("tool_use_id", ""), "tool")
        label = "error" if block.get("is_error") else "result"
        out.extend(
            [
                f"### ↳ {name} {label}",
                "",
                *_code_block(_truncate(_tool_result_text(block.get("content")))),
                "",
            ]
        )
    return out


def _render_result(event: dict) -> list[str]:
    out = ["---", "", "## Result", ""]
    result_text = event.get("result")
    if isinstance(result_text, str) and result_text.strip():
        out.extend([result_text.strip(), ""])
    stats = []
    if event.get("is_error"):
        stats.append("- **Status:** error")
    if isinstance(event.get("duration_ms"), (int, float)):
        stats.append(f"- **Duration:** {event['duration_ms'] / 1000:.1f}s")
    if event.get("num_turns") is not None:
        stats.append(f"- **Turns:** {event['num_turns']}")
    if isinstance(event.get("total_cost_usd"), (int, float)):
        stats.append(f"- **Cost:** ${event['total_cost_usd']:.4f}")
    if stats:
        out.extend([*stats, ""])
    return out


def _content_blocks(event: dict) -> list[dict]:
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _tool_result_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(block.get("text", ""))
            elif isinstance(block, dict):
                chunks.append(f"[{block.get('type', 'content')}]")
        return "\n".join(chunks)
    return str(content)


def _code_block(text: str, info: str = "") -> list[str]:
    """Wrap text in a fence longer than any backtick run it contains.

    A fenced block is closed by the first line holding at least as many
    backticks as its opening fence, so tool output containing ``` would
    otherwise break out and inject arbitrary Markdown/HTML into the sidecar.
    Size the fence to one more backtick than the longest run in the body.
    """
    longest = 0
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{info}", text, fence]


def _blockquote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _truncate(text: str) -> str:
    if len(text) <= _MAX_BLOCK_CHARS:
        return text
    omitted = len(text) - _MAX_BLOCK_CHARS
    return text[:_MAX_BLOCK_CHARS] + f"\n… ({omitted} more characters truncated)"
