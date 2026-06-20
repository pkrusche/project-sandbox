import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import transcript


def _log_lines(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


# A realistic stream-json session: container preamble, init, an assistant turn
# with text + a tool call, the tool result, and the final result event.
SAMPLE_EVENTS = [
    {
        "type": "system",
        "subtype": "init",
        "session_id": "sess-123",
        "model": "claude-opus-4-8",
        "cwd": "/workspace",
        "tools": ["Read", "Bash"],
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll read the file."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Read",
                    "input": {"file_path": "/workspace/foo.py"},
                },
            ],
        },
    },
    {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "print('hello')",
                    "is_error": False,
                }
            ],
        },
    },
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Done reading the file.",
        "duration_ms": 4200,
        "num_turns": 2,
        "total_cost_usd": 0.0123,
    },
]


class TranscriptRenderTests(TestCase):
    def test_render_includes_header_text_tool_and_result(self) -> None:
        md = transcript.render_markdown(SAMPLE_EVENTS)

        self.assertIn("# Claude session transcript", md)
        self.assertIn("- **Session:** `sess-123`", md)
        self.assertIn("- **Model:** claude-opus-4-8", md)
        self.assertIn("I'll read the file.", md)
        # Tool call rendered with name and JSON input.
        self.assertIn("### 🔧 Read", md)
        self.assertIn('"file_path": "/workspace/foo.py"', md)
        # Tool result correlated back to the tool name.
        self.assertIn("### ↳ Read result", md)
        self.assertIn("print('hello')", md)
        # Final result and stats.
        self.assertIn("## Result", md)
        self.assertIn("Done reading the file.", md)
        self.assertIn("- **Duration:** 4.2s", md)
        self.assertIn("- **Turns:** 2", md)
        self.assertIn("- **Cost:** $0.0123", md)

    def test_error_tool_result_is_labelled(self) -> None:
        events = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "x",
                            "content": "boom",
                            "is_error": True,
                        }
                    ]
                },
            }
        ]
        md = transcript.render_markdown(events)
        self.assertIn("### ↳ tool error", md)
        self.assertIn("boom", md)

    def test_tool_output_with_backticks_cannot_break_out_of_fence(self) -> None:
        # Tool output that embeds a triple-backtick fence plus injected markup
        # must not be able to terminate the surrounding code block early.
        hostile = "before\n```\n# Injected heading\n<script>alert(1)</script>\nafter"
        events = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "x",
                            "content": hostile,
                        }
                    ]
                },
            }
        ]
        md = transcript.render_markdown(events)

        # The opening fence must be longer than any backtick run in the body, so
        # the embedded ``` is treated as literal content, not a closing fence.
        self.assertIn("````", md)
        self.assertNotIn("```json", md)
        # The hostile payload survives verbatim inside the block.
        self.assertIn("# Injected heading", md)
        self.assertIn("<script>alert(1)</script>", md)

    def test_code_block_fence_outgrows_longest_backtick_run(self) -> None:
        # A body whose longest run is four backticks needs a five-backtick fence.
        lines = transcript._code_block("a ```` b")
        self.assertEqual(lines[0], "`````")
        self.assertEqual(lines[-1], "`````")
        # Plain text still uses the standard three-backtick fence.
        self.assertEqual(transcript._code_block("plain")[0], "```")

    def test_long_blocks_are_truncated(self) -> None:
        big = "A" * (transcript._MAX_BLOCK_CHARS + 500)
        events = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "x", "content": big}
                    ]
                },
            }
        ]
        md = transcript.render_markdown(events)
        self.assertIn("more characters truncated", md)
        self.assertNotIn("A" * (transcript._MAX_BLOCK_CHARS + 1), md)


class TranscriptLogToMarkdownTests(TestCase):
    def test_writes_sidecar_skipping_non_json_preamble(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "claude-main-20260603-120000.log"
            log_path.write_text(
                "Firewall initialized.\n"
                "  IPv4 allowlist: 66 entries\n"
                "not json { broken\n"
                + _log_lines(*SAMPLE_EVENTS),
                encoding="utf-8",
            )

            md_path = transcript.log_to_markdown(log_path)

            self.assertIsNotNone(md_path)
            self.assertEqual(md_path, log_path.with_suffix(".md"))
            self.assertTrue(md_path.exists())
            text = md_path.read_text(encoding="utf-8")
            self.assertIn("Done reading the file.", text)
            self.assertNotIn("Firewall initialized", text)

    def test_returns_none_when_no_claude_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "codex-main.log"
            log_path.write_text("just plain text\nno json here\n", encoding="utf-8")

            self.assertIsNone(transcript.log_to_markdown(log_path))
            self.assertFalse(log_path.with_suffix(".md").exists())

    def test_string_content_assistant_message(self) -> None:
        events = [
            {"type": "assistant", "message": {"content": "plain string answer"}}
        ]
        md = transcript.render_markdown(events)
        self.assertIn("plain string answer", md)
