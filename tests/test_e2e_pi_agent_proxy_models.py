import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def _load_checker():
    path = Path(__file__).parents[1] / "scripts" / "e2e-pi-agent-proxy-models.py"
    spec = importlib.util.spec_from_file_location("e2e_pi_agent_proxy_models", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pi_log(prompt: str, reply: str, *, api: str, stop: str = "stop") -> str:
    # Pi echoes the user prompt (which contains the marker) before the reply.
    events = [
        {"type": "session", "version": 3},
        {"type": "message_end", "message": {"role": "user", "content": prompt}},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "the marker is wanted"},
                    {"type": "text", "text": reply},
                ],
                "api": api,
                "stopReason": stop,
            },
        },
    ]
    return "".join(json.dumps(event) + "\n" for event in events)


class PiAgentProxyModelsE2ETests(unittest.TestCase):
    def _run(self, reply_for, models=None):
        checker = _load_checker()
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            self.assertEqual(command[command.index("--agent") + 1], "pi")
            self.assertNotIn("gateway-secret", command)
            self.assertEqual(kwargs["env"]["TEST_GATEWAY_KEY"], "gateway-secret")
            model = command[command.index("--model") + 1]
            prompt = command[command.index("--prompt-text") + 1]
            log = Path(command[command.index("--log") + 1])
            reply, api = reply_for(checker, model)
            log.write_text(_pi_log(prompt, reply, api=api), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        argv = ["--runtime", "docker", "--key-env", "TEST_GATEWAY_KEY"]
        for model in models or ():
            argv += ["--model", model]
        with (
            TemporaryDirectory() as tmp,
            patch.object(checker, "ROOT", Path(tmp)),
            patch.object(
                checker.agent_proxy,
                "resolve_key",
                return_value=("gateway-secret", "environment"),
            ),
            patch.object(
                checker.agent_proxy,
                "discover_models",
                return_value=list(checker.DEFAULT_MODELS) + ["other"],
            ),
            patch.object(checker.subprocess, "run", side_effect=run),
        ):
            code = checker.main(argv)
            leftovers = list((Path(tmp) / ".project-sandbox" / "e2e").iterdir())
        return checker, code, calls, leftovers

    def test_default_models_cover_each_provider_family_and_pass(self) -> None:
        checker, code, calls, leftovers = self._run(
            lambda checker, model: (
                checker.marker_for(model),
                checker.expected_api(model),
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            [call[call.index("--model") + 1] for call in calls],
            ["gpt-6-luna", "claude-haiku-5-5", "gemini-4-hydrogen"],
        )
        # Only the first session may build the image.
        self.assertEqual(["--no-build" in call for call in calls], [False, True, True])
        self.assertEqual(leftovers, [])
        self.assertEqual(checker.expected_api("gpt-6-luna"), "openai-responses")
        self.assertEqual(checker.expected_api("claude-haiku-5-5"), "openai-completions")
        self.assertEqual(
            checker.expected_api("gemini-4-hydrogen"), "openai-completions"
        )

    def test_empty_reply_fails_even_though_prompt_echo_contains_marker(self) -> None:
        # Regression: agentgateway's Responses translation for Gemini returned
        # empty assistant text, which a plain output marker search accepted.
        _checker, code, _calls, leftovers = self._run(
            lambda checker, model: ("", checker.expected_api(model)),
            models=["gemini-4-hydrogen"],
        )
        self.assertEqual(code, 1)
        self.assertEqual(len(leftovers), 1)

    def test_wrong_api_fails(self) -> None:
        _checker, code, _calls, _leftovers = self._run(
            lambda checker, model: (checker.marker_for(model), "openai-responses"),
            models=["gemini-4-hydrogen"],
        )
        self.assertEqual(code, 1)

    def test_unknown_model_is_rejected_before_any_session(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self._run(lambda checker, model: ("", ""), models=["missing-model"])
        self.assertIn("missing-model", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
